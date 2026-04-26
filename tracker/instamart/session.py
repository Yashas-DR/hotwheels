"""
instamart/session.py — Swiggy Instamart session management

Completely independent from Blinkit session.
Reads from instamart_session.json (written by instamart_extractor.py).

Session file shape:
    {
        "cookies":       { name: value, ... },   ← sid, tid, deviceId, aws-waf-token
        "api_headers":   { key: value, ... },     ← captured live headers
        "user_agent":    "Mozilla/5.0 ...",
        "store_map":     {                         ← storeId per lat/lng pair
            "12.9567,77.7001": 1400609,
            "12.9435,77.7070": 1400610
        },
        "layout_id":     6021,
        "saved_at":      1712700000.0
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SESSION_FILE = _PROJECT_ROOT / "instamart_session.json"

_FALLBACK_LAT = "12.956695"
_FALLBACK_LON = "77.7000965"


class InstaSession:
    """
    Swiggy Instamart session — completely isolated from Blinkit.
    Load once, validate, then provide (cookies, headers, storeId) per request.
    """

    def __init__(self, config: dict):
        self.config = config
        instamart_cfg = config.get("instamart", {})
        timing = config.get("timing", {})

        self._refresh_min = timing.get("session_refresh_interval_min", 30) * 60
        self._refresh_max = timing.get("session_refresh_interval_max", 60) * 60
        self._layout_id: int = instamart_cfg.get("layout_id", 6021)
        self._client_id: str = instamart_cfg.get("client_id", "INSTAMART-APP")

        # Build location lookup: name → (primary_store_id, secondary_store_id)
        self._location_stores: dict[str, tuple[int, str]] = {}
        for loc in instamart_cfg.get("locations", []):
            name = loc.get("name", "")
            primary = loc.get("store_id")
            secondary = str(loc.get("secondary_store_id", "") or "")
            if primary:
                self._location_stores[name] = (int(primary), secondary)

        self._cookies: dict[str, str] = {}
        self._api_headers: dict[str, str] = {}
        self._user_agent: str = ""
        self._store_map: dict[str, int] = {}  # lat,lng key (from session file)
        self._valid: bool = False
        self._last_refresh: float = 0.0
        self._next_refresh_in: float = self._random_interval()

    @property
    def is_valid(self) -> bool:
        return self._valid

    @property
    def layout_id(self) -> int:
        return self._layout_id

    @property
    def client_id(self) -> str:
        return self._client_id

    def _random_interval(self) -> float:
        return random.uniform(self._refresh_min, self._refresh_max)

    def needs_refresh(self) -> bool:
        if not self._valid:
            return True
        return time.monotonic() - self._last_refresh >= self._next_refresh_in

    # ── File I/O ──────────────────────────────────────────────────

    async def load(self) -> bool:
        """Load session from instamart_session.json."""
        if not SESSION_FILE.exists():
            logger.info(
                "No Instamart session found at %s — "
                "run: python tracker/tools/instamart_extractor.py",
                SESSION_FILE,
            )
            return False
        try:
            raw = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            self._cookies = raw.get("cookies", {})
            self._api_headers = raw.get("api_headers", {})
            self._user_agent = raw.get("user_agent", "")
            self._store_map = raw.get("store_map", {})
            if raw.get("layout_id"):
                self._layout_id = raw["layout_id"]
            logger.info(
                "Instamart session loaded: %d cookies, %d stores mapped",
                len(self._cookies), len(self._store_map),
            )
            return bool(self._cookies)
        except Exception as e:
            logger.warning("Failed to load instamart_session.json: %s", e)
            return False

    # ── Store ID lookup ───────────────────────────────────────────

    def get_store_ids(self, location_name: str) -> tuple[Optional[int], str]:
        """
        Return (primary_store_id, secondary_store_id) for a location name.
        Falls back to session file store_map if config has no entry.
        Returns (None, "") if nothing found.
        """
        # 1. Config-based lookup (preferred, from hardcoded storeIds)
        if location_name in self._location_stores:
            return self._location_stores[location_name]

        # 2. Session file fallback (from instamart_extractor.py)
        if self._store_map:
            default_id = self._store_map.get("default")
            if default_id:
                try:
                    return int(default_id), ""
                except (ValueError, TypeError):
                    pass
            # First available
            first = next(iter(self._store_map.values()))
            try:
                return int(first), ""
            except (ValueError, TypeError):
                pass

        logger.warning(
            "No storeId for location '%s' — re-run instamart_extractor.py "
            "or add store_id to config.yaml",
            location_name,
        )
        return None, ""

    # ── Request args ──────────────────────────────────────────────

    def get_request_args(self, location=None) -> tuple[dict, dict]:
        """Return (headers, cookies) for a curl-cffi request."""
        lat = str(location.lat) if location else _FALLBACK_LAT
        lon = str(location.lng) if location else _FALLBACK_LON

        headers = {}
        if self._api_headers:
            # Strip browser-fingerprinting headers — curl-cffi's `impersonate="chrome124"`
            # injects its own perfectly-matched UA and sec-ch-ua headers.
            # If we pass our own, the UA (Chrome 124) vs sec-ch-ua (whatever session says)
            # mismatch is exactly what AWS WAF uses to fingerprint bots.
            STRIP = {"user-agent", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"}
            for k, v in self._api_headers.items():
                if k.lower() not in STRIP:
                    headers[k] = v
        else:
            headers = {
                "content-type": "application/json",
                "accept": "application/json, text/plain, */*",
                "origin": "https://www.swiggy.com",
                "referer": "https://www.swiggy.com/instamart/search?query=hot+wheels",
            }


        return headers, dict(self._cookies)

    # ── Validation ────────────────────────────────────────────────

    async def validate(self) -> bool:
        """Quick validation — just checks cookies are present and non-empty."""
        if not self._cookies:
            return False

        # Check for key Swiggy session cookie
        has_sid = "sid" in self._cookies or any(
            "swiggy" in k.lower() or "sid" in k.lower()
            for k in self._cookies
        )
        if not has_sid:
            logger.warning(
                "Instamart session: 'sid' cookie missing — session may be invalid. "
                "Re-run instamart_extractor.py"
            )
            # Don't hard-fail — let the actual API call reveal if it's bad
        return True

    async def start(self) -> bool:
        """Load session and do basic validation."""
        loaded = await self.load()
        if not loaded:
            self._valid = False
            return False
        valid = await self.validate()
        self._valid = valid
        self._last_refresh = time.monotonic()
        self._next_refresh_in = self._random_interval()
        if valid:
            logger.info("✅ Instamart session ready.")
        return valid
