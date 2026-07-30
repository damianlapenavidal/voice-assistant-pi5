#!/usr/bin/env python3
"""
Raspberry Pi 5 Voice Assistant Client

A lightweight WebSocket client that connects to the voice-assistant-app
running on a laptop. This script is designed to run on the Pi 5 with
minimal dependencies.

Usage:
    python pi5_client.py ws://LAPTOP_IP:8765

The client performs the following:
  1. Connects to the app's WebSocket server
  2. Sends HELLO with device info
  3. Waits for HELLO_ACK (session config)
  4. Enters a main loop: sends DEVICE_STATUS on recording-state changes (and
     periodically while recording), handles commands
  5. Reconnects with exponential backoff on disconnection
"""

import argparse
import asyncio
import base64
import json
import logging
import os
import platform
import socket
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from audio_capture import AudioCapture, AudioCaptureError, CHUNK_BYTES
from audio_gating import CHUNK_MS, AudioGating, CalibrationPhase, CalibrationStep, chunk_rms
from audio_playback import PlaybackError, PlaybackManager
from calibration_prompt import PROMPT_TEXT, get_calibration_prompt_pcm, prompt_asset_status

try:
    import websockets
    from websockets.exceptions import (
        ConnectionClosed,
        ConnectionClosedError,
        InvalidURI,
        WebSocketException,
    )
except ImportError:
    print("ERROR: 'websockets' library is required.")
    print("Install it with: pip install websockets>=15.0")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEVICE_TYPE = "pi5"
FIRMWARE_VERSION = "0.1.0"
CAPABILITIES = ["audio_capture", "audio_playback", "binary_audio"]
STATUS_INTERVAL_SECONDS = 10

# Phase 5b of ../voice-assistant-piZero2W/docs/battery-plan.md: elide silent
# chunks from the stream instead of sending them, replacing long runs of
# silence with an occasional AUDIO_GAP marker the app uses to synthesize the
# missing audio. Off by default -- "do it last, behind a flag" per the plan.
# The Pi 5 is mains-powered, so the win here is wire traffic and app-side work
# rather than battery; the flag exists so it can be measured either way.
ELIDE_SILENCE = os.getenv("ELIDE_SILENCE", "false").lower() in ("true", "1", "yes")
GAP_FLUSH_INTERVAL_MS = 1000  # batch markers ~once/second

# Binary AUDIO_FRAME/PLAY_AUDIO framing (Phase 4 of the battery plan), used
# only once "binary_audio" is negotiated in HELLO_ACK -- see the app's
# docs/protocol.md "Binary Audio Framing" section for the wire format. Both
# reserved fields are earmarked for Phase 6 (barge-in echo alignment); they
# must stay 0 until that phase assigns them meaning. These constants and the
# struct layouts must stay byte-identical to the Zero 2 W client's, since the
# app decodes both with the same code.
AUDIO_FRAME_TAG = 0x01
PLAY_AUDIO_TAG = 0x02
HEADER_VERSION = 1
_AUDIO_FRAME_STRUCT = struct.Struct(">BBIQI")  # tag, version, seq, ts_ms, reserved
_PLAY_AUDIO_STRUCT = struct.Struct(">BBIBII")  # tag, version, seq, flags, duration_ms, reserved
_FLAG_IS_FINAL = 0x01
_DURATION_UNKNOWN = 0xFFFFFFFF
MAX_RECONNECT_ATTEMPTS = 5
MAX_CALIBRATION_RETRIES = 5
MIN_PROMPT_PCM_BYTES = 4000
INITIAL_BACKOFF_SECONDS = 1.0
# Silence enforced after the "say hello to start" prompt finishes playing, so
# the speaker buffer + acoustic tail decay before the mic starts listening.
PROMPT_SETTLE_SEC = 0.6
# SET_VOLUME's 0-100 range maps onto [0, MAX_PLAYBACK_GAIN]. Everything this
# client plays -- the calibration prompt asset and OpenAI's TTS -- is already
# normalized and near full scale, so volume is pure attenuation: 100% == the
# source untouched (the loudest a normalized signal is meant to play), lower
# values scale it down. Capping at unity keeps the soft-knee limiter from ever
# engaging on these sources, which is what made the slider inert above ~30%:
# gains >1.0 drove loud TTS into the knee, compressing the whole upper half of
# the slider into the ceiling. (The old range boosted above unity for a quiet
# raw-mic loopback source that this thin client no longer has.)
MAX_PLAYBACK_GAIN = 1.0
# SET_MIC_GAIN's 0-100 range maps onto [0, MAX_INPUT_GAIN]. Measured on the
# Pi 5 capture chain: past ~15x, normal speech peaks drive the soft-knee
# limiter hard and clip very easily. The old 50.0 ceiling put that threshold
# at 30% of the slider, so the top ~70% was all limiter mush and the usable
# range was squeezed into the bottom third -- the same flaw MAX_PLAYBACK_GAIN
# above was capped to fix. Cap at the clipping threshold instead, so the full
# slider is usable and 100% is the loudest setting that still holds together.
MAX_INPUT_GAIN = 15.0

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("pi5_client")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_start_time = time.monotonic()


