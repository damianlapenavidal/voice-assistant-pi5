"""Voice assistant: USB mic -> OpenAI Realtime API -> USB speaker."""

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

import websockets
from dotenv import load_dotenv

SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from assistant_audio import (
    CHUNK_BYTES,
    CHUNK_SEC,
    CHANNELS,
    CalibrationPhase,
    MicMode,
    PlaybackManager,
    SAMPLE_FORMAT,
    SAMPLE_RATE,
    AssistantState,
    audio_duration_sec,
    chunk_rms,
)
from assistant_ui import TerminalUI
from check_audio import get_audio_devices

DEFAULT_MODEL = "gpt-realtime-mini"
REALTIME_URL = "wss://api.openai.com/v1/realtime?model={model}"
IGNORABLE_API_ERRORS = {"response_cancel_not_active"}
SKIP_LOG_EVENTS = {
    "response.output_audio.delta", "response.output_audio_transcript.delta",
    "conversation.item.input_audio_transcription.delta", "input_audio_buffer.cleared",
    "rate_limits.updated", "conversation.item.added", "conversation.item.done",
    "response.output_item.added", "response.content_part.added", "response.output_audio.done",
    "response.output_audio_transcript.done", "response.content_part.done", "response.output_item.done",
}
TRANSCRIPT_EVENTS = {
    "conversation.item.input_audio_transcription.completed",
    "conversation.item.input_audio_transcription.done",
}
AUDIO_DELTA_EVENTS = {"response.output_audio.delta", "response.audio.delta"}
ASSISTANT_MODES = (MicMode.ASSISTANT_SPEAKING, MicMode.PLAYING)


def load_config():
    load_dotenv(SRC_DIR.parent / ".env")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY not found in .env")
        sys.exit(1)
    input_device, output_device = get_audio_devices()
    if not input_device or not output_device:
        print("ERROR: Microphone or speaker not found. Run: python src/check_audio.py")
        sys.exit(1)
    return {
        "api_key": api_key,
        "model": os.getenv("REALTIME_MODEL", DEFAULT_MODEL),
        "voice": os.getenv("REALTIME_VOICE", "alloy"),
        "input_device": input_device,
        "output_device": output_device,
        "debug": os.getenv("VOICE_DEBUG", "").lower() in {"1", "true", "yes"},
        "calibration_quiet_sec": float(os.getenv("CALIBRATION_QUIET_SEC", "1.0")),
        "calibration_speak_sec": float(os.getenv("CALIBRATION_SPEAK_SEC", "3.5")),
    }


def extract_user_transcript(event):
    if event.get("transcript"):
        return event["transcript"]
    item = event.get("item", {})
    if item.get("role") != "user":
        return None
    for content in item.get("content", []):
        if content.get("transcript"):
            return content["transcript"]
        if content.get("text"):
            return content["text"]
    return None


def likely_echo_transcript(heard, assistant_text):
    if not heard or not assistant_text:
        return False
    heard_words = {w for w in heard.lower().split() if len(w) > 3}
    if len(heard_words) < 2:
        return False
    assistant_words = {w for w in assistant_text.lower().split() if len(w) > 3}
    overlap = heard_words & assistant_words
    return len(overlap) >= 2 and len(overlap) / len(heard_words) >= 0.70


async def ws_send(ws, payload):
    await ws.send(json.dumps(payload))


async def wait_for_event(ws, expected_type, timeout=30):
    while True:
        event = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if event.get("type") == "error":
            print("ERROR:", json.dumps(event, indent=2))
            sys.exit(1)
        if event.get("type") == expected_type:
            return event


async def configure_session(ws, voice):
    await ws_send(ws, {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": (
                "You are a helpful voice assistant on a Raspberry Pi. "
                "Keep replies clear and appropriately brief."
            ),
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                    "transcription": {"model": "gpt-4o-mini-transcribe"},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.6,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 700,
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                    "voice": voice,
                },
            },
        },
    })
    await wait_for_event(ws, "session.updated")


