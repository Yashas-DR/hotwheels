"""
stop_sound.py — Silence the looping audio alert without stopping the tracker.

Run from the project root (y:\\hobby\\hotwheels):

    python tracker/tools/stop_sound.py

Or run from the tracker/ directory:

    python tools/stop_sound.py

The running tracker will detect the flag file within 0.5 seconds,
stop the audio, and continue scanning normally.
"""

import sys
from pathlib import Path

# The flag file must be created in the tracker/ working directory,
# which is where the running tracker process is chdir'd to.
TRACKER_DIR = Path(__file__).resolve().parent.parent   # tracker/
FLAG_FILE = TRACKER_DIR / "STOP_SOUND"

def main() -> None:
    FLAG_FILE.touch()
    print(f"✅ STOP_SOUND flag created at: {FLAG_FILE}")
    print("   The tracker will silence audio within 0.5 seconds.")
    print("   Scanning continues normally — only the sound stops.")

if __name__ == "__main__":
    main()
