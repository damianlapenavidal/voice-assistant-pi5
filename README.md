# Voice Assistant (Raspberry Pi 5)

Live voice assistant for Raspberry Pi 5: USB microphone → OpenAI Realtime API → USB speaker.

## Current stage

**Working end-to-end on Pi 5.** The main script streams your voice to the Realtime API, plays the assistant’s reply on the speaker, then waits for the room to quiet before listening again.

What works today:

- Live voice conversations with `gpt-realtime-mini` (default) over WebSocket
- USB mic/speaker via ALSA (`arecord` / `aplay`), with auto-detected devices
- Startup calibration (stay quiet, then speak) so speech thresholds fit your room
- Echo protection: mic is ignored while the assistant is speaking and during a short recovery period afterward
- Colored terminal output grouped by turn (`assistant_ui.py`)

Not included (by design):

- **Interrupting the assistant mid-reply** — removed after testing showed speaker echo on this hardware is louder than the user’s voice at the mic, so reliable voice barge-in would need acoustic echo cancellation or different hardware.

See [speaker_mic_set_up.md](speaker_mic_set_up.md) for ALSA card numbers, volume levels, and device overrides.

## Project structure

```
voice-assistant/
├── src/
│   ├── main.py              # Startup sanity test + .env load check
│   ├── verify_api.py        # Verify OpenAI API key (REST)
│   ├── realtime_connect.py  # Realtime API WebSocket text test (no mic)
│   ├── check_audio.py       # List mic/speaker + record/playback test
│   ├── voice_assistant.py   # Main loop: mic → Realtime API → speaker
│   ├── assistant_audio.py   # Mic modes, thresholds, playback (aplay)
│   └── assistant_ui.py      # Colored terminal output
├── speaker_mic_set_up.md    # Pi 5 mic/speaker volume and ALSA notes
├── .env                     # Your API key (local only, not in Git)
├── .env.example             # Template for env vars
├── requirements.txt
└── README.md
```

## Setup

```bash
cd ~/voice-assistant
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set your `OPENAI_API_KEY`.

## Run the checks

**1. Sanity test (run first):**

```bash
python src/main.py
```

**2. Verify API key (REST):**

```bash
python src/verify_api.py
```

**3. Realtime API WebSocket test (text only, no mic):**

```bash
python src/realtime_connect.py
```

**4. Audio hardware test:**

```bash
python src/check_audio.py
```

Lists devices; with hardware connected, records 3 seconds and plays them back.

**5. Live voice assistant:**

```bash
python src/voice_assistant.py
```

On start you’ll be asked to stay quiet, then say a few words for calibration. After `[Ready]`, speak normally. Wait for the full reply and `[Ready]` again before your next turn. Press `Ctrl+C` to quit.

Optional env vars (see `.env.example`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `REALTIME_MODEL` | `gpt-realtime-mini` | Realtime model |
| `REALTIME_VOICE` | `alloy` | Assistant voice |
| `AUDIO_INPUT_DEVICE` | auto | ALSA mic (e.g. `plughw:2,0`) |
| `AUDIO_OUTPUT_DEVICE` | auto | ALSA speaker (e.g. `plughw:3,0`) |
| `VOICE_DEBUG` | off | Threshold and event debug lines |
| `CALIBRATION_QUIET_SEC` | `1.0` | Quiet phase length |
| `CALIBRATION_SPEAK_SEC` | `3.5` | Speak phase max length |

## Quick start with hardware

1. Plug in USB mic and USB speaker.
2. `python src/check_audio.py` — confirm record/playback works.
3. Adjust volume if needed (see [speaker_mic_set_up.md](speaker_mic_set_up.md)).
4. `python src/voice_assistant.py` — talk to the assistant.
