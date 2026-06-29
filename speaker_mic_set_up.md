# Audio Volume Commands (Raspberry Pi 5)

Your USB devices (card numbers may change if replugged):

| Device | ALSA card | Control |
|--------|-----------|---------|
| USB mic (CMTECK) | `2` | `Mic` |
| USB speaker | `3` | `PCM` |

Check card numbers anytime:

```bash
arecord -l    # microphones
aplay -l      # speakers
```

Optional `.env` overrides:

```env
AUDIO_INPUT_DEVICE=plughw:2,0
AUDIO_OUTPUT_DEVICE=plughw:3,0
```

---

## Check current levels

```bash
amixer -c 2    # mic (capture)
amixer -c 3    # speaker (playback)
```

---

## Speaker volume (card 3)

```bash
amixer -c 3 set PCM 80%       # set to 80%
amixer -c 3 set PCM 5%+       # increase by 5%
amixer -c 3 set PCM 5%-       # decrease by 5%
amixer -c 3 set PCM unmute    # unmute
amixer -c 3 set PCM mute      # mute
```

---

## Mic sensitivity (card 2)

```bash
amixer -c 2 set Mic 80%       # set capture level
amixer -c 2 set Mic 5%+       # more sensitive
amixer -c 2 set Mic 5%-       # less sensitive
amixer -c 2 set Mic unmute    # unmute
amixer -c 2 set Mic mute      # mute
```

---

## Interactive mixer (optional)

```bash
alsamixer -c 3    # speaker — ↑/↓ volume, M mute, Esc quit
alsamixer -c 2    # mic
```

---

## Test after adjusting volume

```bash
arecord -D plughw:2,0 -d 5 -f cd /tmp/vol-test.wav
aplay -D plughw:3,0 /tmp/vol-test.wav
```

Or use the project script:

```bash
cd ~/voice-assistant
source .venv/bin/activate
python src/check_audio.py
```
