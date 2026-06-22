# Voice Assistant (Raspberry Pi 5)

A voice assistant project for Raspberry Pi 5. The long-term goal is to stream live microphone audio to the OpenAI Realtime API and play responses through a speaker.

## Current stage

**Pre-hardware prep** — foundation, API checks, Realtime WebSocket text test, and audio smoke-test scripts are ready. No live mic streaming yet.

## Project structure

```
voice-assistant/
├── src/
│   ├── main.py              # Startup sanity test + .env load check
│   ├── verify_api.py        # Verify OpenAI API key works (REST)
│   ├── realtime_connect.py  # Realtime API WebSocket text test (no mic)
│   └── check_audio.py       # List mic/speaker + record/playback test
├── .env                 # Your API key (local only, not in Git)
├── .env.example         # Template for required env vars
├── README.md
├── requirements.txt
└── .gitignore
```

## Setup

```bash
cd ~/voice-assistant
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and add your real `OPENAI_API_KEY` if you have not already.

## Run the checks

**1. Sanity test (always run this first):**
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

**4. Audio hardware test (run when mic/speaker are plugged in):**
```bash
python src/check_audio.py
```

Without hardware, step 4 will list devices and tell you to plug in a mic. When hardware is ready, it records 3 seconds and plays them back.

## Tomorrow quick start (when hardware arrives)

1. Plug in USB mic and speaker (or USB audio dongle with both).
2. `python src/check_audio.py` — confirm record/playback works.
3. Next step: stream mic audio into `realtime_connect.py` (we will wire that up together).
