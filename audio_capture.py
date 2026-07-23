"""Microphone capture via arecord subprocess (24 kHz PCM16 mono)."""

from __future__ import annotations

import array
import asyncio
import logging
import math
import os

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
CHANNELS = 1
FORMAT = "S16_LE"
BYTES_PER_SAMPLE = 2
CHUNK_BYTES = 4800  # 100 ms at 24 kHz mono S16_LE

DEFAULT_INPUT_GAIN = 1.0
_INT16_MAX = 32767
_INT16_MIN = -32768
# Soft-knee limiter: below this fraction of full scale, gain is applied with
# no shaping at all. Above it, the excess compresses asymptotically toward
# the ceiling instead of hard-clipping. A gain tuned for a quiet source can
# massively over-drive a louder one -- hard-clipping that would produce an
# audible, harsh "click" per sample over the ceiling; this rounds it off
# smoothly instead.
_LIMITER_KNEE_FRACTION = 0.85


def _soft_limit(value: float) -> int:
    """Gain-scaled sample -> int16, softly compressing anything past the knee."""
    sign = 1.0 if value >= 0 else -1.0
    magnitude = abs(value)
    knee = _LIMITER_KNEE_FRACTION * _INT16_MAX
    if magnitude <= knee:
        limited = magnitude
    else:
        headroom = _INT16_MAX - knee
        over = magnitude - knee
        limited = knee + headroom * (1 - math.exp(-over / headroom))
    result = sign * limited
    if result > _INT16_MAX:
        result = _INT16_MAX
    elif result < _INT16_MIN:
        result = _INT16_MIN
    return int(result)


def _apply_gain(pcm_bytes: bytes, gain: float) -> bytes:
    """Scale mono PCM16 samples by gain, through a soft-knee limiter."""
    if gain == 1.0 or not pcm_bytes:
        return pcm_bytes

    samples = array.array("h")
    samples.frombytes(pcm_bytes)
    for i, value in enumerate(samples):
        samples[i] = _soft_limit(value * gain)
    return samples.tobytes()


class AudioCaptureError(Exception):
    """Raised when capture cannot start or fails unexpectedly."""


class AudioCapture:
    """Capture raw PCM16 audio using an arecord subprocess.

    Returns mono ``CHUNK_BYTES`` chunks with an optional software gain applied
    through a soft-knee limiter.
    """

    def __init__(self, device: str | None = None, *, input_gain: float | None = None):
        self._device = device if device is not None else os.environ.get("AUDIO_INPUT_DEVICE")
        if input_gain is None:
            input_gain = float(os.environ.get("INPUT_GAIN", DEFAULT_INPUT_GAIN))
        self._input_gain = input_gain
        self._process: asyncio.subprocess.Process | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running and self._process is not None

    @property
    def input_gain(self) -> float:
        return self._input_gain

    @input_gain.setter
    def input_gain(self, value: float) -> None:
        """Update gain live (e.g. from a SET_MIC_GAIN command); takes effect
        on the next chunk read -- capture does not need to restart."""
        self._input_gain = value

    def _build_command(self) -> list[str]:
        cmd = [
            "arecord",
            "-f", FORMAT,
            "-r", str(SAMPLE_RATE),
            "-c", str(CHANNELS),
            "-t", "raw",
        ]
        if self._device:
            cmd.extend(["-D", self._device])
        return cmd

    async def start(self) -> None:
        """Start the arecord subprocess."""
        if self.is_running:
            return

        cmd = self._build_command()
        logger.info("Starting audio capture: %s", " ".join(cmd))

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise AudioCaptureError("arecord not found; install alsa-utils") from exc

        if self._process.returncode is not None:
            stderr = await self._read_stderr()
            raise AudioCaptureError(f"arecord failed to start: {stderr}")

        self._running = True

    async def stop(self) -> None:
        """Stop capture and terminate the arecord subprocess."""
        self._running = False
        process = self._process
        self._process = None

        if process is None:
            return

        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

        stderr = await self._read_process_stderr(process)
        if stderr:
            logger.debug("arecord stderr on stop: %s", stderr.strip())

    async def read_chunk(self) -> bytes | None:
        """Read one fixed-size mono chunk, gain-adjusted."""
        if not self.is_running or self._process is None or self._process.stdout is None:
            return None

        if self._process.returncode is not None:
            self._running = False
            return None

        try:
            chunk = await self._process.stdout.readexactly(CHUNK_BYTES)
        except asyncio.IncompleteReadError:
            self._running = False
            return None

        return _apply_gain(chunk, self._input_gain)

    async def drain_continuously(self) -> None:
        """Keep consuming arecord's stdout until cancelled; discards everything.

        Nothing else reads the mic while the calibration prompt plays through
        the speaker (the audio loop is blocked awaiting playback), but arecord
        keeps capturing regardless. Left undrained, its stdout pipe fills and
        arecord's writes block. Run this as a background task for the duration
        of anything that would otherwise leave the mic unread; cancel it
        immediately after. ``drain_buffered_audio()`` remains as a fast, final
        sweep for the brief gap between cancellation and actually stopping.
        """
        if not self.is_running or self._process is None or self._process.stdout is None:
            return
        try:
            while True:
                data = await self._process.stdout.read(CHUNK_BYTES)
                if not data:
                    return
        except asyncio.CancelledError:
            raise

    async def drain_buffered_audio(self, max_drain_sec: float = 1.5) -> int:
        """Discard audio that piled up while nobody was reading the mic.

        ``arecord`` keeps capturing into its pipe even while the audio loop is
        busy elsewhere — notably while the calibration prompt plays through the
        speaker. That backlog contains the prompt's own acoustic echo, and if it
        were fed to the calibrator it would be replayed in a burst and mistaken
        for the user's hello. Read and throw it away until reads start blocking,
        i.e. we've caught up to real time. Returns the number of bytes dropped.
        """
        if not self.is_running or self._process is None or self._process.stdout is None:
            return 0

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_drain_sec
        discarded = 0
        while loop.time() < deadline:
            try:
                data = await asyncio.wait_for(
                    self._process.stdout.read(CHUNK_BYTES),
                    timeout=0.05,
                )
            except asyncio.TimeoutError:
                # No data within 50 ms → the backlog is gone and fresh audio is
                # now arriving at real-time cadence. We're caught up.
                break
            if not data:
                break
            discarded += len(data)
        return discarded

    async def _read_stderr(self) -> str:
        if self._process is None or self._process.stderr is None:
            return ""
        data = await self._process.stderr.read()
        return data.decode("utf-8", errors="replace")

    async def _read_process_stderr(self, process: asyncio.subprocess.Process) -> str:
        if process.stderr is None:
            return ""
        data = await process.stderr.read()
        return data.decode("utf-8", errors="replace")