def get_device_id() -> str:
    """Use the hostname as a stable device identifier."""
    return socket.gethostname()


def load_device_env() -> None:
    """Load optional .env from this repo's root (does not override existing env)."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_cpu_temp() -> float | None:
    """Read CPU temperature from the Pi's thermal zone.

    Returns None if the thermal zone file is not available (e.g. on macOS/Linux desktop).
    """
    thermal_path = Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        raw = thermal_path.read_text().strip()
        return int(raw) / 1000.0
    except (FileNotFoundError, ValueError, PermissionError):
        return None


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def make_message(msg_type: str, payload: dict | None = None) -> str:
    """Serialize a protocol message to JSON."""
    msg = {
        "type": msg_type,
        "payload": payload or {},
        "timestamp": utc_now_iso(),
    }
    return json.dumps(msg)


def parse_message(raw: str) -> dict:
    """Deserialize a JSON protocol message."""
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Protocol Messages
# ---------------------------------------------------------------------------


def make_hello() -> str:
    """Create a HELLO message with device info."""
    return make_message("HELLO", {
        "device_id": get_device_id(),
        "device_type": DEVICE_TYPE,
        "firmware_version": FIRMWARE_VERSION,
        "capabilities": CAPABILITIES,
    })


def make_device_status(is_recording: bool) -> str:
    """Create a DEVICE_STATUS heartbeat message."""
    return make_message("DEVICE_STATUS", {
        "battery_percent": None,
        "cpu_temp": get_cpu_temp(),
        "is_recording": is_recording,
        "uptime_seconds": round(time.monotonic() - _start_time, 1),
    })


def make_pong(ping_timestamp: str) -> str:
    """Create a PONG response to a PING."""
    return make_message("PONG", {
        "timestamp": ping_timestamp,
    })


def make_audio_frame(
    pcm_bytes: bytes,
    sequence_number: int,
    capture_timestamp: str | None = None,
    *,
    binary: bool = False,
) -> str | bytes:
    """Create an AUDIO_FRAME message.

    JSON form (default): base64-encoded PCM16 audio, as always. Binary form
    (only once "binary_audio" is negotiated) is a short header + raw PCM --
    base64+JSON was measured at 26.8% wire overhead; the header is ~0.4%.
    """
    capture_ts = capture_timestamp or utc_now_iso()
    if binary:
        capture_ms = int(datetime.fromisoformat(capture_ts).timestamp() * 1000)
        header = _AUDIO_FRAME_STRUCT.pack(
            AUDIO_FRAME_TAG, HEADER_VERSION, sequence_number & 0xFFFFFFFF, capture_ms, 0,
        )
        return header + pcm_bytes
    return make_message("AUDIO_FRAME", {
        "audio": base64.b64encode(pcm_bytes).decode("ascii"),
        "sequence_number": sequence_number,
        "timestamp": capture_ts,
    })


def decode_play_audio_binary(raw: bytes) -> dict | None:
    """Parse a binary PLAY_AUDIO frame into the same payload shape a JSON
    PLAY_AUDIO message would produce.

    Returns None (logged) on any malformed frame -- this is live audio a
    child is listening to, so a bad frame is dropped, not a crashed session.
    """
    try:
        if len(raw) < 2:
            raise ValueError("frame shorter than 2-byte tag+version prefix")
        tag, version = raw[0], raw[1]
        if tag != PLAY_AUDIO_TAG:
            raise ValueError(f"unexpected binary frame tag {tag:#04x}")
        if version != HEADER_VERSION:
            raise ValueError(f"unsupported PLAY_AUDIO header version {version}")
        if len(raw) < _PLAY_AUDIO_STRUCT.size:
            raise ValueError("truncated PLAY_AUDIO header")
        _, _, seq, flags, duration, _ = _PLAY_AUDIO_STRUCT.unpack_from(raw, 0)
        return {
            "type": "PLAY_AUDIO",
            "payload": {
                "audio": raw[_PLAY_AUDIO_STRUCT.size:],
                "sequence_number": seq,
                "is_final": bool(flags & _FLAG_IS_FINAL),
                "duration_ms": None if duration == _DURATION_UNKNOWN else duration,
            },
        }
    except (struct.error, ValueError, IndexError) as exc:
        logger.warning("Malformed binary PLAY_AUDIO frame (%s), dropping", exc)
        return None


def as_pcm_bytes(audio: bytes | bytearray | str | None) -> bytes:
    """Normalize a PLAY_AUDIO payload's `audio` field to raw PCM bytes,
    whether it arrived as base64 (JSON form) or already raw (binary form)."""
    if audio is None:
        return b""
    if isinstance(audio, (bytes, bytearray)):
        return bytes(audio)
    return base64.b64decode(audio)


def make_error(code: str, message: str, recoverable: bool) -> str:
    """Create an ERROR message for device-side failures."""
    return make_message("ERROR", {
        "code": code,
        "message": message,
        "recoverable": recoverable,
    })


def make_playback_complete(sequence_number: int, duration_ms: int) -> str:
    """Notify the app that speaker playback of a final chunk has finished."""
    return make_message("PLAYBACK_COMPLETE", {
        "sequence_number": sequence_number,
        "duration_ms": duration_ms,
    })


def make_audio_gap(duration_ms: int, sequence_number: int, reason: str = "silence") -> str:
    """Tell the app to synthesize duration_ms of audio before sequence_number.

    `sequence_number` is the value the *next real* AUDIO_FRAME will carry --
    elided chunks never increment it, so an unexplained jump in consecutive
    sequence numbers still means a genuinely dropped frame; a jump preceded
    by this marker is accounted for. `reason` is always "silence" today;
    kept general so a future gate (e.g. non-target-speaker elision) can
    reuse this same message type instead of inventing a second one.
    """
    return make_message("AUDIO_GAP", {
        "duration_ms": duration_ms,
        "sequence_number": sequence_number,
        "reason": reason,
    })


def make_calibration_status(phase: str) -> str:
    """Notify the app of the current calibration phase (quiet or speak)."""
    return make_message("CALIBRATION_STATUS", {"phase": phase})


def make_calibration_complete(metrics: dict) -> str:
    """Send calibrated noise/voice levels to the laptop."""
    return make_message("CALIBRATION_COMPLETE", metrics)


# ---------------------------------------------------------------------------
# Client Logic
# ---------------------------------------------------------------------------


class Pi5Client:
    """WebSocket client that implements the device side of the protocol."""

    def __init__(self, server_url: str):
        self.server_url = server_url
        self.session_id: str | None = None
        self.is_recording = False
        self._status_event = asyncio.Event()
        self._binary_audio_enabled = False
        self._running = False
        self._ws = None
        self._audio_capture = AudioCapture()
        self._playback = PlaybackManager()
        self._audio_gating = AudioGating(
            quiet_sec=float(os.getenv("CALIBRATION_QUIET_SEC", "1.0")),
            speak_sec=float(os.getenv("CALIBRATION_SPEAK_SEC", "10.0")),
        )
        self._audio_task: asyncio.Task | None = None
        self._sequence_number = 0
        self._elided_ms = 0.0
        self._mic_muted = False
        self._calibration_playing_prompt = False
        self._calibration_retries = 0
        self._stream_to_laptop = False
        # PLAY_AUDIO is drained by a dedicated worker so the receive loop never
        # blocks on aplay backpressure while the assistant is talking. That
        # keeps live controls -- SET_VOLUME above all -- responsive mid-speech
        # instead of only landing once playback drains (i.e. once it pauses).
        self._playback_queue: asyncio.Queue | None = None
        self._playback_worker: asyncio.Task | None = None

    async def run(self) -> None:
        """Connect to the server and run the main loop.

        Handles reconnection with exponential backoff.
        """
        attempt = 0

        while attempt < MAX_RECONNECT_ATTEMPTS:
            try:
                logger.info(
                    "Connecting to %s (attempt %d/%d)...",
                    self.server_url, attempt + 1, MAX_RECONNECT_ATTEMPTS,
                )
                async with websockets.connect(
                    self.server_url,
                    max_size=8 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    attempt = 0  # Reset on successful connection
                    await self._session(ws)

            except ConnectionClosed as e:
                # Dropped after a successful handshake -- reconnect.
                attempt += 1
                if attempt >= MAX_RECONNECT_ATTEMPTS:
                    logger.error(
                        "Lost connection after %d reconnect attempts. Giving up.",
                        MAX_RECONNECT_ATTEMPTS,
                    )
                    break
                backoff = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Connection closed (%s). Reconnecting in %.1fs...", e, backoff,
                )
                await asyncio.sleep(backoff)

            except InvalidURI as e:
                logger.error("Invalid WebSocket URL: %s", e)
                break

            except (OSError, ConnectionRefusedError, WebSocketException) as e:
                # Includes the SSH-tunnel case: TCP to 127.0.0.1:8765 succeeds
                # (sshd accepts) but the Mac app is not listening yet, so the
                # handshake fails with InvalidMessage instead of ECONNREFUSED.
                attempt += 1
                if attempt >= MAX_RECONNECT_ATTEMPTS:
                    logger.error(
                        "Failed to connect after %d attempts. Giving up.",
                        MAX_RECONNECT_ATTEMPTS,
                    )
                    break
                backoff = INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "Connection failed (%s). Retrying in %.1fs...", e, backoff,
                )
                await asyncio.sleep(backoff)

        self._running = False
        logger.info("Client stopped.")

    async def _session(self, ws) -> None:
        """Perform handshake then enter the main loop."""
        # --- Handshake ---
        await self._handshake(ws)

        # --- Main loop ---
        self._running = True
        logger.info(
            "Entering main loop. DEVICE_STATUS on recording changes, every %ds while recording.",
            STATUS_INTERVAL_SECONDS,
        )

        status_task = asyncio.create_task(self._status_loop(ws))
        try:
            await self._receive_loop(ws)
        finally:
            status_task.cancel()
            try:
                await status_task
            except asyncio.CancelledError:
                pass
            await self._stop_audio()

    async def _handshake(self, ws) -> None:
        """Send HELLO and wait for HELLO_ACK."""
        hello = make_hello()
        logger.info("Sending HELLO (device_id=%s)", get_device_id())
        await ws.send(hello)

        # Wait for HELLO_ACK (with a timeout)
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.error("Timeout waiting for HELLO_ACK")
            raise ConnectionError("No HELLO_ACK received within 10s")

        msg = parse_message(raw)
        if msg.get("type") != "HELLO_ACK":
            logger.error("Expected HELLO_ACK, got: %s", msg.get("type"))
            raise ConnectionError(f"Unexpected message type: {msg.get('type')}")

        payload = msg.get("payload", {})
        self.session_id = payload.get("session_id")
        audio_config = payload.get("audio_config", {})
        logger.info(
            "Handshake complete! session_id=%s, audio_config=%s",
            self.session_id, audio_config,
        )
        negotiated = payload.get("negotiated_capabilities", [])
        self._binary_audio_enabled = "binary_audio" in negotiated
        if self._binary_audio_enabled:
            logger.info("Binary audio frames negotiated with the app")

    async def _status_loop(self, ws) -> None:
        """Send DEVICE_STATUS on recording-state changes, and every
        STATUS_INTERVAL_SECONDS while actually recording.

        Idle is most of the device's life, and cpu_temp only matters mid-
        session, so idle waits here indefinitely instead of sending an
        unconditional heartbeat every 10 s forever. `_start_audio`/`_stop_audio`
        set `_status_event` on every is_recording transition, which both wakes
        an idle wait immediately and interrupts a recording-side timeout early
        -- either way this sends a fresh status right when there's something new
        to report, rather than up to STATUS_INTERVAL_SECONDS stale.
        """
        while True:
            if self.is_recording:
                try:
                    await asyncio.wait_for(
                        self._status_event.wait(), timeout=STATUS_INTERVAL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    pass
            else:
                await self._status_event.wait()
            self._status_event.clear()

            status = make_device_status(self.is_recording)
            try:
                await ws.send(status)
                logger.debug("Sent DEVICE_STATUS (recording=%s)", self.is_recording)
            except ConnectionClosed:
                break

    async def _receive_loop(self, ws) -> None:
        """Listen for messages from the app and handle commands."""
        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)):
                # The only binary frames the app ever sends are PLAY_AUDIO,
                # once negotiated (see the app's docs/protocol.md "Binary
                # Audio Framing" section) -- a malformed one is dropped, not
                # fatal.
                msg = decode_play_audio_binary(raw)
                if msg is None:
                    continue
            else:
                msg = parse_message(raw)
            msg_type = msg.get("type")
            payload = msg.get("payload", {})

            if msg_type == "START_AUDIO_STREAM":
                await self._start_audio(ws, payload)

            elif msg_type == "STOP_AUDIO_STREAM":
                await self._stop_audio()
                logger.info("Audio streaming stopped")

            elif msg_type == "SET_VOLUME":
                volume = payload.get("volume")
                if volume is None:
                    logger.warning("SET_VOLUME received with no volume value")
                else:
                    pct = max(0, min(100, int(volume))) / 100.0
                    gain = pct * MAX_PLAYBACK_GAIN
                    self._playback.playback_gain = gain
                    logger.info("Volume set to %s%% (playback_gain=%.2f)", volume, gain)

            elif msg_type == "SET_MIC_GAIN":
                mic_gain = payload.get("gain")
                if mic_gain is None:
                    logger.warning("SET_MIC_GAIN received with no gain value")
                else:
                    pct = max(0, min(100, int(mic_gain))) / 100.0
                    gain = pct * MAX_INPUT_GAIN
                    self._audio_capture.input_gain = gain
                    logger.info("Mic gain set to %s%% (input_gain=%.2f)", mic_gain, gain)

            elif msg_type == "PLAY_AUDIO":
                # Hand off to the playback worker and return immediately so a
                # SET_VOLUME arriving mid-response isn't stuck behind the queued
                # audio chunks (which drain only at real-time playback rate).
                self._ensure_playback_worker()
                self._playback_queue.put_nowait((ws, payload))

            elif msg_type == "MUTE_MIC":
                self._mic_muted = True
                logger.info("Mic muted (AI speaking)")

            elif msg_type == "UNMUTE_MIC":
                self._mic_muted = False
                self._stream_to_laptop = True
                logger.info("Mic unmuted — streaming to laptop enabled")

            elif msg_type == "SHUTDOWN_DEVICE":
                logger.info("Shutdown requested by app. Disconnecting...")
                await self._stop_audio()
                self._running = False
                await ws.close()
                return

            elif msg_type == "PING":
                ping_ts = payload.get("timestamp", utc_now_iso())
                pong = make_pong(ping_ts)
                await ws.send(pong)
                logger.debug("Responded to PING with PONG")

            else:
                logger.warning("Unknown message type: %s", msg_type)

    async def _start_audio(self, ws, payload: dict | None = None) -> None:
        """Start microphone capture and stream AUDIO_FRAME messages.

        When the app sends ``skip_calibration: true`` (a resume of a paused
        session) the device must NOT re-run calibration — no prompt, no
        re-measuring — and should begin streaming live audio immediately.
        """
        if self.is_recording:
            logger.debug("Audio stream already active")
            return

        skip_calibration = bool((payload or {}).get("skip_calibration", False))

        try:
            await self._audio_capture.start()
        except AudioCaptureError as exc:
            logger.error("Failed to start audio capture: %s", exc)
            await ws.send(make_error("MIC_UNAVAILABLE", str(exc), recoverable=False))
            return

        self.is_recording = True
        self._status_event.set()
        self._sequence_number = 0
        self._elided_ms = 0.0
        self._calibration_retries = 0
        self._mic_muted = False

        if skip_calibration:
            # Resume: levels are already known on the app side. Stream live
            # audio straight away instead of replaying "say hello to start".
            self._stream_to_laptop = True
            logger.info("Audio streaming resumed (skip_calibration) — no prompt")
        else:
            self._stream_to_laptop = False
            self._audio_gating.start_calibration()
            await ws.send(make_calibration_status("quiet"))
            logger.info("Audio streaming started with calibration (%d-byte chunks)", CHUNK_BYTES)

        self._audio_task = asyncio.create_task(self._audio_stream_loop(ws))

    async def _stop_audio(self) -> None:
        """Stop capture task and terminate arecord."""
        self.is_recording = False
        self._status_event.set()

        await self._stop_playback_worker()

        if self._audio_task is not None:
            self._audio_task.cancel()
            try:
                await self._audio_task
            except asyncio.CancelledError:
                pass
            self._audio_task = None

        await self._audio_capture.stop()
        await self._playback.stop()

    async def _audio_stream_loop(self, ws) -> None:
        """Read PCM chunks from arecord and send AUDIO_FRAME messages."""
        try:
            while self.is_recording:
                chunk = await self._audio_capture.read_chunk()
                if chunk is None:
                    if self.is_recording:
                        logger.warning("Audio capture ended unexpectedly")
                        await ws.send(make_error(
                            "MIC_ERROR",
                            "Microphone capture stopped unexpectedly",
                            recoverable=True,
                        ))
                    break

                if self._mic_muted:
                    continue

                if self._audio_gating.is_calibrating:
                    if self._calibration_playing_prompt:
                        continue

                    step = self._audio_gating.process_calibration_chunk(chunk)
                    if step == CalibrationStep.PLAY_PROMPT:
                        if not await self._play_calibration_prompt(ws):
                            await self._fail_calibration(
                                ws,
                                "SPEAKER_ERROR",
                                "Could not play calibration prompt on speaker",
                            )
                    elif step == CalibrationStep.SPEECH_TIMEOUT:
                        await self._retry_calibration_prompt(ws)
                    elif step == CalibrationStep.COMPLETE:
                        # Report levels only. We deliberately do NOT forward the
                        # captured hello audio: the app greets first and then
                        # listens live, so replaying calibration audio would just
                        # inject the prompt tail / a stale "hello" into the chat.
                        metrics = self._audio_gating.calibration_payload()
                        await ws.send(make_calibration_complete(metrics))
                        logger.info(
                            "Calibration complete — noise=%.0f voice=%.0f",
                            metrics.get("noise_floor", 0.0),
                            metrics.get("user_speech_peak", 0.0),
                        )
                    continue

                if not self._stream_to_laptop or self._mic_muted:
                    continue

                # The app needs a faithful timeline of speech vs. silence --
                # OpenAI's turn detection judges when the user is done talking
                # from that -- so silence isn't dropped, only elided from the
                # wire: a cheap approximate RMS (stride=4 -- full precision is
                # calibration's job, not every streamed chunk's) gates it
                # against the threshold calibration already measured, and long
                # runs collapse into an occasional AUDIO_GAP marker the app
                # re-expands into real silence before it reaches OpenAI.
                is_silence = ELIDE_SILENCE and (
                    chunk_rms(chunk, stride=4) < self._audio_gating.speech_start_threshold()
                )

                if is_silence:
                    self._elided_ms += CHUNK_MS
                    if self._elided_ms >= GAP_FLUSH_INTERVAL_MS:
                        await ws.send(make_audio_gap(int(self._elided_ms), self._sequence_number + 1))
                        logger.debug("Sent AUDIO_GAP (%dms elided)", int(self._elided_ms))
                        self._elided_ms = 0.0
                    continue

                if self._elided_ms > 0:
                    # Speech resumed with a partial gap still pending (or
                    # elision just turned off mid-gap) -- flush it before the
                    # resuming frame so the app hears the silence in order.
                    await ws.send(make_audio_gap(int(self._elided_ms), self._sequence_number + 1))
                    logger.debug("Sent AUDIO_GAP (%dms elided, resuming)", int(self._elided_ms))
                    self._elided_ms = 0.0

                self._sequence_number += 1
                capture_ts = utc_now_iso()
                frame = make_audio_frame(
                    chunk, self._sequence_number, capture_ts,
                    binary=self._binary_audio_enabled,
                )
                await ws.send(frame)
                logger.debug("Sent AUDIO_FRAME #%d (%d bytes)", self._sequence_number, len(chunk))
        except ConnectionClosed:
            logger.debug("WebSocket closed during audio streaming")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Audio stream loop error: %s", exc)
            try:
                await ws.send(make_error("MIC_ERROR", str(exc), recoverable=True))
            except ConnectionClosed:
                pass

    async def _play_calibration_prompt(self, ws) -> bool:
        """Play 'Say hello to start' on the Pi speaker, then listen for hello."""
        self._calibration_playing_prompt = True
        await ws.send(make_calibration_status("prompt"))
        # Nothing else reads the mic while this coroutine is running (the
        # audio loop that calls read_chunk() is blocked awaiting us). Keep
        # draining arecord's stdout concurrently so its pipe never backs up.
        drain_task = asyncio.create_task(self._audio_capture.drain_continuously())
        try:
            pcm = await get_calibration_prompt_pcm()
            if len(pcm) < MIN_PROMPT_PCM_BYTES:
                raise PlaybackError(
                    f"Calibration prompt audio too short ({len(pcm)} bytes)",
                )

            device = self._playback.device or "(default ALSA device)"
            logger.info(
                "Playing calibration prompt: %d bytes (~%.1fs) on %s",
                len(pcm),
                len(pcm) / (24000 * 2),
                device,
            )
            await self._playback.play_pcm16_chunk(pcm, is_final=True)
            logger.info("Calibration prompt finished")
            # Let the speaker's ALSA buffer and the room's acoustic tail fully
            # decay before we start listening. Without this the mic captures
            # the prompt's own "...to start" tail and treats it as the user's
            # hello — calibration then "completes" even in total silence.
            await asyncio.sleep(PROMPT_SETTLE_SEC)
        except (PlaybackError, RuntimeError, OSError) as exc:
            logger.error("Calibration prompt playback failed: %s", exc)
            await ws.send(make_error("SPEAKER_ERROR", str(exc), recoverable=True))
            return False
        finally:
            self._calibration_playing_prompt = False
            drain_task.cancel()
            try:
                await drain_task
            except asyncio.CancelledError:
                pass

        # The mic kept recording (including the prompt's own echo) the entire
        # time we were blocked playing it. Drop that backlog so the speak phase
        # only evaluates fresh, real-time audio — otherwise the buffered echo is
        # replayed in a burst and instantly "completes" calibration on silence.
        discarded = await self._audio_capture.drain_buffered_audio()
        if discarded:
            logger.info(
                "Discarded %d bytes (~%.1fs) of buffered mic audio before listening",
                discarded,
                discarded / (24000 * 2),
            )

        await ws.send(make_calibration_status("speak"))
        self._audio_gating.begin_speak_phase()
        return True

    async def _retry_calibration_prompt(self, ws) -> None:
        """Replay the prompt when the user did not say hello in time."""
        self._calibration_retries += 1
        if self._calibration_retries >= MAX_CALIBRATION_RETRIES:
            await self._fail_calibration(
                ws,
                "CALIBRATION_FAILED",
                f'No speech detected after {MAX_CALIBRATION_RETRIES} attempts. '
                f'Wait for "{PROMPT_TEXT}" then say hello.',
            )
            return

        logger.info(
            "Calibration retry %d/%d — replaying prompt",
            self._calibration_retries,
            MAX_CALIBRATION_RETRIES,
        )
        await ws.send(make_calibration_status("retry"))
        self._audio_gating.reset_for_prompt_retry()
        if not await self._play_calibration_prompt(ws):
            await self._fail_calibration(
                ws,
                "SPEAKER_ERROR",
                "Could not replay calibration prompt on speaker",
            )

    async def _fail_calibration(self, ws, code: str, message: str) -> None:
        """Abort calibration and stop the audio stream."""
        logger.error("Calibration failed: %s", message)
        self._audio_gating.cancel_calibration()
        await ws.send(make_error(code, message, recoverable=True))
        await self._stop_audio()

    def _ensure_playback_worker(self) -> None:
        """Start the background PLAY_AUDIO consumer if it isn't running."""
        if self._playback_worker is not None and not self._playback_worker.done():
            return
        self._playback_queue = asyncio.Queue()
        self._playback_worker = asyncio.create_task(self._playback_worker_loop())

    async def _playback_worker_loop(self) -> None:
        """Play queued PLAY_AUDIO chunks in order, off the receive loop.

        PlaybackManager writes paced sub-chunks and applies the gain current
        at each write, so a SET_VOLUME lands on audio not yet written --
        including audio still queued here. That pacing means this loop now
        blocks for roughly the real duration of the response, which is
        exactly why it must not run on the receive loop.
        """
        assert self._playback_queue is not None
        # Captured once: _stop_playback_worker() nils self._playback_queue
        # before cancelling this task, so a cancellation landing mid-
        # _handle_play_audio (e.g. inside finalize_streaming()'s subprocess
        # wait) would otherwise hit task_done() on None instead of
        # propagating CancelledError, crashing the client on a race that's
        # one STOP_AUDIO_STREAM-right-after-a-final-chunk away from real.
        queue = self._playback_queue
        while True:
            ws, payload = await queue.get()
            try:
                await self._handle_play_audio(ws, payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let one frame kill playback
                logger.error("Playback worker error: %s", exc)
            finally:
                queue.task_done()

    async def _stop_playback_worker(self) -> None:
        """Cancel the playback worker and drop any un-played chunks."""
        worker = self._playback_worker
        self._playback_worker = None
        self._playback_queue = None
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass

    async def _handle_play_audio(self, ws, payload: dict) -> None:
        """Decode PLAY_AUDIO payload and pipe PCM to aplay."""
        seq = payload.get("sequence_number")
        is_final = payload.get("is_final", False)

        try:
            pcm_bytes = as_pcm_bytes(payload.get("audio"))

            if is_final and self._playback.is_streaming:
                if pcm_bytes:
                    await self._playback.play_pcm16_chunk(pcm_bytes, is_final=False)
                duration_sec = await self._playback.finalize_streaming()
            elif is_final:
                if not pcm_bytes:
                    logger.warning(
                        "PLAY_AUDIO is_final=True with empty audio and no active stream",
                    )
                    duration_sec = 0.0
                else:
                    duration_sec = await self._playback.play_pcm16_chunk(
                        pcm_bytes,
                        is_final=True,
                    )
            else:
                duration_sec = await self._playback.play_pcm16_chunk(
                    pcm_bytes,
                    is_final=False,
                )

            logger.debug(
                "Played PLAY_AUDIO frame #%s (%d bytes, final=%s)",
                seq, len(pcm_bytes), is_final,
            )

            if is_final:
                recovery_sec = 0.3
                await asyncio.sleep(recovery_sec)
                duration_ms = int((duration_sec + recovery_sec) * 1000)
                await ws.send(make_playback_complete(seq or 0, duration_ms))
                logger.info(
                    "Sent PLAYBACK_COMPLETE seq=%s duration_ms=%d",
                    seq, duration_ms,
                )
        except PlaybackError as exc:
            logger.error("Playback error: %s", exc)
            await ws.send(make_error("SPEAKER_ERROR", str(exc), recoverable=True))
        except Exception as exc:
            logger.error("Failed to play audio frame #%s: %s", seq, exc)
            await ws.send(make_error("AUDIO_FORMAT_ERROR", str(exc), recoverable=True))

    async def shutdown(self) -> None:
        """Gracefully close the connection."""
        self._running = False
        await self._stop_audio()
        if self._ws:
            await self._ws.close()
            logger.info("WebSocket connection closed.")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Pi 5 Voice Assistant Client",
        epilog="Example: python pi5_client.py ws://192.168.1.100:8765",
    )
    parser.add_argument(
        "server_url",
        help="WebSocket URL of the voice-assistant-app (e.g. ws://LAPTOP_IP:8765)",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug-level logging",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    load_device_env()

    # ELIDE_SILENCE is read once at import time (above) so tests can patch it
    # as a plain module attribute -- but that means it's frozen before
    # load_device_env() has populated os.environ from .env. Re-derive it now
    # that .env is actually loaded, or setting it there would silently do
    # nothing (every other .env-driven value in this file is read lazily,
    # inside __init__ or inline, specifically to avoid this).
    global ELIDE_SILENCE
    ELIDE_SILENCE = os.getenv("ELIDE_SILENCE", "false").lower() in ("true", "1", "yes")

    logger.info("Pi 5 Voice Assistant Client v%s", FIRMWARE_VERSION)
    logger.info("Device ID: %s | Platform: %s", get_device_id(), platform.platform())
    logger.info("Target server: %s", args.server_url)
    logger.info(
        "Audio devices: input=%s output=%s",
        os.environ.get("AUDIO_INPUT_DEVICE", "(default)"),
        os.environ.get("AUDIO_OUTPUT_DEVICE", "(default)"),
    )
    logger.info("Silence elision: %s", "on" if ELIDE_SILENCE else "off")
    logger.info("Calibration prompt asset: %s", prompt_asset_status())

    client = Pi5Client(args.server_url)

    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        logger.info("Interrupted by user. Shutting down...")
        sys.exit(0)


if __name__ == "__main__":
    main()
