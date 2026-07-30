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
| `ELIDE_SILENCE` | `false` | Elide silent chunks from the stream — see [Wire efficiency](#wire-efficiency) |

Mic and speaker are often **different ALSA cards** when using separate USB
devices. On this Pi 5, the mic is card 2 and the USB speaker is card 3 — do not
use `plughw:0,0` unless that is your actual speaker (`aplay -l`). See
[speaker_mic_set_up.md](speaker_mic_set_up.md) for card numbers and volume
commands.

`INPUT_GAIN` / `PLAYBACK_GAIN` are steady multipliers applied per chunk (not
AGC). Peaks that would clip are soft-limited. After connect, the laptop
dashboard's **Speaker Volume** / **Mic Gain** sliders update them live via
`SET_VOLUME` / `SET_MIC_GAIN`: 0–100% maps to 0–1.0x playback (attenuation
only, so normalized TTS never hits the limiter) and 0–15x mic (the measured
clipping threshold on this capture chain, so the whole slider stays usable).

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
2026-06-30 15:00:00 [INFO] Binary audio frames negotiated with the app
2026-06-30 15:00:00 [INFO] Entering main loop. DEVICE_STATUS on recording changes, every 10s while recording.
```

## Wire efficiency

Three optimizations, all developed and measured on the Pi Zero 2 W first (see
`../voice-assistant-piZero2W/docs/battery-plan.md`) and ported here once proven.
The Pi 5 is mains-powered, so the win here is wire traffic, latency and app-side
work rather than battery life — but the code is the same code.

**Binary audio framing.** `AUDIO_FRAME` and `PLAY_AUDIO` travel as binary
WebSocket frames — a packed header plus raw PCM — instead of base64 inside JSON,
once the `binary_audio` capability is negotiated in `HELLO`/`HELLO_ACK`. Measured
on the Zero: 26.8% wire overhead down to ~0.4%. If the app is on an older build
(or has `BINARY_AUDIO_FRAMES=false` set), negotiation simply fails and both sides
stay on the JSON path with no configuration here. Wire format is documented in
the app's `docs/protocol.md` under *Binary Audio Framing*; the header layout must
stay byte-identical to the Zero 2 W client's, since one app decodes both.

**Event-driven `DEVICE_STATUS`.** Status is sent when `is_recording` changes, and
then every 10 s *while recording only* — not as an unconditional heartbeat. Idle
sends nothing at all. The WebSocket's own ping/pong is what proves liveness.

**Silence elision (`ELIDE_SILENCE=true`, off by default).** Silent chunks are not
sent; long runs of silence collapse into an occasional `AUDIO_GAP` marker
(`{duration_ms, sequence_number, reason}`) that the app expands back into real
synthesized silence before OpenAI's turn detection sees it — so the elision is
invisible downstream. Costs a cheap stride-4 RMS per chunk. Measured on the Zero
in a quiet room: 18 real frames sent over 15 s versus ~150 unelided.

Elided chunks never increment `sequence_number`, so an unexplained jump in
consecutive sequence numbers still means a genuinely dropped frame, while a jump
preceded by an `AUDIO_GAP` is accounted for.

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
`AUDIO_FRAME`, `PLAY_AUDIO`, `AUDIO_GAP`, calibration, `PLAYBACK_COMPLETE`) is
the same on both, so the same app handlers work for either. Both negotiate
`binary_audio` and use byte-identical binary headers.

The two clients are **separate copies, not a shared library**, and they have
diverged where the hardware differs:

| | Pi 5 (this repo) | Pi Zero 2 W |
|---|---|---|
| Audio hardware | USB sound cards (mic card 2, speaker card 3) | GPIO I2S (`sndrpigooglevoi`) |
| Mic/volume gain | Python per-sample multiply with a soft-knee limiter | ALSA `softvol`/`route` plugins driven by `amixer` |
| Volume slider taper | Linear | Square-law (perceptual) |
| Mic gain ceiling | 15x (measured clipping threshold) | 50x (much quieter raw I2S signal) |
| Playback write pacing | Paced sub-chunk writes, so a mid-response `SET_VOLUME` still lands | Not needed — `softvol` sits downstream of `aplay`'s buffer |

The gain-offload work (Phase 2 of the battery plan) is Zero-only so far: it is
tied to an `~/.asoundrc` written for that specific I2S card, and its payoff was
~22% of one core on a single-core-class device. Porting it here would mainly buy
the responsiveness simplification in the last row, and needs the Pi 5 physically
present to write and verify a USB-card `asoundrc`.
