"""
bigbasket/session.py — BigBasket session manager

Loads cookies and headers captured by bigbasket_extractor.py.
Location context is baked into the session cookies (_bb_nhid, _bb_dsid)
at capture time — no runtime location switching needed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SESSION_FILE = Path("bigbasket_session.json")

# Required headers BigBasket sends on every listing API call
_STATIC_HEADERS = {
    "x-channel": "BB-WEB",
    "x-entry-context": "bb-b2c",
    "x-entry-context-id": "100",
    "x-integrated-fc-door-visible": "true",
    "osmos-enabled": "true",
    "common-client-static-version": "101",
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "dnt": "1",
    # These are required by BB's anti-bot layer — browsers always send them on XHR
    "origin": "https://www.bigbasket.com",
    "referer": "https://www.bigbasket.com/ps/?q=hot+wheels",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}


class BBSession:
    """
    Manages BigBasket session state loaded from a session JSON file.

    The session file is written by tracker/tools/bigbasket_extractor.py
    and contains cookies + the captured User-Agent.
    """

    def __init__(self, config: dict, session_file_override: str | None = None):
        bb_cfg = config.get("bigbasket", {})
        # session_file_override wins over config value
        self._session_file = Path(
            session_file_override or bb_cfg.get("session_file", "bigbasket_session.json")
        )
        self._cookies: dict[str, str] = {}
        self._user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
        self.is_valid: bool = False

    async def start(self) -> bool:
        """Load session from file. Returns True if usable."""
        return self._load()

    def _load(self) -> bool:
        if not self._session_file.exists():
            logger.warning(
                "BigBasket session file not found: %s — "
                "run: python tracker/tools/bigbasket_extractor.py",
                self._session_file,
            )
            self.is_valid = False
            return False

        try:
            data = json.loads(self._session_file.read_text(encoding="utf-8"))
            self._cookies = data.get("cookies", {})
            self._user_agent = data.get("user_agent", self._user_agent)

            if not self._cookies:
                logger.warning("BigBasket session file has no cookies — re-run extractor")
                self.is_valid = False
                return False

            # Verify at least a sessionid is present
            if "sessionid" not in self._cookies:
                logger.warning("BigBasket session missing 'sessionid' — re-run extractor")
                self.is_valid = False
                return False

            nhid = self._cookies.get("_bb_nhid", "?")
            logger.info(
                "✅ BigBasket session loaded: %d cookies, nhid=%s",
                len(self._cookies), nhid,
            )
            self.is_valid = True
            return True

        except Exception as e:
            logger.error("Failed to load BigBasket session: %s", e)
            self.is_valid = False
            return False

    def get_request_args(self) -> tuple[dict, dict]:
        """
        Returns (headers, cookies) for use in API requests.
        """
        headers = {
            **_STATIC_HEADERS,
            "user-agent": self._user_agent,
        }
        return headers, dict(self._cookies)
