"""
sound_alert.py — Cross-platform sound alert

Plays a WAV/MP3 alert when a Hot Wheels match is found.
Tries playsound3 first, falls back to winsound (Windows-only beep).
Non-fatal: if sound fails, logs a warning and continues.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


class SoundAlerter:
    """Plays an audio alert on product match."""

    def __init__(self, config: dict):
        alerts_cfg = config.get("alerts", {})
        self.enabled: bool = alerts_cfg.get("sound_enabled", True)
        sound_file = alerts_cfg.get("sound_file", "assets/alert.wav")

        # Resolve path relative to tracker directory
        self.sound_path = Path(sound_file)
        if not self.sound_path.is_absolute():
            self.sound_path = Path(__file__).parent.parent / sound_file

    async def play(self) -> None:
        """Play the alert sound asynchronously (non-blocking)."""
        if not self.enabled:
            return

        # Run in thread pool so it doesn't block the event loop
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._play_sync)

    def _play_sync(self) -> None:
        """Synchronous sound playback with fallback chain."""
        # Attempt 1: playsound3 (cross-platform)
        if self.sound_path.exists():
            try:
                from playsound3 import playsound
                playsound(str(self.sound_path))
                return
            except Exception as e:
                logger.debug("playsound3 failed: %s — trying fallback", e)

        # Attempt 2: winsound (Windows built-in, no file needed)
        if sys.platform == "win32":
            try:
                import winsound
                # Three quick beeps
                for _ in range(3):
                    winsound.Beep(880, 200)
                    winsound.Beep(1100, 200)
                return
            except Exception as e:
                logger.debug("winsound failed: %s", e)

        # Attempt 3: terminal bell
        try:
            print("\a\a\a", end="", flush=True)
        except Exception:
            pass

        if not self.sound_path.exists():
            logger.warning(
                "Sound file not found: %s — run setup to generate it.", self.sound_path
            )
