"""
session_mgr.py — Blinkit session management (curl-cffi edition)

NO PLAYWRIGHT IN THE TRACKER LOOP.

Architecture:
    - Initial login:   Done once via session_extractor.py (visible browser)
    - API calls:       Done via curl-cffi with Chrome TLS impersonation
    - Session refresh: Navigate in background Playwright browser (visible=False ok
                       since we only refresh cookies, not make tracked API calls)

Session data (session.json):
    {
        "cookies":     { name: value, ... },
        "api_headers": { key: value, ... },   ← real browser headers
        "user_agent":  "Mozilla/5.0 ...",
        "saved_at":    1712700000.0
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

# Always resolve relative to project root regardless of CWD
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BROWSER_PROFILE_DIR = _PROJECT_ROOT / "browser_profile"
SESSION_FILE = _PROJECT_ROOT / "session.json"

_FALLBACK_LAT = "12.956695"
_FALLBACK_LON = "77.7000965"


class SessionManager:
    """
    Lightweight session manager — loads cookies/headers from disk,
    provides them to BlinkitClient (curl-cffi). No browser in the loop.
    """

    def __init__(self, config: dict):
        self.config = config
        timing = config.get("timing", {})
        self._refresh_min = timing.get("session_refresh_interval_min", 30) * 60
        self._refresh_max = timing.get("session_refresh_interval_max", 60) * 60

        self._cookies: dict[str, str] = {}
        self._api_headers: dict[str, str] = {}
        self._user_agent: str = ""
        self._valid: bool = False
        self._last_refresh: float = 0.0
        self._next_refresh_in: float = self._random_refresh_interval()
        self._lock = asyncio.Lock()

    # ── Properties ────────────────────────────────────────────────

    @property
    def is_valid(self) -> bool:
        return self._valid

    @property
    def cookies(self) -> dict[str, str]:
        return dict(self._cookies)

    @property
    def api_headers(self) -> dict[str, str]:
        return dict(self._api_headers)

    @property
    def user_agent(self) -> str:
        return self._user_agent

    def _random_refresh_interval(self) -> float:
        return random.uniform(self._refresh_min, self._refresh_max)

    # ── Startup ───────────────────────────────────────────────────

    async def start(self, headless: bool = True) -> bool:
        """
        Load session from disk and validate via curl-cffi.
        No browser launched here — curl-cffi handles Chrome TLS impersonation.
        """
        loaded = await self.load_from_file()
        if not loaded:
            logger.error(
                "No session found. Run: python tracker/tools/session_extractor.py"
            )
            return False

        logger.info("Validating session via curl-cffi (Chrome TLS)...")
        valid = await self.validate()
        self._valid = valid
        self._last_refresh = time.monotonic()
        self._next_refresh_in = self._random_refresh_interval()

        if valid:
            logger.info(
                "✅ Session valid. Next refresh in %.0f min.",
                self._next_refresh_in / 60,
            )
        else:
            logger.warning("❌ Session invalid — run session_extractor.py")

        return valid

    async def stop(self) -> None:
        """No browser to close — no-op."""
        logger.debug("SessionManager.stop() — nothing to close (curl-cffi mode)")

    # ── File I/O ──────────────────────────────────────────────────

    async def load_from_file(self) -> bool:
        """Load cookies and API headers from session.json."""
        if not SESSION_FILE.exists():
            logger.info("No session.json found at %s", SESSION_FILE)
            return False
        try:
            data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            self._cookies = data.get("cookies", {})
            self._api_headers = data.get("api_headers", {})
            self._user_agent = data.get("user_agent", "")
            logger.info(
                "Loaded session: %d cookies, %d api_headers",
                len(self._cookies), len(self._api_headers),
            )
            return bool(self._cookies)
        except Exception as e:
            logger.warning("Failed to load session.json: %s", e)
            return False

    async def save_to_file(self) -> None:
        """Save current session to session.json."""
        data = {
            "cookies": self._cookies,
            "api_headers": self._api_headers,
            "user_agent": self._user_agent,
            "saved_at": time.time(),
        }
        SESSION_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.debug("Session saved to %s", SESSION_FILE)

    # ── Validation ────────────────────────────────────────────────

    async def validate(self) -> bool:
        """Validate session by making a real API call via curl-cffi."""
        return await self._validate_via_curl_cffi()

    async def _validate_via_curl_cffi(self) -> bool:
        """
        Make a Blinkit API call using curl-cffi with Chrome TLS impersonation.
        curl-cffi mimics Chrome's TLS fingerprint at the socket level —
        Cloudflare cannot distinguish it from a real browser.
        """
        if not self._cookies:
            logger.warning("No cookies — cannot validate")
            return False

        try:
            from curl_cffi.requests import AsyncSession
        except ImportError:
            logger.error(
                "curl-cffi not installed. Run: pip install curl-cffi"
            )
            return False

        headers = self._build_headers(lat=_FALLBACK_LAT, lon=_FALLBACK_LON)
        params = {"q": "hot wheels", "search_type": "type_to_search"}

        try:
            async with AsyncSession(impersonate="chrome120") as session:
                r = await session.post(
                    "https://blinkit.com/v1/layout/search",
                    params=params,
                    headers=headers,
                    cookies=self._cookies,
                    timeout=15,
                )

            logger.debug("Validation HTTP %d", r.status_code)

            if r.status_code in (401, 403):
                logger.warning("Session validation: HTTP %d", r.status_code)
                return False

            if r.status_code != 200:
                logger.warning("Session validation: unexpected HTTP %d", r.status_code)
                return False

            data = r.json()

            if not data.get("is_success"):
                logger.warning("Session validation: is_success=False")
                return False

            snippets = data.get("response", {}).get("snippets", [])
            logger.info("✅ Session valid (%d snippets)", len(snippets))
            return True

        except Exception as e:
            logger.error("curl-cffi validation error: %s", e)
            return False

    # ── Refresh ───────────────────────────────────────────────────

    def needs_refresh(self) -> bool:
        if not self._valid:
            return True
        elapsed = time.monotonic() - self._last_refresh
        return elapsed >= self._next_refresh_in

    async def refresh(self, headless: bool = True) -> bool:
        """
        Refresh session by re-extracting cookies via a headless Playwright browser.
        Only Playwright is needed here (for cookie refresh), not for API calls.
        """
        async with self._lock:
            logger.info("⏰ Refreshing session via Playwright...")

            try:
                from playwright.async_api import async_playwright

                async with async_playwright() as pw:
                    browser = await pw.chromium.launch_persistent_context(
                        user_data_dir=str(BROWSER_PROFILE_DIR),
                        headless=headless,
                        args=[
                            "--no-sandbox",
                            "--disable-dev-shm-usage",
                            "--disable-blink-features=AutomationControlled",
                        ],
                        ignore_default_args=["--enable-automation"],
                    )
                    await browser.add_init_script(
                        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                    )

                    page = (
                        browser.pages[0]
                        if browser.pages
                        else await browser.new_page()
                    )

                    try:
                        await page.goto("https://blinkit.com/", timeout=30_000)
                        await page.wait_for_timeout(4000)
                    except Exception as e:
                        logger.warning("Refresh navigation warning: %s", e)

                    raw_cookies = await browser.cookies("https://blinkit.com")
                    self._cookies = {c["name"]: c["value"] for c in raw_cookies}
                    await browser.close()

                await self.save_to_file()
                logger.info("Cookies refreshed (%d cookies)", len(self._cookies))

            except Exception as e:
                logger.error("Playwright refresh failed: %s", e)
                self._valid = False
                return False

            # Re-validate with the new cookies
            valid = await self._validate_via_curl_cffi()
            self._valid = valid
            self._last_refresh = time.monotonic()
            self._next_refresh_in = self._random_refresh_interval()

            if valid:
                logger.info("✅ Session refreshed. Next in %.0f min.", self._next_refresh_in / 60)
            else:
                logger.warning("❌ Session still invalid after refresh.")

            return valid

    # ── Header builder ────────────────────────────────────────────

    def _build_headers(self, lat: str = _FALLBACK_LAT, lon: str = _FALLBACK_LON) -> dict:
        """
        Build request headers. Uses captured real headers from session_extractor
        if available, otherwise falls back to a minimal set.
        Location headers always overridden fresh.
        """
        if self._api_headers:
            headers = dict(self._api_headers)
        else:
            # Minimal fallback — works if curl-cffi TLS impersonation is active
            headers = {
                "content-type": "application/json",
                "accept": "application/json, text/plain, */*",
                "app_client": "consumer_web",
                "web_app_version": "1000076",
                "app_version": "1000076",
            }
            if self._user_agent:
                headers["user-agent"] = self._user_agent

        # Always inject fresh location
        headers["lat"] = lat
        headers["lon"] = lon
        return headers

    def get_request_args(self, location=None) -> tuple[dict, dict]:
        """
        Return (headers, cookies) tuple for a curl-cffi request.
        location: Location dataclass with .lat and .lng attributes.
        """
        lat = str(location.lat) if location else _FALLBACK_LAT
        lon = str(location.lng) if location else _FALLBACK_LON
        headers = self._build_headers(lat=lat, lon=lon)
        return headers, self.cookies
