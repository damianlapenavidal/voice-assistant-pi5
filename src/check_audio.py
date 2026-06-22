"""List audio hardware and run a short record/playback test when devices are found."""

import re
import subprocess
import sys
from pathlib import Path

TEST_WAV = Path("/tmp/voice-assistant-test.wav")
CARD_LINE = re.compile(r"^card \d+:", re.MULTILINE)


def run_command(command):
    result = subprocess.run(command, capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip()
    return result.returncode, output


def list_devices():
    print("=== Capture devices (microphones) ===")
    _, capture_output = run_command(["arecord", "-l"])
    print(capture_output or "(none)")

    print("\n=== Playback devices (speakers) ===")
    _, playback_output = run_command(["aplay", "-l"])
    print(playback_output or "(none)")

    has_capture = bool(CARD_LINE.search(capture_output))
    has_playback = bool(CARD_LINE.search(playback_output))
    return has_capture, has_playback


def record_and_play():
    print(f"\nRecording 3 seconds to {TEST_WAV} ...")
    record_code, record_output = run_command(
        ["arecord", "-d", "3", "-f", "cd", str(TEST_WAV)]
    )
    if record_code != 0:
        print("Recording failed:")
        print(record_output)
        sys.exit(1)

    print("Recording saved. Playing it back ...")
    play_code, play_output = run_command(["aplay", str(TEST_WAV)])
    if play_code != 0:
        print("Playback failed:")
        print(play_output)
        sys.exit(1)

    print("Audio smoke test passed.")


def main():
    has_capture, has_playback = list_devices()

    if not has_capture:
        print("\nNo microphone found. Plug in a USB mic and run this script again.")
        sys.exit(0)

    if not has_playback:
        print("\nNo speaker found. Plug in speakers or a USB audio device and run again.")
        sys.exit(0)

    record_and_play()


if __name__ == "__main__":
    main()