async def finish_listening(ws, state):
    env = state.env
    await ws_send(ws, {"type": "input_audio_buffer.clear"})
    if state.debug and env.mode == MicMode.RECOVERING:
        elapsed = time.monotonic() - env.recovery_started_at
        state.ui.debug(
            f"recovery done in {elapsed:.1f}s "
            f"(peak rms={env.recovery_peak:.0f}, quiet<{env.recovery_quiet_limit():.0f})"
        )
    env.mode = MicMode.LISTENING
    env.quiet_streak = env.local_silence_streak = 0
    env.local_speaking = False
    env.turn_had_local_speech = False
    state.user_spoke_this_turn = False
    state.user_transcript_printed = False
    state.responding_pending = False
    state.ui.ready(env.debug_levels() if state.debug else None)


async def block_phantom_turn(ws, state, reason):
    await ws_send(ws, {"type": "input_audio_buffer.clear"})
    state.response_active = False
    state.env.begin_recovery(audio_duration_sec(state.audio_bytes_played))
    state.ui.echo_block(reason)


def _start_recovery(state, played_sec):
    state.assistant_transcript_parts.clear()
    state.audio_bytes_received = 0
    state.env.begin_recovery(played_sec)
    state.ui.waiting(played_sec, state.env.recovery_min_sec)


def _log_playback(state, received_sec):
    played_sec = audio_duration_sec(state.audio_bytes_played)
    if received_sec > 0 and played_sec < received_sec - 0.4:
        state.ui.warn(f"Playback shorter than received ({played_sec:.1f}s vs {received_sec:.1f}s)")
    if state.debug or received_sec > 0:
        state.ui.debug(
            f"audio received {received_sec:.2f}s, played {played_sec:.2f}s | {state.env.debug_levels()}"
        )


async def complete_turn(ws, state, playback):
    received_sec = audio_duration_sec(state.audio_bytes_received)
    state.response_active = False
    state.current_response_id = None
    state.responding_pending = False
    state.turn_received_sec = received_sec

    transcript = "".join(state.assistant_transcript_parts)
    if transcript and state.audio_bytes_received == 0:
        state.ui.warn("Transcript without audio — check session settings.")
    if transcript:
        state.ui.assistant_said(transcript)
        state.env.last_assistant_transcript = transcript

    if state.response_pcm:
        pcm = bytes(state.response_pcm)
        state.response_pcm.clear()
        state.env.mode = MicMode.PLAYING

        async def done():
            await after_playback(state)

        await playback.start_playback(pcm, done)
    else:
        _log_playback(state, received_sec)
        _start_recovery(state, received_sec)


async def after_playback(state):
    _log_playback(state, state.turn_received_sec)
    _start_recovery(state, max(audio_duration_sec(state.audio_bytes_played), state.turn_received_sec))


