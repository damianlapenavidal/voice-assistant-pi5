# Pi 5 Device Client

A lightweight WebSocket client that runs on the Raspberry Pi 5 and connects to the `voice-assistant-app` on your laptop. The Pi acts as a thin client: it reports device status, streams microphone audio, and plays back AI-generated speech relayed from the laptop.

## How It Fits in the System

```
┌─────────────┐         WebSocket (Wi-Fi)         ┌─────────────────────┐
│  Raspberry  │ ──────────────────────────────────▶│  voice-assistant-app │
│    Pi 5     │◀────────────────────────────────── │     (Laptop)         │
│  (this      │   HELLO, DEVICE_STATUS,           │                     │
│   script)   │   AUDIO_FRAME, PLAY_AUDIO         │  → OpenAI Realtime  │
└─────────────┘                                    └─────────────────────┘
```

The Pi connects **as a client** to the laptop's WebSocket server. The laptop owns all intelligence: API keys, session management, parent controls.

## Audio Hardware Configuration

Audio capture and playback use `arecord` and `aplay` (from `alsa-utils`) at **24 kHz, PCM16, mono**.

Optional environment variables select ALSA devices:

| Variable | Example | Description |
|----------|---------|-------------|
| `AUDIO_INPUT_DEVICE` | `plughw:2,0` | Microphone device for `arecord` |
| `AUDIO_OUTPUT_DEVICE` | `plughw:3,0` | Speaker device for `aplay` |

Mic and speaker are often **different ALSA cards** when using separate USB devices. On `voice-assistant-pi5`, the mic is card 2 and the USB speaker (0x1908:0x1331) is card 3 — do not use `plughw:0,0` unless that is your actual speaker (`aplay -l`).

Copy `.env.example` to `.env` in this folder on the Pi; `pi5_client.py` loads it automatically on startup.

List available devices on the Pi:

```bash
arecord -l   # input devices
aplay -l     # output devices
```

Use the `plughw:` prefix for plug-in conversion (recommended). If unset, ALSA uses the system default device.

Example:

```bash
cp .env.example .env   # edit if your card numbers differ
python pi5_client.py ws://192.168.1.42:8765
```

Or export manually:

```bash
export AUDIO_INPUT_DEVICE=plughw:2,0
export AUDIO_OUTPUT_DEVICE=plughw:3,0
python pi5_client.py ws://192.168.1.42:8765
```

Install ALSA tools if needed:

```bash
sudo apt install alsa-utils
```

## Prerequisites

- **Python 3.11+** (pre-installed on Raspberry Pi OS Bookworm)
- **websockets** library

Install the dependency:

```bash
pip install -r requirements.txt
```

Or manually:

```bash
pip install websockets>=15.0
```

## Finding the Laptop's IP Address

On the laptop running voice-assistant-app:

**macOS:**
```bash
ipconfig getifaddr en0
```

**Linux:**
```bash
hostname -I | awk '{print $1}'
```

**Windows:**
```bash
ipconfig
# Look for "IPv4 Address" under your Wi-Fi adapter
```

Both devices must be on the same Wi-Fi network.

## Running the Client

```bash
python pi5_client.py ws://LAPTOP_IP:8765
```

Replace `LAPTOP_IP` with the actual IP address of the laptop (e.g., `192.168.1.42`).

For verbose output:

```bash
python pi5_client.py ws://192.168.1.42:8765 --debug
```

## Expected Output

On a successful connection:

```
2026-06-30 15:00:00 [INFO] Pi 5 Voice Assistant Client v0.1.0
2026-06-30 15:00:00 [INFO] Device ID: raspberrypi | Platform: Linux-6.1.0-rpi-arm64
2026-06-30 15:00:00 [INFO] Target server: ws://192.168.1.42:8765
2026-06-30 15:00:00 [INFO] Connecting to ws://192.168.1.42:8765 (attempt 1/5)...
2026-06-30 15:00:00 [INFO] Sending HELLO (device_id=raspberrypi)
2026-06-30 15:00:00 [INFO] Handshake complete! session_id=sess_abc123, audio_config={'sample_rate': 24000, 'format': 'pcm16', 'channels': 1}
2026-06-30 15:00:00 [INFO] Entering main loop. Sending status every 10s.
```

If the server is not running:

```
2026-06-30 15:00:00 [INFO] Connecting to ws://192.168.1.42:8765 (attempt 1/5)...
2026-06-30 15:00:00 [WARNING] Connection failed (Connection refused). Retrying in 1.0s...
2026-06-30 15:00:01 [INFO] Connecting to ws://192.168.1.42:8765 (attempt 2/5)...
...
2026-06-30 15:00:15 [ERROR] Failed to connect after 5 attempts. Giving up.
2026-06-30 15:00:15 [INFO] Client stopped.
```

## Troubleshooting

### "Connection refused"
- The voice-assistant-app is not running on the laptop, or is not listening on port 8765.
- Start the app first: `python -m voice_assistant` on the laptop.

### "No route to host" / "Network is unreachable"
- The Pi and laptop are not on the same network.
- Check that both are connected to the same Wi-Fi SSID.
- Try pinging the laptop from the Pi: `ping LAPTOP_IP`.

### Timeout waiting for HELLO_ACK
- The app is running but may not have a WebSocket server active (e.g., running in mock mode without the WebSocket transport).
- Ensure the app is started with WebSocket transport enabled.

### "websockets" import error
- Install with: `pip install websockets>=15.0`
- If using a virtual environment, make sure it's activated.

### Ctrl+C doesn't stop the script
- The script handles KeyboardInterrupt. If it doesn't respond immediately, it may be in a backoff sleep. Wait for the current sleep to finish or press Ctrl+C again.

## Loopback Test (Phase 4)

Once the laptop app relays `AUDIO_FRAME` back as `PLAY_AUDIO`, you can verify Pi mic → laptop → Pi speaker:

1. Start the laptop app with WebSocket server: `python -m voice_assistant --web`
2. On the Pi, connect the client: `python pi5_client.py ws://LAPTOP_IP:8765`
3. From the dashboard, click **Start Session** (sends `START_AUDIO_STREAM`)
4. Speak into the Pi microphone — you should hear your voice on the Pi speaker after round-trip through the laptop
5. Click **Stop Session** to stop capture

Quick local ALSA sanity check on the Pi (no network):

```bash
# Record 2 seconds, play back immediately
arecord -f S16_LE -r 24000 -c 1 -t raw -d 2 /tmp/test.raw
aplay -f S16_LE -r 24000 -c 1 -t raw /tmp/test.raw
```

With explicit devices:

```bash
arecord -D plughw:2,0 -f S16_LE -r 24000 -c 1 -t raw -d 2 /tmp/test.raw
aplay -D plughw:3,0 -f S16_LE -r 24000 -c 1 -t raw /tmp/test.raw
```

Run unit tests on any machine (no hardware required):

```bash
python test_client.py
python test_audio.py
```
