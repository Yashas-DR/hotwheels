"""
sound_alert.py — Looping audio alert player

Design
------
pygame.mixer MUST be initialised from the main thread on Windows (SDL2 limitation).
SoundAlerter.__init__ does the one-time init + file preload (called from main thread).
The daemon audio thread only calls pygame.mixer.music.play / stop — no init there.

Reason-based active set
-----------------------
Multiple callers can independently request looping:
    await player.play_loop("match")         # new product found
    await player.play_loop("internet_down") # internet lost

Sound keeps playing as long as ANY reason is active.
    await player.stop("internet_down")  # internet back — "match" still loops
    await player.stop("match")          # now silent

Stop sound without killing the tracker
---------------------------------------
    python tracker/tools/stop_sound.py      ← run from a second terminal

Creates STOP_SOUND in tracker/ dir. The audio loop detects it within 0.5 s,
silences audio, deletes the flag, and scanning continues normally.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Optional, Set

logger = logging.getLogger(__name__)

STOP_FLAG_FILE = Path("STOP_SOUND")   # relative to tracker/ CWD


class LoopingAudioPlayer:
    """
    Plays an MP3/WAV in an infinite loop in a background daemon thread.

    pygame.mixer is initialised ONCE from the calling (main) thread.
    The daemon thread only calls play/stop — thread-safe on Windows.
    """

    def __init__(self, sound_path: Path, enabled: bool = True):
        self.sound_path = sound_path
        self.enabled = False   # set True only if init + load succeed

        if not enabled:
            return

        if not sound_path.exists():
            logger.warning("🔇 Sound file not found: %s — audio disabled", sound_path)
            return

        # ── Init pygame from main thread ──────────────────────────────────
        try:
            import pygame
            if not pygame.mixer.get_init():
                pygame.mixer.pre_init(frequency=44100, size=-16, channels=2, buffer=1024)
                pygame.mixer.init()
            logger.info("🔊 pygame.mixer ready (SDL %s)", pygame.version.SDL)
        except Exception as e:
            logger.warning("🔇 pygame.mixer init failed: %s — audio disabled", e)
            return

        # ── Preload music file from main thread ───────────────────────────
        try:
            import pygame
            pygame.mixer.music.load(str(sound_path))
            logger.info("🔊 Sound loaded: %s", sound_path.name)
        except Exception as e:
            logger.warning("🔇 Cannot load sound file %s: %s — audio disabled", sound_path, e)
            return

        self.enabled = True

        self._active_reasons: Set[str] = set()
        self._lock = threading.Lock()
        self._player_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ── Public async interface ────────────────────────────────────────────

    async def play_loop(self, reason: str = "match") -> None:
        """Start looping audio for `reason`. No-op if already active."""
        if not self.enabled:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._add_reason, reason)

    async def stop(self, reason: str = "match") -> None:
        """Remove `reason`. If no more reasons remain, stop the audio."""
        if not self.enabled:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._remove_reason, reason)

    def stop_all(self) -> None:
        """Immediately silence everything — called on Ctrl+C."""
        if not self.enabled:
            return
        with self._lock:
            self._active_reasons.clear()
        self._stop_event.set()
        self._join_thread()

    # ── Internal ─────────────────────────────────────────────────────────

    def _add_reason(self, reason: str) -> None:
        with self._lock:
            already = bool(self._active_reasons)
            self._active_reasons.add(reason)

        if already:
            logger.debug("🔊 Audio: reason '%s' added (already looping)", reason)
            return

        logger.info("🔊 Audio loop START — %s [%s]", reason, self.sound_path.name)
        self._start_thread()

    def _remove_reason(self, reason: str) -> None:
        with self._lock:
            self._active_reasons.discard(reason)
            remaining = set(self._active_reasons)

        if remaining:
            logger.info("🔊 Audio: '%s' done, still looping for: %s", reason, remaining)
            return

        logger.info("🔇 Audio loop STOP (no more active reasons)")
        self._stop_event.set()
        self._join_thread()

    def _start_thread(self) -> None:
        self._stop_event.set()
        self._join_thread()
        self._stop_event = threading.Event()
        t = threading.Thread(target=self._loop_worker, daemon=True, name="HW-Audio")
        self._player_thread = t
        t.start()

    def _join_thread(self) -> None:
        t = self._player_thread
        if t and t.is_alive():
            t.join(timeout=3.0)
        self._player_thread = None

    def _loop_worker(self) -> None:
        """
        Daemon thread — music is already loaded (done in __init__).
        Just play(loops=-1) and wait for stop signal or flag file.
        pygame.mixer.music is global but thread-safe for play/stop calls
        when init was done on the main thread.
        """
        try:
            import pygame
            pygame.mixer.music.play(loops=-1)
            logger.info("🔊 pygame music playing: %s (looping)", self.sound_path.name)
        except Exception as e:
            logger.warning("🔇 pygame.mixer.music.play failed: %s", e)
            return

        # Poll for stop signal or STOP_SOUND flag file every 0.5 s
        while not self._stop_event.wait(timeout=0.5):
            if STOP_FLAG_FILE.exists():
                logger.info("🔇 STOP_SOUND flag detected — silencing (tracker keeps running)")
                try:
                    STOP_FLAG_FILE.unlink()
                except Exception:
                    pass
                with self._lock:
                    self._active_reasons.clear()
                break

        try:
            import pygame
            pygame.mixer.music.stop()
            logger.info("🔇 Audio stopped")
        except Exception as e:
            logger.debug("music.stop error (non-fatal): %s", e)


# ── SoundAlerter — public interface used by main.py ──────────────────────

class SoundAlerter:
    """
    High-level sound interface.

    Calling play() starts the fein.mp3 loop for a product match.
    Call stop_all() on Ctrl+C to immediately silence audio.
    """

    def __init__(self, config: dict):
        alerts_cfg = config.get("alerts", {})
        enabled    = alerts_cfg.get("sound_enabled", True)
        sound_file = alerts_cfg.get("sound_file", "fein.mp3")

        sound_path = Path(sound_file)
        if not sound_path.is_absolute():
            # Resolve relative to tracker/ (parent of alerts/)
            sound_path = Path(__file__).resolve().parent.parent / sound_file

        logger.info("🔊 Sound file path: %s (exists=%s)", sound_path, sound_path.exists())

        self._player = LoopingAudioPlayer(sound_path=sound_path, enabled=enabled)

    async def play(self) -> None:
        """Loop alert for a new product match."""
        await self._player.play_loop("match")

    async def play_loop(self, reason: str = "match") -> None:
        await self._player.play_loop(reason)

    async def stop(self, reason: str = "match") -> None:
        await self._player.stop(reason)

    def stop_all(self) -> None:
        self._player.stop_all()

    @property
    def player(self) -> LoopingAudioPlayer:
        return self._player
