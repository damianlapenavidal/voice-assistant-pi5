"""Microphone capture via arecord subprocess (24 kHz PCM16 mono)."""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
CHANNELS = 1
FORMAT = "S16_LE"
CHUNK_BYTES = 4800  # 100 ms at 24 kHz mono S16_LE


class AudioCaptureError(Exception):
    """Raised when capture cannot start or fails unexpectedly."""


class AudioCapture:
    """Capture raw PCM16 audio using an arecord subprocess."""

    def __init__(self, device: str | None = None):
        self._device = device if device is not None else os.environ.get("AUDIO_INPUT_DEVICE")
        self._process: asyncio.subprocess.Process | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running and self._process is not None

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
        """Read one fixed-size chunk from arecord stdout."""
        if not self.is_running or self._process is None or self._process.stdout is None:
            return None

        if self._process.returncode is not None:
            self._running = False
            return None

        try:
            return await self._process.stdout.readexactly(CHUNK_BYTES)
        except asyncio.IncompleteReadError:
            self._running = False
            return None

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