async def run_calibration_chunk(ws, state, level, config):
    env, ui = state.env, state.ui
    if env.calibration_phase == CalibrationPhase.QUIET:
        env.observe_quiet(level)
        env.calibration_chunks_left -= 1
        if env.calibration_chunks_left <= 0:
            env.calibration_phase = CalibrationPhase.SPEAK
            env.calibration_chunks_left = max(1, int(config["calibration_speak_sec"] / CHUNK_SEC))
            ui.calibrate_speak()
        return

    env.calibration_samples.append(level)
    speech_floor = env.noise_floor + max(80.0, env.speech_margin * 0.2)
    if level >= speech_floor:
        env.calibration_speech_seen = True
        env.calibration_silence_streak = 0
    elif env.calibration_speech_seen:
        env.calibration_silence_streak += 1

    env.calibration_chunks_left -= 1
    if env.calibration_chunks_left > 0 and not (
        env.calibration_speech_seen and env.calibration_silence_streak >= 8
    ):
        return

    if env.calibration_speech_seen and env.calibration_samples:
        ranked = sorted(env.calibration_samples)
        peak = ranked[len(ranked) * 3 // 4]
    else:
        peak = env.noise_floor + 200
    env.finish_calibration(peak)
    ui.calibrate_done(env.noise_floor, env.user_speech_peak)
    await finish_listening(ws, state)


async def stream_microphone(ws, arecord, state, config):
    env = state.env
    if env.mode == MicMode.CALIBRATING:
        state.ui.calibrate_quiet()
        env.calibration_chunks_left = max(1, int(config["calibration_quiet_sec"] / CHUNK_SEC))

    while True:
        chunk = await arecord.stdout.read(CHUNK_BYTES)
        if not chunk:
            break
        level = chunk_rms(chunk)

        if env.mode == MicMode.CALIBRATING:
            await run_calibration_chunk(ws, state, level, config)
            continue

        if env.mode == MicMode.RECOVERING:
            env.observe_recovery(level)
            elapsed = time.monotonic() - env.recovery_started_at
            if elapsed < env.recovery_min_sec:
                continue
            if level < env.recovery_quiet_limit():
                env.quiet_streak += 1
            else:
                env.quiet_streak = 0
                if state.debug and time.monotonic() - env._recovery_log_at >= 2.0:
                    env._recovery_log_at = time.monotonic()
                    state.ui.debug(
                        f"recovery rms={level:.0f} > quiet<{env.recovery_quiet_limit():.0f} "
                        f"(peak={env.recovery_peak:.0f})"
                    )
            if env.quiet_streak >= env.recovery_quiet_chunks_needed or elapsed >= env.recovery_max_sec:
                if elapsed >= env.recovery_max_sec and state.debug:
                    state.ui.debug("recovery timed out — opening mic anyway")
                await finish_listening(ws, state)
            continue

        if env.mode in ASSISTANT_MODES:
            if env.mode == MicMode.ASSISTANT_SPEAKING:
                env.observe_quiet(level)
            continue

        if env.mode == MicMode.LISTENING:
            env.note_local_speech(level)
            if not env.local_speaking:
                env.observe_quiet(level)
                if level >= env.speech_start_threshold():
                    env.local_speaking = True
                    env.local_silence_streak = 0
            elif level < env.speech_stop_threshold():
                env.local_silence_streak += 1
                if env.local_silence_streak >= 4:
                    env.local_speaking = False
            else:
                env.local_silence_streak = 0
            await ws_send(ws, {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(chunk).decode("ascii"),
            })


async def handle_server_events(ws, state, playback):
    env, ui = state.env, state.ui
    try:
        while True:
            event = json.loads(await ws.recv())
            event_type = event.get("type", "")
            if event_type not in SKIP_LOG_EVENTS:
                ui.server_event(event_type)

            if event_type == "error":
                err = event.get("error", {})
                if err.get("code") in IGNORABLE_API_ERRORS:
                    ui.api_notice(err.get("message", err.get("code")))
                else:
                    ui.error(event)
                continue

            if event_type == "input_audio_buffer.speech_started":
                if env.mode in ASSISTANT_MODES:
                    ui.debug("ignored server speech_started during assistant turn")
                elif env.mode == MicMode.LISTENING:
                    if env.turn_had_local_speech:
                        ui.user_speaking()
                        state.assistant_transcript_parts.clear()
                        state.user_transcript_printed = False
                        state.responding_pending = False
                    else:
                        await block_phantom_turn(ws, state, "no local speech")

            elif event_type == "input_audio_buffer.speech_stopped":
                if env.mode == MicMode.LISTENING and env.turn_had_local_speech:
                    state.user_spoke_this_turn = True
                    ui.user_finished()

            elif event_type in TRANSCRIPT_EVENTS:
                heard = extract_user_transcript(event)
                if not heard:
                    continue
                if state.user_spoke_this_turn:
                    ui.user_heard(heard)
                    state.user_spoke_this_turn = False
                    state.user_transcript_printed = True
                    if state.responding_pending and state.response_active:
                        ui.assistant_responding()
                        state.responding_pending = False
                elif likely_echo_transcript(heard, env.last_assistant_transcript):
                    await ws_send(ws, {"type": "input_audio_buffer.clear"})
                    if env.mode == MicMode.LISTENING:
                        await block_phantom_turn(ws, state, "transcript matches assistant")
                    ui.user_heard(heard, "echo — ignored")
                else:
                    ui.user_heard(heard, "ignored — no local speech")

            elif event_type == "response.created":
                if not env.turn_had_local_speech and env.mode not in ASSISTANT_MODES:
                    ui.echo_cancel()
                    await ws_send(ws, {"type": "response.cancel"})
                    await block_phantom_turn(ws, state, "phantom response")
                    continue
                state.current_response_id = event.get("response", {}).get("id")
                state.response_active = True
                env.mode = MicMode.ASSISTANT_SPEAKING
                state.assistant_transcript_parts.clear()
                state.audio_bytes_received = state.audio_bytes_played = 0
                state.response_pcm.clear()
                if state.user_transcript_printed:
                    ui.assistant_responding()
                    state.responding_pending = False
                else:
                    state.responding_pending = True

            elif event_type in AUDIO_DELTA_EVENTS:
                rid = event.get("response_id")
                if state.current_response_id and rid and rid != state.current_response_id:
                    continue
                chunk = base64.b64decode(event.get("delta") or event.get("audio", ""))
                if chunk:
                    state.audio_bytes_received += len(chunk)
                    state.response_pcm.extend(chunk)

            elif event_type in {"response.output_audio_transcript.delta", "response.output_text.delta"}:
                state.assistant_transcript_parts.append(event.get("delta", ""))

            elif event_type == "response.cancelled":
                ui.assistant_cancelled()
                state.response_pcm.clear()
                await playback.stop()
                await complete_turn(ws, state, playback)

            elif event_type == "response.done":
                await complete_turn(ws, state, playback)
    except websockets.ConnectionClosed:
        return


async def run_assistant(config):
    url = REALTIME_URL.format(model=config["model"])
    headers = {"Authorization": f"Bearer {config['api_key']}"}
    ui = TerminalUI(debug=config["debug"])
    state = AssistantState(debug=config["debug"], ui=ui)
    playback = PlaybackManager(config["output_device"], state)

    for line in (
        f"Mic:     {config['input_device']}",
        f"Speaker: {config['output_device']}",
        f"Model:   {config['model']}",
        f"Voice:   {config['voice']}",
        *(("Debug:   on (thresholds adapt automatically)",) if config["debug"] else ()),
    ):
        ui.startup(line)
    ui.divider("Connecting")
    print("Connecting to Realtime API...")

    arecord = await asyncio.create_subprocess_exec(
        "arecord", "-D", config["input_device"],
        "-f", SAMPLE_FORMAT, "-r", str(SAMPLE_RATE), "-c", str(CHANNELS),
        "-t", "raw", "-q",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )

    try:
        async with websockets.connect(url, additional_headers=headers) as ws:
            ui.divider("Live")
            print("WebSocket connected.")
            await wait_for_event(ws, "session.created")
            await configure_session(ws, config["voice"])
            await ws_send(ws, {"type": "input_audio_buffer.clear"})
            tasks = [
                asyncio.create_task(stream_microphone(ws, arecord, state, config)),
                asyncio.create_task(handle_server_events(ws, state, playback)),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                if task.exception():
                    raise task.exception()
    finally:
        await playback.stop()
        if arecord.returncode is None:
            arecord.terminate()
            await arecord.wait()


def main():
    try:
        asyncio.run(run_assistant(load_config()))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
