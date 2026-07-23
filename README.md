# Voice Assistant (Raspberry Pi 5) — Thin Client

A lightweight WebSocket client that runs on the Raspberry Pi 5 and connects to
`voice-assistant-app` on your laptop. The Pi is a **thin client**: it captures
microphone audio, plays back AI-generated speech relayed from the laptop, runs
mic calibration/gating, and reports device status. No API keys and no Realtime
logic live on the device — that intelligence stays on the laptop app.

## How it fits in the system

```
┌─────────────┐         WebSocket (Wi-Fi)          ┌──────────────────────┐
│  Raspberry  │ ───────────────────────────────────▶│  voice-assistant-app │
│    Pi 5     │◀─────────────────────────────────── │      (Laptop)        │
│ (this repo) │   HELLO, DEVICE_STATUS,              │                      │
│             │   AUDIO_FRAME, PLAY_AUDIO            │  → OpenAI Realtime   │
└─────────────┘                                      └──────────────────────┘
```

The Pi connects **as a client** to the laptop's WebSocket server. The laptop
owns all intelligence: API keys, session management, parent controls. This
device reports `device_type: "pi5"`.

## Project structure

```
pi5_client.py          # entrypoint: WebSocket client, handshake, main loop
audio_capture.py        # arecord-backed mic capture
audio_playback.py       # aplay-backed speaker playback
audio_gating.py         # calibration thresholds, echo/mic gating
calibration_prompt.py   # "say hello to start" prompt playback
assets/                 # calibration prompt audio asset
test_client.py           # protocol/message unit tests (no hardware)
test_audio.py             # audio pipeline unit tests (no hardware)
requirements.txt
.env.example
speaker_mic_set_up.md    # USB mic/speaker ALSA card numbers and volume notes
```

## Audio hardware configuration

Audio capture and playback use `arecord` and `aplay` (from `alsa-utils`) at
**24 kHz, PCM16, mono**.

Optional environment variables select ALSA devices:

| Variable | Example | Description |
|----------|---------|-------------|
| `AUDIO_INPUT_DEVICE` | `plughw:2,0` | Microphone device for `arecord` |
| `AUDIO_OUTPUT_DEVICE` | `plughw:3,0` | Speaker device for `aplay` |
| `INPUT_GAIN` | `1.0` | Mic software make-up gain (soft-limited) |
| `PLAYBACK_GAIN` | `1.0` | Speaker gain at connect (before dashboard slider) |

Mic and speaker are often **different ALSA cards** when using separate USB
devices. On this Pi 5, the mic is card 2 and the USB speaker is card 3 — do not
use `plughw:0,0` unless that is your actual speaker (`aplay -l`). See
[speaker_mic_set_up.md](speaker_mic_set_up.md) for card numbers and volume
commands.

`INPUT_GAIN` / `PLAYBACK_GAIN` are steady multipliers applied per chunk (not
AGC). Peaks that would clip are soft-limited. After connect, the laptop
dashboard's **Speaker Volume** / **Mic Gain** sliders update them live via
`SET_VOLUME` / `SET_MIC_GAIN` (0–100% maps to 0–3.0x playback and 0–50x mic).

Copy `.env.example` to `.env`; `pi5_client.py` loads it automatically on
startup.

```bash
arecord -l   # input devices
aplay -l     # output devices
```

Use the `plughw:` prefix for plug-in conversion (recommended). If unset, ALSA
uses the system default device.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # websockets only
cp .env.example .env                   # edit if your card numbers differ
```

## Finding the laptop's IP address

On the laptop running `voice-assistant-app`:

**macOS:** `ipconfig getifaddr en0`
**Linux:** `hostname -I | awk '{print $1}'`

Both devices must be on the same Wi-Fi network.

## Running the client

```bash
python pi5_client.py ws://LAPTOP_IP:8765
python pi5_client.py ws://192.168.1.42:8765 --debug   # verbose
```

On a successful connection:

```
2026-06-30 15:00:00 [INFO] Pi 5 Voice Assistant Client v0.1.0
2026-06-30 15:00:00 [INFO] Device ID: raspberrypi | Platform: Linux-6.1.0-rpi-arm64
2026-06-30 15:00:00 [INFO] Target server: ws://192.168.1.42:8765
2026-06-30 15:00:00 [INFO] Sending HELLO (device_id=raspberrypi)
2026-06-30 15:00:00 [INFO] Handshake complete! session_id=sess_abc123, audio_config={'sample_rate': 24000, 'format': 'pcm16', 'channels': 1}
2026-06-30 15:00:00 [INFO] Entering main loop. Sending status every 10s.
```

## Tests (no hardware required)

Run on any machine:

```bash
python test_client.py
python test_audio.py
```

## End-to-end test

1. Start the laptop app with the WebSocket server: `python -m voice_assistant --web`
2. On the Pi, connect the client: `python pi5_client.py ws://LAPTOP_IP:8765`
3. From the dashboard, click **Start Session** (sends `START_AUDIO_STREAM`)
4. Speak into the Pi microphone — you should hear your voice on the Pi speaker
   after round-trip through the laptop
5. Click **Stop Session** to stop capture

Quick local ALSA sanity check on the Pi (no network):

```bash
arecord -D plughw:2,0 -f S16_LE -r 24000 -c 1 -t raw -d 2 /tmp/test.raw
aplay -D plughw:3,0 -f S16_LE -r 24000 -c 1 -t raw /tmp/test.raw
```

## Troubleshooting

### "Connection refused"
The `voice-assistant-app` is not running on the laptop, or is not listening on
port 8765. Start it first: `python -m voice_assistant` on the laptop.

### "No route to host" / "Network is unreachable"
The Pi and laptop are not on the same network. Check both are on the same
Wi-Fi SSID, and try `ping LAPTOP_IP` from the Pi.

### Timeout waiting for HELLO_ACK
The app is running but may not have a WebSocket server active (e.g. running in
mock mode without the WebSocket transport). Start it without `--mock`.

### "websockets" import error
`pip install websockets>=15.0`. If using a virtual environment, make sure it's
activated.

### Ctrl+C doesn't stop the script
The script handles `KeyboardInterrupt`. If it doesn't respond immediately, it
may be in a backoff sleep — wait for the current sleep to finish or press
Ctrl+C again.

## Relationship to the Pi Zero 2W client

Protocol-identical to `voice-assistant-piZero2W`'s `zero2w_client.py`. The
message schema (`HELLO`, `HELLO_ACK`, `DEVICE_STATUS`, `START/STOP_AUDIO_STREAM`,
`AUDIO_FRAME`, `PLAY_AUDIO`, calibration, `PLAYBACK_COMPLETE`) is unchanged, so
the same app handlers work for both. Only the hardware layer (USB sound cards
vs. GPIO I2S) and `device_type` differ.
