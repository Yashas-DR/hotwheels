"""
zepto_extractor.py — Zepto session extractor

Opens Zepto in a real browser, lets you log in, then:
  - Captures cookies (accessToken, refreshToken, isAuth, user_id)
  - Captures session headers (x-xsrf-token, x-csrf-secret, session_id, device_id)
  - Captures store_id / store_ids / store_etas for each of your addresses

All three addresses are already pre-configured from your HAR data:
  Novel Office (Marathahalli), Home, Friend

You only need to re-run this when:
  - Your accessToken refresh loop fails (after ~many hours)
  - You see "session expired" warnings from Zepto

Usage:
    python tracker/tools/zepto_extractor.py

IMPORTANT: The accessToken is short-lived (~1hr).
The tracker auto-refreshes it using the refreshToken. If refresh fails,
re-run this extractor to capture a fresh session.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# ── Path setup ──────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).resolve().parent.parent   # tracker/
SESSION_FILE = BASE_DIR / "zepto_session.json"
PROFILE_DIR  = BASE_DIR / "browser_profile" / "zepto"

# Pre-discovered store IDs from HAR — no need to capture interactively.
# The extractor will intercept network requests to confirm/update these.
_KNOWN_STORES: dict[str, dict] = {
    "Novel Office (Marathahalli)": {
        "store_id":   "6e2378a8-458e-4aff-b783-80685cf86a43",
        "store_ids":  "6e2378a8-458e-4aff-b783-80685cf86a43,1c9e155e-83a8-457a-a687-01eabb1e7a36",
        "store_etas": '{"6e2378a8-458e-4aff-b783-80685cf86a43":14,"1c9e155e-83a8-457a-a687-01eabb1e7a36":32}',
    },
    "Home (Whitefield)": {
        "store_id":   "318b567c-b7bf-4997-a79b-27c3055e080a",
        "store_ids":  "318b567c-b7bf-4997-a79b-27c3055e080a,76cae5a0-b75d-456b-9aab-df0b0b7559c2",
        "store_etas": '{"318b567c-b7bf-4997-a79b-27c3055e080a":15,"76cae5a0-b75d-456b-9aab-df0b0b7559c2":35}',
    },
    "Friend": {
        "store_id":   "70587079-1b46-4f4e-85fa-e84acebcd919",
        "store_ids":  "70587079-1b46-4f4e-85fa-e84acebcd919,6b1d82f8-b2ea-4e43-ae70-6f6bca1ec77a",
        "store_etas": '{"70587079-1b46-4f4e-85fa-e84acebcd919":15,"6b1d82f8-b2ea-4e43-ae70-6f6bca1ec77a":35}',
    },
}

# Headers to intercept from any search request
_CAPTURE_HEADERS = {
    "x-xsrf-token", "x-csrf-secret",
    "session_id", "sessionid",
    "device_id", "deviceid",
    "store_id", "store_ids", "store_etas",
}


async def main(auto: bool = False) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("❌ Playwright not installed. Run: pip install playwright")
        print("   Then: playwright install chromium")
        sys.exit(1)

    print("=" * 62)
    print("  Zepto Session Extractor")
    print("=" * 62)
    print()
    print("📌 Steps:")
    print("  1. A Chrome window will open → https://www.zepto.com")
    print("  2. Log in if needed")
    print("  3. Search 'hot wheels' once to warm up the session")
    print("  4. Press ENTER in this terminal when done")
    print()
    print("  NOTE: accessToken auto-refreshes during tracker operation.")
    print("  Only re-run this if you see 'session expired' errors.")
    print()

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    # Captured from network interception
    captured_session_headers: dict[str, str] = {}
    captured_stores: dict[str, dict] = dict(_KNOWN_STORES)  # start with known

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_https_errors=True,
        )

        page = browser.pages[0] if browser.pages else await browser.new_page()

        # Intercept requests to capture headers
        def on_request(request):
            url = request.url
            if "user-search-service/api/v3/search" in url and "filters" not in url:
                req_headers = request.headers
                for h in _CAPTURE_HEADERS:
                    val = req_headers.get(h, "")
                    if val:
                        normalized = h.replace("sessionid", "session_id").replace("deviceid", "device_id")
                        captured_session_headers[normalized] = val

                # Check if this is a different store_id than Novel Office
                sid = req_headers.get("store_id", "")
                sids = req_headers.get("store_ids", req_headers.get("store_ids", sid))
                setas = req_headers.get("store_etas", "")
                if sid and sid != _KNOWN_STORES["Novel Office (Marathahalli)"]["store_id"]:
                    # Try to match to known stores and update etas (ETAs change in real-time)
                    for loc_name, loc_data in captured_stores.items():
                        if loc_data["store_id"] == sid:
                            captured_stores[loc_name]["store_etas"] = setas or loc_data["store_etas"]
                elif sid and setas:
                    # Update Novel Office etas
                    captured_stores["Novel Office (Marathahalli)"]["store_etas"] = setas

        page.on("request", on_request)

        print("🌐 Navigating to Zepto...")
        await page.goto("https://www.zepto.com", wait_until="domcontentloaded")

        print()
        print("✅ Browser is open.")
        print("   → Log in (if needed)")
        print("   → Search 'hot wheels' to warm up the session")
        print()
        print("    >>> Press ENTER HERE (in this terminal) when done <<<")
        print()

        if auto:
            print("  👉 Auto mode — waiting 10s for page to settle...")
            await page.wait_for_timeout(10000)
        else:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, input, "")

        print("🍪 Extracting session data...")

        try:
            cookies_list = await browser.cookies()
        except Exception:
            cookies_list = []

        await browser.close()

    # Build cookies dict
    cookies: dict[str, str] = {c["name"]: c["value"] for c in cookies_list}

    # Validate key cookies
    missing = []
    for key in ("accessToken", "refreshToken", "isAuth", "user_id"):
        if key not in cookies:
            missing.append(key)

    if missing:
        print()
        print(f"⚠️  Warning: missing cookies: {missing}")
        print("   Make sure you are logged in before pressing ENTER.")
        print("   Re-run and log in first.")

    # Build session_headers from captured network data
    session_headers: dict[str, str] = {}
    for h in ("x-xsrf-token", "x-csrf-secret", "session_id", "device_id"):
        val = captured_session_headers.get(h, "")
        if val:
            session_headers[h] = val

    if not session_headers.get("x-xsrf-token"):
        print("⚠️  x-xsrf-token not captured — search 'hot wheels' in Zepto before pressing ENTER")

    session_data = {
        "cookies":         cookies,
        "session_headers": session_headers,
        "locations":       captured_stores,
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "_note": "Generated by zepto_extractor.py — do not edit manually",
    }

    SESSION_FILE.write_text(
        json.dumps(session_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print()
    print(f"✅ Session saved → {SESSION_FILE}")
    print(f"   Cookies captured    : {len(cookies)}")
    print(f"   Session headers     : {list(session_headers.keys())}")
    print(f"   Locations pre-set   : {list(captured_stores.keys())}")
    print(f"   accessToken present : {'accessToken' in cookies}")
    print(f"   refreshToken present: {'refreshToken' in cookies}")
    print()
    print("Next steps:")
    print("  1. Edit tracker/config.yaml → zepto.enabled: true")
    print("  2. Run: python tracker/main.py --once")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto", action="store_true", help="Auto-refresh (no manual inputs)")
    args = parser.parse_args()

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main(auto=args.auto))
