"""Audio I/O, mic gating, and playback for the voice assistant."""

import asyncio
import struct
import time
from enum import Enum

SAMPLE_RATE = 24000
CHANNELS = 1
BYTES_PER_SAMPLE = 2
SAMPLE_FORMAT = "S16_LE"
CHUNK_MS = 100
CHUNK_SEC = CHUNK_MS / 1000
CHUNK_BYTES = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE * CHUNK_MS // 1000
BYTE_RATE = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE


class MicMode(Enum):
  CALIBRATING = "calibrating"
  LISTENING = "listening"
  ASSISTANT_SPEAKING = "assistant"
  PLAYING = "playing"
  RECOVERING = "recovering"


class CalibrationPhase(Enum):
  QUIET = "quiet"
  SPEAK = "speak"


class MicEnvironment:
  def __init__(self):
    self.mode = MicMode.CALIBRATING
    self.calibration_phase = CalibrationPhase.QUIET
    self.noise_floor = 400.0
    self.user_speech_peak = 0.0
    self.speech_margin = 350.0
    self.echo_level = 0.0
    self.recovery_peak = 0.0
    self.quiet_streak = 0
    self.local_speaking = False
    self.local_silence_streak = 0
    self.turn_had_local_speech = False
    self.recovery_started_at = 0.0
    self.recovery_min_sec = 0.3
    self.recovery_max_sec = 4.0
    self.recovery_quiet_chunks_needed = 8
    self.last_assistant_transcript = ""
    self._recovery_log_at = 0.0
    self.calibration_samples = []
    self.calibration_speech_seen = False
    self.calibration_silence_streak = 0

  def observe_quiet(self, rms):
    if rms < self.noise_floor + self.speech_margin * 0.25:
      self.noise_floor = 0.9 * self.noise_floor + 0.1 * rms

  def observe_recovery(self, rms):
    self.recovery_peak = max(self.recovery_peak, rms)
    if rms < self.speech_start_threshold():
      self.noise_floor = 0.95 * self.noise_floor + 0.05 * rms
      if self.echo_level < rms:
        self.echo_level = 0.92 * self.echo_level + 0.08 * rms

  def speech_start_threshold(self):
    base = self.noise_floor + self.speech_margin * 0.32
    echo_margin = max(0.0, self.echo_level - self.noise_floor)
    return max(base, self.noise_floor + echo_margin * 0.55) if echo_margin > 0 else base

  def speech_stop_threshold(self):
    span = self.speech_start_threshold() - self.noise_floor
    return self.noise_floor + max(self.speech_margin * 0.18, span * 0.45)

  def recovery_quiet_limit(self):
    return self.noise_floor + max(120.0, self.speech_margin * 0.5)

  def finish_calibration(self, speech_peak):
    self.user_speech_peak = max(speech_peak, self.noise_floor + 250.0)
    self.user_speech_peak = min(self.user_speech_peak, self.noise_floor + 1200.0)
    self.speech_margin = self.user_speech_peak - self.noise_floor

  def begin_recovery(self, played_sec):
    self.mode = MicMode.RECOVERING
    self.recovery_started_at = time.monotonic()
    self.recovery_peak = 0.0
    self.quiet_streak = 0
    self.local_speaking = False
    self.local_silence_streak = 0
    self.turn_had_local_speech = False
    self.recovery_min_sec = min(2.0, max(0.4, played_sec * 0.10 + 0.3))
    self.recovery_max_sec = self.recovery_min_sec + 4.0
    self.recovery_quiet_chunks_needed = max(5, min(20, int(played_sec * 1.2) + 5))
    self._recovery_log_at = 0.0

  def note_local_speech(self, rms):
    if rms >= self.speech_start_threshold():
      self.turn_had_local_speech = True

  def debug_levels(self):
    return (
      f"noise={self.noise_floor:.0f} voice≈{self.user_speech_peak:.0f} "
      f"speak>{self.speech_start_threshold():.0f}"
    )


class AssistantState:
  def __init__(self, debug=False, ui=None):
    from assistant_ui import TerminalUI

    self.debug = debug
    self.ui = ui or TerminalUI(debug=debug)
    self.env = MicEnvironment()
    self.response_active = False
    self.current_response_id = None
    self.assistant_transcript_parts = []
    self.audio_bytes_received = 0
    self.audio_bytes_played = 0
    self.user_spoke_this_turn = False
    self.user_transcript_printed = False
    self.responding_pending = False
    self.turn_received_sec = 0.0
    self.response_pcm = bytearray()


def audio_duration_sec(num_bytes):
  return num_bytes / BYTE_RATE


def chunk_rms(chunk):
  if len(chunk) < BYTES_PER_SAMPLE:
    return 0.0
  count = len(chunk) // BYTES_PER_SAMPLE
  samples = struct.unpack(f"<{count}h", chunk[: count * BYTES_PER_SAMPLE])
  if not samples:
    return 0.0
  return (sum(s * s for s in samples) / len(samples)) ** 0.5


class PlaybackManager:
  BUFFER_US = 300000

  def __init__(self, output_device, state):
    self.output_device = output_device
    self.state = state
    self.process = None
    self._feeder = None
    self._stop = False

  async def start_playback(self, pcm, on_done=None):
    self._stop = False
    self.state.audio_bytes_played = 0
    self.process = await asyncio.create_subprocess_exec(
      "aplay", "-D", self.output_device,
      "-f", SAMPLE_FORMAT, "-r", str(SAMPLE_RATE), "-c", str(CHANNELS),
      "-t", "raw", "-q", f"--buffer-time={self.BUFFER_US}",
      stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    self._feeder = asyncio.create_task(self._feed(pcm, on_done))

  async def _feed(self, pcm, on_done):
    proc = self.process
    try:
      for i in range(0, len(pcm), CHUNK_BYTES):
        if self._stop or proc.returncode is not None:
          break
        part = pcm[i:i + CHUNK_BYTES]
        proc.stdin.write(part)
        await proc.stdin.drain()
        self.state.audio_bytes_played += len(part)
      if not self._stop and proc.stdin and not proc.stdin.is_closing():
        proc.stdin.close()
        await proc.wait()
    except (BrokenPipeError, ConnectionResetError, asyncio.CancelledError):
      pass
    if proc.returncode not in (0, None, -15):
      self.state.ui.warn(f"Speaker playback failed (code {proc.returncode}).")
    self.process = self._feeder = None
    if on_done and not self._stop:
      await on_done()

  async def stop(self):
    self._stop = True
    if self._feeder and not self._feeder.done():
      self._feeder.cancel()
      try:
        await self._feeder
      except asyncio.CancelledError:
        pass
    if self.process and self.process.returncode is None:
      self.process.terminate()
      try:
        await asyncio.wait_for(self.process.wait(), timeout=1)
      except asyncio.TimeoutError:
        self.process.kill()
        await self.process.wait()
    self.process = self._feeder = None
