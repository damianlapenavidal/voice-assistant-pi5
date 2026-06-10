# Voice Assistant (Raspberry Pi 5)

A voice assistant project for Raspberry Pi 5. The long-term goal is to stream live microphone audio to the OpenAI Realtime API and play responses through a speaker.

## Current stage

**Foundation setup** — project structure and a basic Python sanity test. No API, microphone, or speaker code yet.

## Project structure

```
voice-assistant/
├── src/
│   └── main.py       # Entry point
├── README.md
├── requirements.txt  # Python dependencies (empty for now)
└── .gitignore
```

## Run the sanity test

```bash
cd ~/voice-assistant
python3 src/main.py
```

You should see three startup messages confirming Python and the project layout work.
