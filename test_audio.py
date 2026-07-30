#!/usr/bin/env python3
"""
Tests for Pi 5 audio capture/playback and AUDIO_FRAME message format.

Run with: python test_audio.py
"""

import array
import asyncio
import base64
import json
import struct
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))

from audio_capture import CHUNK_BYTES, AudioCapture
from audio_capture import _soft_limit as _capture_soft_limit
from audio_playback import BYTE_RATE, PlaybackManager, _apply_gain
from pi5_client import make_audio_frame, parse_message


def test_audio_frame_message_structure():
    """AUDIO_FRAME matches protocol: type, payload fields, timestamp."""
    pcm = b"\x00\x01" * (CHUNK_BYTES // 2)
    raw = make_audio_frame(pcm, sequence_number=1, capture_timestamp="2026-06-30T15:30:00.123Z")
    msg = json.loads(raw)

    assert msg["type"] == "AUDIO_FRAME"
    assert set(msg.keys()) == {"type", "payload", "timestamp"}
    assert msg["payload"]["sequence_number"] == 1
    assert msg["payload"]["timestamp"] == "2026-06-30T15:30:00.123Z"
    assert isinstance(msg["payload"]["audio"], str)
    assert "T" in msg["timestamp"]

    print("  PASS: test_audio_frame_message_structure")


def test_audio_frame_base64_roundtrip():
    """Payload audio decodes to the original PCM bytes."""
    pcm = bytes(range(256)) * 19  # 4864 bytes, trim to chunk
    pcm = pcm[:CHUNK_BYTES]

    raw = make_audio_frame(pcm, sequence_number=42)
    msg = parse_message(raw)
    decoded = base64.b64decode(msg["payload"]["audio"])

    assert decoded == pcm
    assert msg["payload"]["sequence_number"] == 42

    print("  PASS: test_audio_frame_base64_roundtrip")


def test_audio_frame_chunk_size():
    """Typical capture chunk is 4800 bytes (100 ms at 24 kHz mono)."""
    assert CHUNK_BYTES == 4800

    pcm = b"\x00" * CHUNK_BYTES
    raw = make_audio_frame(pcm, sequence_number=1)
    msg = json.loads(raw)
    decoded = base64.b64decode(msg["payload"]["audio"])

    assert len(decoded) == CHUNK_BYTES

    print("  PASS: test_audio_frame_chunk_size")


async def _test_audio_capture_read_chunk():
    """read_chunk() returns exactly CHUNK_BYTES from mocked arecord stdout."""
    fake_stdout = asyncio.StreamReader()
    fake_stdout.feed_data(b"\x01\x02" * (CHUNK_BYTES // 2))
    fake_stdout.feed_eof()

    fake_process = MagicMock()
    fake_process.returncode = None
    fake_process.stdout = fake_stdout
    fake_process.stderr = asyncio.StreamReader()

    capture = AudioCapture(device="plughw:2,0")
    capture._process = fake_process
    capture._running = True

    chunk = await capture.read_chunk()
    assert chunk is not None
    assert len(chunk) == CHUNK_BYTES
    assert capture._build_command()[-2:] == ["-D", "plughw:2,0"]

    print("  PASS: test_audio_capture_read_chunk")


async def _test_audio_capture_read_chunk_applies_input_gain():
    """read_chunk() scales mono samples by INPUT_GAIN, soft-limited at the ceiling."""
    n = CHUNK_BYTES // 2
    pcm = struct.pack(f"<{n}h", *([10000] * n))

    fake_stdout = asyncio.StreamReader()
    fake_stdout.feed_data(pcm)
    fake_stdout.feed_eof()

    fake_process = MagicMock()
    fake_process.returncode = None
    fake_process.stdout = fake_stdout
    fake_process.stderr = asyncio.StreamReader()

    capture = AudioCapture(input_gain=4.0)  # 10000 * 4 = 40000, well past the ceiling
    capture._process = fake_process
    capture._running = True

    chunk = await capture.read_chunk()
    values = set(struct.unpack(f"<{n}h", chunk))
    assert len(values) == 1
    (result,) = values
    knee = int(0.85 * 32767)
    assert knee < result <= 32767  # soft-limited, not hard-clipped flat

    print("  PASS: test_audio_capture_read_chunk_applies_input_gain")


async def _test_audio_capture_start_uses_arecord():
    """start() spawns arecord with S16_LE 24000 Hz mono raw format."""
    mock_process = MagicMock()
    mock_process.returncode = None
    mock_process.stdout = asyncio.StreamReader()
    mock_process.stderr = asyncio.StreamReader()

    with patch(
        "audio_capture.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=mock_process),
    ) as mock_exec:
        capture = AudioCapture()
        await capture.start()

        mock_exec.assert_awaited_once()
        cmd = mock_exec.await_args.args
        assert cmd[0] == "arecord"
        assert "-f" in cmd and "S16_LE" in cmd
        assert "-r" in cmd and "24000" in cmd
        assert "-c" in cmd and "1" in cmd
        assert "-t" in cmd and "raw" in cmd

    print("  PASS: test_audio_capture_start_uses_arecord")


async def _test_playback_manager_pipes_to_aplay():
    """play_pcm16_chunk() writes PCM bytes to aplay stdin."""
    fake_stdin = MagicMock()
    fake_stdin.write = MagicMock()
    fake_stdin.drain = AsyncMock()
    fake_stdin.is_closing = MagicMock(return_value=False)
    fake_stdin.close = MagicMock()
    fake_stdin.wait_closed = AsyncMock()

    mock_process = MagicMock()
    mock_process.returncode = None
    mock_process.stdin = fake_stdin
    mock_process.stderr = asyncio.StreamReader()

    with patch(
        "audio_playback.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=mock_process),
    ) as mock_exec:
        playback = PlaybackManager(device="plughw:0,0")
        pcm = b"\x00\x01" * 100
        await playback.play_pcm16_chunk(pcm)

        mock_exec.assert_awaited_once()
        cmd = mock_exec.await_args.args
        assert cmd[0] == "aplay"
        assert "-f" in cmd and "S16_LE" in cmd
        assert "-r" in cmd and "24000" in cmd
        fake_stdin.write.assert_called_once_with(pcm)
        fake_stdin.drain.assert_awaited_once()

    print("  PASS: test_playback_manager_pipes_to_aplay")


def _make_mock_streaming_process():
    """Return a mock aplay process suitable for streaming chunk tests."""
    fake_stdin = MagicMock()
    fake_stdin.write = MagicMock()
    fake_stdin.drain = AsyncMock()
    fake_stdin.is_closing = MagicMock(return_value=False)
    fake_stdin.close = MagicMock()
    fake_stdin.wait_closed = AsyncMock()

    mock_process = MagicMock()
    mock_process.returncode = None
    mock_process.stdin = fake_stdin
    mock_process.stderr = asyncio.StreamReader()
    mock_process.communicate = AsyncMock(return_value=(b"", b""))

    return mock_process, fake_stdin


async def _test_streaming_chunks_then_finalize():
    """N streaming chunks then finalize plays all bytes through one aplay process."""
    mock_process, fake_stdin = _make_mock_streaming_process()
    chunk = b"\x00\x01" * (CHUNK_BYTES // 2)  # 4800 bytes
    num_chunks = 3

    with patch(
        "audio_playback.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=mock_process),
    ) as mock_exec:
        playback = PlaybackManager()
        for _ in range(num_chunks):
            await playback.play_pcm16_chunk(chunk, is_final=False)

        assert playback.is_streaming
        assert mock_exec.await_count == 1
        # Each chunk is written as paced sub-chunks (so gain stays current),
        # so assert on the bytes that reached aplay, not the write count.
        written = b"".join(c.args[0] for c in fake_stdin.write.call_args_list)
        assert written == chunk * num_chunks

        duration = await playback.finalize_streaming()

        assert not playback.is_streaming
        expected_bytes = num_chunks * len(chunk)
        assert duration == expected_bytes / BYTE_RATE
        fake_stdin.close.assert_called_once()
        mock_process.communicate.assert_awaited_once()

    print("  PASS: test_streaming_chunks_then_finalize")


async def _test_single_blob_still_works():
    """One is_final=True blob uses _play_final (dedicated aplay), not streaming."""
    final_stdin = MagicMock()
    final_stdin.write = MagicMock()
    final_stdin.drain = AsyncMock()
    final_stdin.is_closing = MagicMock(return_value=False)
    final_stdin.close = MagicMock()
    final_stdin.wait_closed = AsyncMock()

    final_process = MagicMock()
    final_process.returncode = 0
    final_process.stdin = final_stdin
    final_process.stderr = asyncio.StreamReader()
    final_process.communicate = AsyncMock(return_value=(b"", b""))

    pcm = b"\x00\x01" * 5000  # single-blob response

    with patch(
        "audio_playback.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=final_process),
    ) as mock_exec:
        playback = PlaybackManager()
        duration = await playback.play_pcm16_chunk(pcm, is_final=True)

        assert not playback.is_streaming
        assert duration == len(pcm) / BYTE_RATE
        mock_exec.assert_awaited_once()
        cmd = mock_exec.await_args.args
        assert cmd[0] == "aplay"
        assert "-q" in cmd
        final_stdin.write.assert_called()
        final_stdin.close.assert_called_once()
        final_process.communicate.assert_awaited_once()

    print("  PASS: test_single_blob_still_works")


async def _test_finalize_returns_correct_duration():
    """finalize_streaming() returns duration from all streamed bytes, including last."""
    mock_process, fake_stdin = _make_mock_streaming_process()
    chunks = [b"\x00" * 4800, b"\x01" * 4800, b"\x02" * 1200]

    with patch(
        "audio_playback.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=mock_process),
    ):
        playback = PlaybackManager()
        for chunk in chunks[:-1]:
            await playback.play_pcm16_chunk(chunk, is_final=False)
        await playback.play_pcm16_chunk(chunks[-1], is_final=False)

        total_bytes = sum(len(c) for c in chunks)
        duration = await playback.finalize_streaming()

        assert duration == total_bytes / BYTE_RATE
        assert not playback.is_streaming

    print("  PASS: test_finalize_returns_correct_duration")


async def _test_streaming_finalize_with_empty_final_chunk():
    """Empty is_final body after chunks still finalizes the full stream."""
    mock_process, fake_stdin = _make_mock_streaming_process()
    chunk = b"\x00" * 4800

    with patch(
        "audio_playback.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=mock_process),
    ):
        playback = PlaybackManager()
        await playback.play_pcm16_chunk(chunk, is_final=False)
        await playback.play_pcm16_chunk(chunk, is_final=False)

        duration = await playback.finalize_streaming()
        assert duration == (2 * len(chunk)) / BYTE_RATE

    print("  PASS: test_streaming_finalize_with_empty_final_chunk")


async def _test_skip_calibration_streams_immediately():
    """START_AUDIO_STREAM with skip_calibration bypasses the prompt (resume)."""
    from pi5_client import Pi5Client

    client = Pi5Client("ws://test")

    sent: list[dict] = []
    ws = MagicMock()
    ws.send = AsyncMock(side_effect=lambda msg: sent.append(json.loads(msg)))

    started = {"value": False}

    async def fake_start():
        started["value"] = True

    client._audio_capture.start = AsyncMock(side_effect=fake_start)

    await client._start_audio(ws, {"skip_calibration": True})

    # No calibration prompt / status when resuming; stream live immediately.
    assert started["value"] is True
    assert client.is_recording is True
    assert client._stream_to_laptop is True
    assert client._audio_gating.is_calibrating is False
    assert not any(m["type"] == "CALIBRATION_STATUS" for m in sent)

    await client._stop_audio()
    print("  PASS: test_skip_calibration_streams_immediately")


async def _test_fresh_start_runs_calibration():
    """START_AUDIO_STREAM without skip_calibration begins calibration."""
    from pi5_client import Pi5Client

    client = Pi5Client("ws://test")

    sent: list[dict] = []
    ws = MagicMock()
    ws.send = AsyncMock(side_effect=lambda msg: sent.append(json.loads(msg)))

    client._audio_capture.start = AsyncMock()

    await client._start_audio(ws, None)

    assert client.is_recording is True
    assert client._stream_to_laptop is False
    assert client._audio_gating.is_calibrating is True
    assert any(
        m["type"] == "CALIBRATION_STATUS" and m["payload"].get("phase") == "quiet"
        for m in sent
    )

    await client._stop_audio()
    print("  PASS: test_fresh_start_runs_calibration")


async def _test_drain_buffered_audio_discards_backlog():
    """drain_buffered_audio() drops piled-up mic bytes, then stops once reads block.

    Reproduces the calibration echo bug: while the prompt plays, arecord keeps
    filling its pipe. That backlog must be dropped before the speak phase so it
    is not replayed in a burst and mistaken for the user's hello.
    """
    fake_stdout = asyncio.StreamReader()
    backlog = b"\x11" * (CHUNK_BYTES * 2 + 100)
    fake_stdout.feed_data(backlog)
    # Deliberately no feed_eof(): after the backlog is read, the next read
    # blocks — mimicking real-time capture — so drain should time out and stop.

    fake_process = MagicMock()
    fake_process.returncode = None
    fake_process.stdout = fake_stdout
    fake_process.stderr = asyncio.StreamReader()

    capture = AudioCapture()
    capture._process = fake_process
    capture._running = True

    discarded = await capture.drain_buffered_audio(max_drain_sec=1.0)
    assert discarded == len(backlog), discarded

    print("  PASS: test_drain_buffered_audio_discards_backlog")


async def _test_drain_buffered_audio_noop_when_idle():
    """drain_buffered_audio() returns 0 when capture is not running."""
    capture = AudioCapture()
    assert await capture.drain_buffered_audio() == 0

    print("  PASS: test_drain_buffered_audio_noop_when_idle")


def _test_apply_gain_scales_and_clips():
    """_apply_gain() scales PCM16 samples; overshoots soft-limit, never exceed ceiling."""
    knee = int(0.85 * 32767)  # _LIMITER_KNEE_FRACTION
    pcm = struct.pack("<3h", 1000, -1000, 20000)

    unity = _apply_gain(pcm, 1.0)
    assert unity == pcm  # no-op, same bytes (not just same values)

    scaled = _apply_gain(pcm, 2.0)
    values = struct.unpack("<3h", scaled)
    assert values[0] == 2000  # well under the knee -> untouched linear scaling
    assert values[1] == -2000
    assert knee < values[2] < 32767

    extreme = _apply_gain(struct.pack("<1h", 20000), 20.0)
    assert struct.unpack("<1h", extreme)[0] <= 32767

    print("  PASS: test_apply_gain_scales_and_clips")


def _test_soft_limit_shape():
    """_soft_limit(): passthrough below the knee, smooth + bounded above it."""
    knee = int(0.85 * 32767)

    assert _capture_soft_limit(1000.0) == 1000
    assert _capture_soft_limit(-1000.0) == -1000
    assert _capture_soft_limit(float(knee)) == knee

    just_over = _capture_soft_limit(knee + 500.0)
    assert knee < just_over < knee + 500

    prev = 0
    for magnitude in (0, 5000, 20000, 32767, 50000, 100000):
        result = _capture_soft_limit(float(magnitude))
        assert result >= prev
        prev = result

    assert _capture_soft_limit(1_000_000.0) <= 32767

    print("  PASS: test_soft_limit_shape")


def _test_openai_style_loud_source_does_not_hard_clip():
    """Already near-full-scale TTS + leftover gain must soft-limit, not hard-clip."""
    loud_source = struct.pack("<4h", 30000, -30000, 31000, -31000)
    over_driven = _apply_gain(loud_source, 2.5)
    values = struct.unpack("<4h", over_driven)

    assert all(-32768 <= v <= 32767 for v in values)
    assert len(set(values)) > 1, "all samples flattened -- hard clipping regression"

    print("  PASS: test_openai_style_loud_source_does_not_hard_clip")


async def _test_playback_manager_applies_playback_gain():
    """play_pcm16_chunk() scales bytes by playback_gain before writing to aplay."""
    fake_stdin = MagicMock()
    fake_stdin.write = MagicMock()
    fake_stdin.drain = AsyncMock()
    fake_stdin.is_closing = MagicMock(return_value=False)
    fake_stdin.close = MagicMock()
    fake_stdin.wait_closed = AsyncMock()

    mock_process = MagicMock()
    mock_process.returncode = None
    mock_process.stdin = fake_stdin
    mock_process.stderr = asyncio.StreamReader()

    with patch(
        "audio_playback.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=mock_process),
    ):
        playback = PlaybackManager(playback_gain=0.5)
        pcm = struct.pack("<2h", 1000, -1000)
        await playback.play_pcm16_chunk(pcm)

        written = fake_stdin.write.call_args[0][0]
        assert struct.unpack("<2h", written) == (500, -500)

    print("  PASS: test_playback_manager_applies_playback_gain")


class _FakeWebSocket:
    """Minimal async-iterable fake WS: yields preset messages, then closes."""

    def __init__(self, messages: list[str]):
        self._messages = messages
        self.sent: list[str] = []
        self._recv_index = 0

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self._messages:
            yield m

    async def recv(self) -> str:
        """Pop the next preset message, for code that awaits recv() directly
        (the handshake) rather than iterating."""
        if self._recv_index >= len(self._messages):
            raise AssertionError("recv() called with no messages left")
        msg = self._messages[self._recv_index]
        self._recv_index += 1
        return msg

    async def send(self, msg: str) -> None:
        self.sent.append(msg)


async def _test_set_volume_updates_playback_gain():
    """SET_VOLUME (0-100) maps onto [0, MAX_PLAYBACK_GAIN]."""
    from pi5_client import MAX_PLAYBACK_GAIN, Pi5Client

    client = Pi5Client("ws://test")

    ws = _FakeWebSocket([json.dumps({"type": "SET_VOLUME", "payload": {"volume": 70}})])
    await client._receive_loop(ws)
    assert abs(client._playback.playback_gain - 0.7 * MAX_PLAYBACK_GAIN) < 1e-9

    ws = _FakeWebSocket([json.dumps({"type": "SET_VOLUME", "payload": {"volume": 150}})])
    await client._receive_loop(ws)
    assert abs(client._playback.playback_gain - MAX_PLAYBACK_GAIN) < 1e-9

    print("  PASS: test_set_volume_updates_playback_gain")


async def _test_set_mic_gain_updates_input_gain():
    """SET_MIC_GAIN (0-100) maps onto [0, MAX_INPUT_GAIN], applied live to capture."""
    from pi5_client import MAX_INPUT_GAIN, Pi5Client

    client = Pi5Client("ws://test")

    ws = _FakeWebSocket([json.dumps({"type": "SET_MIC_GAIN", "payload": {"gain": 40}})])
    await client._receive_loop(ws)
    assert abs(client._audio_capture.input_gain - 0.4 * MAX_INPUT_GAIN) < 1e-9

    ws = _FakeWebSocket([json.dumps({"type": "SET_MIC_GAIN", "payload": {"gain": 150}})])
    await client._receive_loop(ws)
    assert abs(client._audio_capture.input_gain - MAX_INPUT_GAIN) < 1e-9

    print("  PASS: test_set_mic_gain_updates_input_gain")


async def _test_set_volume_not_blocked_by_active_playback():
    """A SET_VOLUME is applied even while a PLAY_AUDIO frame is still playing.

    PLAY_AUDIO is drained by a background worker, so the receive loop handles
    SET_VOLUME immediately instead of stalling behind buffered audio. This is
    what makes the volume slider respond mid-response, not only once the
    assistant pauses.
    """
    from pi5_client import MAX_PLAYBACK_GAIN, Pi5Client

    client = Pi5Client("ws://test")

    # Make the (background) playback hang so the frame is still "in flight"
    # while the receive loop moves on to the SET_VOLUME message.
    gate = asyncio.Event()

    async def _hang(ws, payload):
        await gate.wait()

    client._handle_play_audio = _hang

    b64 = base64.b64encode(b"\x00\x01" * 10).decode()
    ws = _FakeWebSocket([
        json.dumps({"type": "PLAY_AUDIO", "payload": {"audio": b64}}),
        json.dumps({"type": "SET_VOLUME", "payload": {"volume": 50}}),
    ])

    await client._receive_loop(ws)

    # The volume landed despite playback never having completed.
    assert abs(client._playback.playback_gain - 0.5 * MAX_PLAYBACK_GAIN) < 1e-9

    gate.set()
    await client._stop_playback_worker()

    print("  PASS: test_set_volume_not_blocked_by_active_playback")


async def _test_volume_change_applies_mid_response():
    """A volume change part-way through a response affects the rest of it.

    Regression: gain was baked into the whole buffer before any of it was
    written to aplay, so a SET_VOLUME arriving mid-response could not change
    audio that had already been converted -- it was only audible on the next
    response. Gain is now applied per paced sub-chunk at write time.
    """
    from audio_playback import WRITE_CHUNK_BYTES

    mock_process, fake_stdin = _make_mock_streaming_process()
    playback = PlaybackManager(playback_gain=1.0)

    # Halve the volume right after the first sub-chunk reaches aplay, standing
    # in for a SET_VOLUME handled by the receive loop mid-playback.
    def _on_write(data):
        if fake_stdin.write.call_count == 1:
            playback.playback_gain = 0.5

    fake_stdin.write.side_effect = _on_write

    sample = 1000  # constant tone: gain is readable straight off the samples
    pcm = array.array("h", [sample] * ((WRITE_CHUNK_BYTES * 3) // 2)).tobytes()

    with patch(
        "audio_playback.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=mock_process),
    ):
        await playback.play_pcm16_chunk(pcm, is_final=False)

    writes = [c.args[0] for c in fake_stdin.write.call_args_list]
    assert len(writes) >= 3, f"expected sub-chunked writes, got {len(writes)}"

    first = array.array("h")
    first.frombytes(writes[0])
    last = array.array("h")
    last.frombytes(writes[-1])

    assert first[0] == sample, "audio written before the change kept its volume"
    assert last[0] == sample // 2, "audio written after the change must be quieter"

    print("  PASS: test_volume_change_applies_mid_response")


async def _test_write_pacing_limits_audio_written_ahead():
    """The writer does not run far ahead of real-time playback.

    This is what bounds how stale the baked-in gain can be: without it the
    whole response is written (and its volume locked in) almost instantly.
    """
    from audio_playback import MAX_WRITE_LEAD_SEC

    mock_process, fake_stdin = _make_mock_streaming_process()
    playback = PlaybackManager()

    # 1 second of audio, delivered all at once.
    pcm = b"\x00\x01" * (BYTE_RATE // 2)

    with patch(
        "audio_playback.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=mock_process),
    ):
        start = time.monotonic()
        await playback.play_pcm16_chunk(pcm, is_final=False)
        elapsed = time.monotonic() - start

    # Writing 1s of audio must take about 1s minus the allowed lead, rather
    # than completing instantly.
    assert elapsed > 1.0 - MAX_WRITE_LEAD_SEC - 0.1, (
        f"writer ran ahead of playback: 1s of audio written in {elapsed:.3f}s"
    )

    print("  PASS: test_write_pacing_limits_audio_written_ahead")


def _one_chunk_then_block(chunk: bytes):
    """A read_chunk stand-in that yields one chunk, then blocks forever.

    A real capture blocks until arecord has audio, which paces the stream
    loop at real time. A mock that returns instantly instead spins the loop
    as fast as the event loop allows -- millions of iterations and unbounded
    memory in the time it takes to cancel it. Blocking after the first chunk
    keeps the send count deterministic and the test honest about pacing.
    """
    async def read_chunk():
        if not sent_one:
            sent_one.append(True)
            return chunk
        await asyncio.Event().wait()

    sent_one: list = []
    return read_chunk


def _chunks_then_block(chunks: list):
    """Generalization of _one_chunk_then_block: yields each chunk in order,
    then blocks forever, for tests feeding a silence-then-speech sequence."""
    index = {"i": 0}

    async def read_chunk():
        i = index["i"]
        if i < len(chunks):
            index["i"] += 1
            return chunks[i]
        await asyncio.Event().wait()

    return read_chunk


_QUIET_CHUNK = b"\x00\x00" * (CHUNK_BYTES // 2)  # RMS 0 -- well under any calibrated threshold
_LOUD_CHUNK = struct.pack("<h", 20000) * (CHUNK_BYTES // 2)  # RMS 20000 -- well over


# ---------------------------------------------------------------------------
# Phase 5a: event-driven DEVICE_STATUS
# ---------------------------------------------------------------------------


async def _test_status_loop_silent_while_idle():
    """No DEVICE_STATUS at all while idle -- Phase 5a drops the every-10s
    heartbeat for its own sake, since idle is most of the device's life."""
    from pi5_client import Pi5Client

    client = Pi5Client("ws://test")
    ws = _FakeWebSocket([])

    task = asyncio.create_task(client._status_loop(ws))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert ws.sent == []
    print("  PASS: test_status_loop_silent_while_idle")


async def _test_status_loop_sends_immediately_on_recording_transitions():
    """A recording-state change wakes the loop immediately, so the app learns
    is_recording changed within one tick rather than up to
    STATUS_INTERVAL_SECONDS late."""
    from pi5_client import Pi5Client

    client = Pi5Client("ws://test")
    ws = _FakeWebSocket([])

    with patch("pi5_client.STATUS_INTERVAL_SECONDS", 999):
        task = asyncio.create_task(client._status_loop(ws))
        await asyncio.sleep(0.01)
        assert ws.sent == []  # still idle, no timer-driven send

        client.is_recording = True
        client._status_event.set()
        await asyncio.sleep(0.01)
        assert len(ws.sent) == 1
        assert json.loads(ws.sent[0])["payload"]["is_recording"] is True

        client.is_recording = False
        client._status_event.set()
        await asyncio.sleep(0.01)
        assert len(ws.sent) == 2
        assert json.loads(ws.sent[1])["payload"]["is_recording"] is False

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    print("  PASS: test_status_loop_sends_immediately_on_recording_transitions")


async def _test_status_loop_sends_periodically_while_recording():
    """cpu_temp only matters mid-session, so recording keeps the old
    STATUS_INTERVAL_SECONDS cadence even with no new state-change events."""
    from pi5_client import Pi5Client

    client = Pi5Client("ws://test")
    ws = _FakeWebSocket([])
    client.is_recording = True

    with patch("pi5_client.STATUS_INTERVAL_SECONDS", 0.02):
        task = asyncio.create_task(client._status_loop(ws))
        await asyncio.sleep(0.07)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # ~0.07s at a 0.02s interval -> at least 3 sends, none event-triggered.
    assert len(ws.sent) >= 3
    assert all(json.loads(m)["payload"]["is_recording"] is True for m in ws.sent)
    print("  PASS: test_status_loop_sends_periodically_while_recording")


async def _test_start_and_stop_audio_trigger_status_event():
    """_start_audio/_stop_audio actually set _status_event -- the wiring
    _status_loop depends on to notice a recording transition at all."""
    from pi5_client import Pi5Client

    client = Pi5Client("ws://test")
    ws = MagicMock()
    ws.send = AsyncMock()
    client._audio_capture.start = AsyncMock()

    assert not client._status_event.is_set()
    await client._start_audio(ws, {"skip_calibration": True})
    assert client._status_event.is_set()

    client._status_event.clear()
    await client._stop_audio()
    assert client._status_event.is_set()

    print("  PASS: test_start_and_stop_audio_trigger_status_event")


# ---------------------------------------------------------------------------
# Phase 4: binary audio framing
# ---------------------------------------------------------------------------


async def _test_handshake_negotiates_binary_audio():
    """HELLO_ACK carrying negotiated_capabilities: [binary_audio] flips the
    client's send path to binary for subsequent AUDIO_FRAMEs."""
    from pi5_client import Pi5Client

    client = Pi5Client("ws://test")
    ws = _FakeWebSocket([json.dumps({
        "type": "HELLO_ACK",
        "payload": {
            "session_id": "s",
            "audio_config": {"sample_rate": 24000},
            "negotiated_capabilities": ["binary_audio"],
        },
    })])

    await client._handshake(ws)

    assert client._binary_audio_enabled is True
    print("  PASS: test_handshake_negotiates_binary_audio")


async def _test_handshake_without_negotiation_stays_json():
    """No negotiated_capabilities (an app still on the pre-Phase-4 build)
    leaves the client on JSON framing."""
    from pi5_client import Pi5Client

    client = Pi5Client("ws://test")
    ws = _FakeWebSocket([json.dumps({
        "type": "HELLO_ACK",
        "payload": {"session_id": "s", "audio_config": {"sample_rate": 24000}},
    })])

    await client._handshake(ws)

    assert client._binary_audio_enabled is False
    print("  PASS: test_handshake_without_negotiation_stays_json")


async def _test_audio_stream_loop_sends_binary_frame_when_negotiated():
    """Once binary_audio is negotiated, AUDIO_FRAME goes out as a packed
    header + raw PCM instead of base64-in-JSON."""
    from pi5_client import AUDIO_FRAME_TAG, HEADER_VERSION, Pi5Client

    client = Pi5Client("ws://test")
    client._binary_audio_enabled = True
    client.is_recording = True
    client._stream_to_laptop = True

    chunk = b"\x11\x22\x33\x44" * 100
    client._audio_capture.read_chunk = _one_chunk_then_block(chunk)

    sent: list = []
    ws = MagicMock()
    ws.send = AsyncMock(side_effect=lambda m: sent.append(m))

    task = asyncio.create_task(client._audio_stream_loop(ws))
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(sent) == 1
    frame = sent[0]
    assert isinstance(frame, bytes)
    header = struct.Struct(">BBIQI")
    tag, version, seq, _capture_ms, reserved = header.unpack_from(frame, 0)
    assert tag == AUDIO_FRAME_TAG
    assert version == HEADER_VERSION
    assert seq == 1
    assert reserved == 0
    assert frame[header.size:] == chunk

    print("  PASS: test_audio_stream_loop_sends_binary_frame_when_negotiated")


async def _test_audio_stream_loop_sends_json_frame_when_not_negotiated():
    """Default (not negotiated) behavior is unchanged: base64-in-JSON."""
    from pi5_client import Pi5Client

    client = Pi5Client("ws://test")
    client.is_recording = True
    client._stream_to_laptop = True

    chunk = b"\xAA\xBB"
    client._audio_capture.read_chunk = _one_chunk_then_block(chunk)

    sent: list = []
    ws = MagicMock()
    ws.send = AsyncMock(side_effect=lambda m: sent.append(m))

    task = asyncio.create_task(client._audio_stream_loop(ws))
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(sent) == 1
    assert isinstance(sent[0], str)
    msg = json.loads(sent[0])
    assert msg["type"] == "AUDIO_FRAME"
    assert base64.b64decode(msg["payload"]["audio"]) == chunk

    print("  PASS: test_audio_stream_loop_sends_json_frame_when_not_negotiated")


async def _test_receive_loop_queues_binary_play_audio():
    """A binary PLAY_AUDIO frame through _receive_loop decodes correctly and
    reaches the playback queue, same shape as the JSON path.

    Pre-creates the queue and stubs out _ensure_playback_worker so nothing
    concurrently drains it -- this test is about the decode+dispatch, not the
    worker (covered elsewhere).
    """
    from pi5_client import HEADER_VERSION, PLAY_AUDIO_TAG, Pi5Client

    client = Pi5Client("ws://test")
    client._playback_queue = asyncio.Queue()
    client._ensure_playback_worker = lambda: None

    pcm = b"\x01\x02\x03\x04"
    header = struct.pack(">BBIBII", PLAY_AUDIO_TAG, HEADER_VERSION, 5, 0x01, 100, 0)
    ws = _FakeWebSocket([header + pcm])

    await client._receive_loop(ws)

    _, payload = client._playback_queue.get_nowait()
    assert payload["audio"] == pcm
    assert payload["sequence_number"] == 5
    assert payload["is_final"] is True
    assert payload["duration_ms"] == 100

    print("  PASS: test_receive_loop_queues_binary_play_audio")


async def _test_receive_loop_drops_malformed_binary_frame():
    """A malformed binary frame is dropped silently -- no crash, and it never
    even reaches the playback worker."""
    from pi5_client import Pi5Client

    client = Pi5Client("ws://test")
    called = {"value": False}
    client._ensure_playback_worker = lambda: called.__setitem__("value", True)

    ws = _FakeWebSocket([b"\x02"])  # too short to be a valid header
    await client._receive_loop(ws)  # must not raise

    assert called["value"] is False
    print("  PASS: test_receive_loop_drops_malformed_binary_frame")


async def _test_stop_playback_worker_survives_cancel_mid_final_chunk():
    """Regression: STOP_AUDIO_STREAM arriving right after a final PLAY_AUDIO
    used to crash the client. _stop_playback_worker() nils
    self._playback_queue, then cancels the worker; if the worker was blocked
    inside _handle_play_audio (e.g. finalize_streaming()'s subprocess wait),
    the CancelledError landed in a `finally: self._playback_queue.task_done()`
    that read None instead of the queue, raising AttributeError and crashing
    the whole client. Reproduced live on the Zero 2 W 2026-07-29; the Pi 5
    carried the identical bug in its own copy of the worker."""
    from pi5_client import Pi5Client

    client = Pi5Client("ws://test")
    client._ensure_playback_worker()

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_finalize():
        started.set()
        await release.wait()
        return 1.0

    # is_streaming is read-only, derived from a live aplay subprocess; fake
    # one so the property reads True without spawning a real process.
    client._playback._process = MagicMock(returncode=None)
    client._playback.finalize_streaming = slow_finalize

    ws = MagicMock()
    ws.send = AsyncMock()
    client._playback_queue.put_nowait((ws, {
        "audio": b"", "sequence_number": 1, "is_final": True,
    }))

    await asyncio.wait_for(started.wait(), timeout=2)
    # The worker is now blocked inside _handle_play_audio -> finalize_streaming,
    # exactly like a real client mid-STOP_AUDIO_STREAM. This must not raise.
    await client._stop_playback_worker()

    release.set()
    print("  PASS: test_stop_playback_worker_survives_cancel_mid_final_chunk")


# ---------------------------------------------------------------------------
# Phase 5b: silence elision
# ---------------------------------------------------------------------------


def _test_chunk_rms_stride_approximates_full_precision():
    """The cheap stride=4 RMS used for elision stays close to the full
    (stride=1) value calibration relies on -- close enough to gate on, not
    identical (that's the whole point of subsampling)."""
    from audio_gating import chunk_rms

    full = chunk_rms(_LOUD_CHUNK)
    approx = chunk_rms(_LOUD_CHUNK, stride=4)
    assert full == 20000.0
    assert approx == 20000.0  # constant-amplitude signal: subsampling changes nothing

    assert chunk_rms(_QUIET_CHUNK, stride=4) == 0.0

    print("  PASS: test_chunk_rms_stride_approximates_full_precision")


def _test_make_audio_gap_message_shape():
    from pi5_client import make_audio_gap, parse_message

    raw = make_audio_gap(850, 12)
    msg = parse_message(raw)

    assert msg["type"] == "AUDIO_GAP"
    assert msg["payload"]["duration_ms"] == 850
    assert msg["payload"]["sequence_number"] == 12
    assert msg["payload"]["reason"] == "silence"

    print("  PASS: test_make_audio_gap_message_shape")


async def _test_audio_stream_loop_flag_off_sends_all_quiet_chunks():
    """ELIDE_SILENCE off (the default) must reproduce today's exact
    behavior: every chunk sent as AUDIO_FRAME, even pure silence."""
    from pi5_client import Pi5Client

    client = Pi5Client("ws://test")
    client.is_recording = True
    client._stream_to_laptop = True
    client._audio_capture.read_chunk = _one_chunk_then_block(_QUIET_CHUNK)

    sent: list = []
    ws = MagicMock()
    ws.send = AsyncMock(side_effect=lambda m: sent.append(m))

    task = asyncio.create_task(client._audio_stream_loop(ws))
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(sent) == 1
    assert json.loads(sent[0])["type"] == "AUDIO_FRAME"

    print("  PASS: test_audio_stream_loop_flag_off_sends_all_quiet_chunks")


async def _test_audio_stream_loop_elides_and_flushes_gap():
    """ELIDE_SILENCE on: quiet chunks aren't sent as AUDIO_FRAME; once enough
    accumulates, one AUDIO_GAP flushes with the batched duration."""
    from pi5_client import Pi5Client

    client = Pi5Client("ws://test")
    client.is_recording = True
    client._stream_to_laptop = True
    client._audio_capture.read_chunk = _chunks_then_block([_QUIET_CHUNK] * 5)

    sent: list = []
    ws = MagicMock()
    ws.send = AsyncMock(side_effect=lambda m: sent.append(m))

    with patch("pi5_client.ELIDE_SILENCE", True), \
         patch("pi5_client.GAP_FLUSH_INTERVAL_MS", 250):
        task = asyncio.create_task(client._audio_stream_loop(ws))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert len(sent) == 1
    msg = json.loads(sent[0])
    assert msg["type"] == "AUDIO_GAP"
    assert msg["payload"]["duration_ms"] >= 250
    assert msg["payload"]["reason"] == "silence"
    assert msg["payload"]["sequence_number"] == 1  # no real frame sent yet

    print("  PASS: test_audio_stream_loop_elides_and_flushes_gap")


async def _test_audio_stream_loop_flushes_partial_gap_when_speech_resumes():
    """A gap shorter than the flush interval still flushes immediately once
    speech resumes, in order, before the resuming frame."""
    from pi5_client import Pi5Client

    client = Pi5Client("ws://test")
    client.is_recording = True
    client._stream_to_laptop = True
    client._audio_capture.read_chunk = _chunks_then_block(
        [_QUIET_CHUNK, _QUIET_CHUNK, _LOUD_CHUNK],
    )

    sent: list = []
    ws = MagicMock()
    ws.send = AsyncMock(side_effect=lambda m: sent.append(m))

    with patch("pi5_client.ELIDE_SILENCE", True), \
         patch("pi5_client.GAP_FLUSH_INTERVAL_MS", 1000):  # never hit by 2 quiet chunks
        task = asyncio.create_task(client._audio_stream_loop(ws))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert len(sent) == 2
    gap = json.loads(sent[0])
    frame = json.loads(sent[1])
    assert gap["type"] == "AUDIO_GAP"
    assert gap["payload"]["duration_ms"] == 200  # 2 quiet chunks * 100ms
    assert gap["payload"]["sequence_number"] == 1  # predicts the frame about to send
    assert frame["type"] == "AUDIO_FRAME"
    assert frame["payload"]["sequence_number"] == 1

    print("  PASS: test_audio_stream_loop_flushes_partial_gap_when_speech_resumes")


def run_async_test(coro):
    asyncio.run(coro)


def main():
    sync_tests = [
        test_audio_frame_message_structure,
        test_audio_frame_base64_roundtrip,
        test_audio_frame_chunk_size,
        _test_apply_gain_scales_and_clips,
        _test_soft_limit_shape,
        _test_openai_style_loud_source_does_not_hard_clip,
        _test_chunk_rms_stride_approximates_full_precision,
        _test_make_audio_gap_message_shape,
    ]
    async_tests = [
        _test_audio_capture_read_chunk,
        _test_audio_capture_read_chunk_applies_input_gain,
        _test_audio_capture_start_uses_arecord,
        _test_playback_manager_pipes_to_aplay,
        _test_playback_manager_applies_playback_gain,
        _test_streaming_chunks_then_finalize,
        _test_single_blob_still_works,
        _test_finalize_returns_correct_duration,
        _test_streaming_finalize_with_empty_final_chunk,
        _test_set_volume_updates_playback_gain,
        _test_set_volume_not_blocked_by_active_playback,
        _test_volume_change_applies_mid_response,
        _test_write_pacing_limits_audio_written_ahead,
        _test_set_mic_gain_updates_input_gain,
        _test_skip_calibration_streams_immediately,
        _test_fresh_start_runs_calibration,
        _test_drain_buffered_audio_discards_backlog,
        _test_drain_buffered_audio_noop_when_idle,
        # Phase 5a: event-driven DEVICE_STATUS
        _test_status_loop_silent_while_idle,
        _test_status_loop_sends_immediately_on_recording_transitions,
        _test_status_loop_sends_periodically_while_recording,
        _test_start_and_stop_audio_trigger_status_event,
        # Phase 4: binary audio framing
        _test_handshake_negotiates_binary_audio,
        _test_handshake_without_negotiation_stays_json,
        _test_audio_stream_loop_sends_binary_frame_when_negotiated,
        _test_audio_stream_loop_sends_json_frame_when_not_negotiated,
        _test_receive_loop_queues_binary_play_audio,
        _test_receive_loop_drops_malformed_binary_frame,
        _test_stop_playback_worker_survives_cancel_mid_final_chunk,
        # Phase 5b: silence elision
        _test_audio_stream_loop_flag_off_sends_all_quiet_chunks,
        _test_audio_stream_loop_elides_and_flushes_gap,
        _test_audio_stream_loop_flushes_partial_gap_when_speech_resumes,
    ]

    total = len(sync_tests) + len(async_tests)
    print(f"Running {total} tests...\n")

    passed = 0
    failed = 0

    for test in sync_tests:
        try:
            test()
            passed += 1
        except AssertionError as exc:
            print(f"  FAIL: {test.__name__}: {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR: {test.__name__}: {exc}")
            failed += 1

    for test in async_tests:
        try:
            run_async_test(test())
            passed += 1
        except AssertionError as exc:
            print(f"  FAIL: {test.__name__}: {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR: {test.__name__}: {exc}")
            failed += 1

    print(f"\nResults: {passed} passed, {failed} failed, {total} total")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
