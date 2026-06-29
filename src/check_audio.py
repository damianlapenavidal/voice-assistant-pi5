"""List audio hardware and run a short record/playback test when devices are found."""

import os
import re
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

TEST_WAV = Path("/tmp/voice-assistant-test.wav")
CARD_LINE = re.compile(r"^card (\d+): (.+)$", re.MULTILINE)


def run_command(command):
    result = subprocess.run(command, capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip()
    return result.returncode, output


def parse_cards(output):
    return [(int(number), name) for number, name in CARD_LINE.findall(output)]


def pick_device(cards, skip_hdmi=True):
    """Pick the first non-HDMI card, or fall back to the first card listed."""
    for number, name in cards:
        if skip_hdmi and "hdmi" in name.lower():
            continue
        return f"plughw:{number},0"

    if cards:
        return f"plughw:{cards[0][0]},0"

    return None


def list_devices():
    print("=== Capture devices (microphones) ===")
    _, capture_output = run_command(["arecord", "-l"])
    print(capture_output or "(none)")

    print("\n=== Playback devices (speakers) ===")
    _, playback_output = run_command(["aplay", "-l"])
    print(playback_output or "(none)")

    capture_cards = parse_cards(capture_output)
    playback_cards = parse_cards(playback_output)
    return capture_cards, playback_cards


def resolve_devices(capture_cards, playback_cards):
    load_dotenv()
    input_device = os.getenv("AUDIO_INPUT_DEVICE")
    output_device = os.getenv("AUDIO_OUTPUT_DEVICE")

    if not input_device:
        input_device = pick_device(capture_cards, skip_hdmi=False)
    if not output_device:
        output_device = pick_device(playback_cards, skip_hdmi=True)

    return input_device, output_device


def get_audio_devices():
    """Return (input_device, output_device) ALSA names, or (None, None) if missing."""
    _, capture_output = run_command(["arecord", "-l"])
    _, playback_output = run_command(["aplay", "-l"])
    capture_cards = parse_cards(capture_output)
    playback_cards = parse_cards(playback_output)

    if not capture_cards or not playback_cards:
        return None, None

    return resolve_devices(capture_cards, playback_cards)


def record_and_play(input_device, output_device):
    print(f"\nUsing mic:    {input_device}")
    print(f"Using speaker: {output_device}")
    print(f"Recording 3 seconds to {TEST_WAV} ...")

    record_code, record_output = run_command(
        ["arecord", "-D", input_device, "-d", "3", "-f", "cd", str(TEST_WAV)]
    )
    if record_code != 0:
        print("Recording failed:")
        print(record_output)
        sys.exit(1)

    print("Recording saved. Playing it back ...")
    play_code, play_output = run_command(
        ["aplay", "-D", output_device, str(TEST_WAV)]
    )
    if play_code != 0:
        print("Playback failed:")
        print(play_output)
        sys.exit(1)

    print("Audio smoke test passed.")


def main():
    capture_cards, playback_cards = list_devices()

    if not capture_cards:
        print("\nNo microphone found. Plug in a USB mic and run this script again.")
        sys.exit(0)

    if not playback_cards:
        print("\nNo speaker found. Plug in speakers or a USB audio device and run again.")
        sys.exit(0)

    input_device, output_device = resolve_devices(capture_cards, playback_cards)
    if not input_device or not output_device:
        print("\nCould not determine audio devices.")
        sys.exit(1)

    record_and_play(input_device, output_device)


if __name__ == "__main__":
    main()
