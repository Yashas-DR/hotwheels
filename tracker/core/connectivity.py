"""
core/connectivity.py — Internet connectivity monitor

A lightweight module-level singleton.
Call report_error(err_str) whenever a curl request fails.
Call report_ok()           whenever a curl request succeeds.

On internet DOWN:
    • Fires Telegram alert (once)
    • Starts looping audio alert (once)

On internet UP:
    • Fires Telegram recovery alert (once)
    • Stops the internet-down audio loop

Audio is independent of the scraping loop — plays in a background
daemon thread so scanning resumes immediately.

Curl error codes that indicate internet loss:
    (6)  — Could not resolve host (DNS failure)
    (7)  — Failed to connect
    (28) — Operation timed out / Connection timed out

Usage (inside _fetch_page):
    from core.connectivity import connectivity
    ...
    except Exception as e:
        connectivity.report_error(str(e))
        ...
    # on success:
    connectivity.report_ok()
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# curl error signatures that mean "no internet"
_NET_PATTERNS = (
    "curl: (6)",   # Could not resolve host
    "curl: (7)",   # Failed to connect to host
    "curl: (28)",  # Operation timed out / Connection timed out
)


def _is_network_error(err_str: str) -> bool:
    low = err_str.lower()
    return any(p.lower() in low for p in _NET_PATTERNS)


class ConnectivityMonitor:
    """
    Tracks internet connectivity state.

    State machine:
        ONLINE  → (network error) → OFFLINE → Telegram down + audio loop start
        OFFLINE → (success)       → ONLINE  → Telegram up   + audio loop stop
    """

    def __init__(self) -> None:
        self._online: bool = True
        self._down_since: Optional[float] = None
        self._alerter  = None   # TelegramAlerter — set via attach_alerter()
        self._sound    = None   # SoundAlerter    — set via attach_sound()

    def attach_alerter(self, alerter) -> None:
        """Attach the TelegramAlerter instance. Call once from main.py init."""
        self._alerter = alerter

    def attach_sound(self, sound_alerter) -> None:
        """Attach the SoundAlerter instance. Call once from main.py init."""
        self._sound = sound_alerter

    # ── Called by every platform's _fetch_page ────────────────────────────

    def report_error(self, err_str: str) -> None:
        """
        Call this when a curl request exception is caught.
        If it's a network error and we were previously online → trigger DOWN alerts.
        """
        if not _is_network_error(err_str):
            return  # unrelated error (HTTP 403 etc.) — ignore
        if not self._online:
            return  # already offline — don't re-alert
        self._online = False
        self._down_since = time.time()
        logger.warning("🌐 Internet connectivity lost")
        asyncio.ensure_future(self._on_down(err_str))

    def report_ok(self) -> None:
        """
        Call this when a curl request SUCCEEDS.
        If we were previously offline → trigger UP alerts.
        """
        if self._online:
            return  # nothing changed
        down_for = int(time.time() - (self._down_since or time.time()))
        self._online = True
        self._down_since = None
        logger.info("🌐 Internet connectivity restored (was down ~%ds)", down_for)
        asyncio.ensure_future(self._on_up(down_for))

    # ── Internal async handlers ───────────────────────────────────────────

    async def _on_down(self, err_str: str) -> None:
        """Fire Telegram alert + start looping audio when internet drops."""
        # Telegram
        if self._alerter:
            try:
                await self._alerter.send_internet_down_alert(err_str)
            except Exception as e:
                logger.debug("Could not send internet-down Telegram alert: %s", e)

        # Audio loop
        if self._sound:
            try:
                await self._sound.play_loop("internet_down")
            except Exception as e:
                logger.debug("Could not start internet-down audio loop: %s", e)

    async def _on_up(self, down_seconds: int) -> None:
        """Stop audio loop + fire Telegram recovery alert when internet returns."""
        # Stop the internet-down audio loop FIRST (so it doesn't keep blaring)
        if self._sound:
            try:
                await self._sound.stop("internet_down")
            except Exception as e:
                logger.debug("Could not stop internet-down audio loop: %s", e)

        # Telegram recovery
        if self._alerter:
            try:
                await self._alerter.send_internet_up_alert(down_seconds)
            except Exception as e:
                logger.debug("Could not send internet-up Telegram alert: %s", e)


# Module-level singleton — import and use anywhere
connectivity = ConnectivityMonitor()
