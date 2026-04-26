"""
instamart_extractor.py — One-time Swiggy Instamart session setup

What it captures:
    1. All session cookies (sid, tid, deviceId, aws-waf-token, lat, lng, addressId)
    2. Real API headers (captured by intercepting the live search request)
    3. storeId (extracted from intercepted URL query params)

All saved to: instamart_session.json

Usage:
    python tracker/tools/instamart_extractor.py

After this, set instamart.enabled: true in config.yaml.
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).parent.parent))

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BROWSER_PROFILE_DIR = _PROJECT_ROOT / "instamart_browser_profile"  # SEPARATE from Blinkit
SESSION_FILE = _PROJECT_ROOT / "instamart_session.json"

SEARCH_API_PATH = "/api/instamart/search/v2"
SKIP_HEADERS = {"accept-encoding", "connection", "content-length", "host"}


def log(msg, tag="INFO"):
    syms = {"INFO": "ℹ️ ", "OK": "✅", "WARN": "⚠️ ", "ERR": "❌", "STEP": "👉", "DATA": "📦"}
    print(f"  {syms.get(tag, '   ')}  {msg}", flush=True)


async def extract(auto: bool = False):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log("Playwright not installed. Run: pip install playwright && playwright install chromium", "ERR")
        sys.exit(1)

    print("\n" + "═" * 62)
    print("  🟠 Swiggy Instamart Session Extractor")
    print("═" * 62 + "\n")
    print("  NOTE: Uses a SEPARATE browser profile from Blinkit.")
    print("        Your Blinkit session is NOT affected.\n")
    if not auto:
        input("  Press Enter to open the browser → ")

    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    log(f"Browser profile: {BROWSER_PROFILE_DIR}", "STEP")

    pw = await async_playwright().start()
    browser = await pw.chromium.launch_persistent_context(
        user_data_dir=str(BROWSER_PROFILE_DIR),
        headless=False,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
        ],
        ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 800},
        # No UA spoof — let Playwright use its real Chrome version
        # so the aws-waf-token is bound to the actual Chromium JA3 fingerprint.
        # curl-cffi is set to impersonate="chrome145" to match.
    )
    await browser.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3, 4, 5] });
        window.chrome = { runtime: {} };
    """)

    page = browser.pages[0] if browser.pages else await browser.new_page()

    # ── Intercept search request ──────────────────────────────────
    captured = {"headers": {}, "store_id": None, "lat": None, "lng": None, "done": False}

    def on_request(req):
        if SEARCH_API_PATH in req.url and not captured["done"]:
            # Extract storeId from URL params
            try:
                parsed = urlparse(req.url)
                params = parse_qs(parsed.query)
                store_id = params.get("storeId", [None])[0]
                if store_id:
                    captured["store_id"] = int(store_id)
            except Exception:
                pass

            # Capture headers
            hdrs = dict(req.headers)
            captured["headers"] = {
                k: v for k, v in hdrs.items()
                if k.lower() not in SKIP_HEADERS
            }
            captured["done"] = True

            log(f"Search API intercepted! store_id={captured['store_id']}", "OK")
            log(f"Headers: {list(captured['headers'].keys())}", "DATA")

    page.on("request", on_request)
    log("Request interceptor active", "OK")

    log("Loading swiggy.com/instamart...", "STEP")
    try:
        await page.goto("https://www.swiggy.com/instamart", timeout=30_000)
        log("Instamart loaded", "OK")
    except Exception as e:
        log(f"Navigation warning: {e}", "WARN")

    print("\n" + "─" * 62)
    print("  👉 Browser is open on Swiggy Instamart.")
    print("  👉 If not logged in, log in with your phone + OTP.")
    print("  👉 Make sure your delivery location is set correctly.")
    print("  👉 Come back here once you see the Instamart home page.")
    print("─" * 62)
    if auto:
        log("Auto mode — waiting 12s for tokens/WAF to update...", "STEP")
        await page.wait_for_timeout(12000)
    else:
        input("\n  Press Enter once logged in and location is set → ")

    log("Triggering search to intercept API...", "STEP")
    try:
        await page.goto(
            "https://www.swiggy.com/instamart/search?query=hot+wheels",
            timeout=30_000,
            wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(4000)
        log("Search page loaded", "OK")
    except Exception as e:
        log(f"Search navigation warning: {e}", "WARN")

    if not captured["done"]:
        log("Waiting 3s more for API request...", "WARN")
        await page.wait_for_timeout(3000)

    if not captured["done"]:
        log("API not intercepted. The storeId will be None.", "WARN")
        log("Try: manually search for something on Instamart, then re-run.", "WARN")

    # ── Extract cookies ───────────────────────────────────────────
    log("Extracting cookies...", "STEP")
    raw_cookies = await browser.cookies(["https://www.swiggy.com", "https://swiggy.com"])
    cookies = {c["name"]: c["value"] for c in raw_cookies}
    log(f"Cookies: {list(cookies.keys())}", "DATA")

    # Try to get lat/lng from cookies
    lat = cookies.get("userLocation[lat]") or cookies.get("lat") or cookies.get("Lat")
    lng = cookies.get("userLocation[lng]") or cookies.get("lng") or cookies.get("Long")

    ua = await page.evaluate("navigator.userAgent")
    log(f"User-Agent: {ua[:80]}", "DATA")

    log(f"Store ID captured: {captured['store_id']}", "DATA")

    # ── Validate from within browser ──────────────────────────────
    log("Validating session from within browser...", "STEP")
    val_js = """
        async () => {
            try {
                const r = await fetch(
                    'https://www.swiggy.com/api/instamart/search/v2?offset=0&ageConsent=false' +
                    '&layoutId=6021&storeId=0&primaryStoreId=0&secondaryStoreId=',
                    {
                        method: 'POST',
                        credentials: 'include',
                        headers: { 'content-type': 'application/json' },
                        body: JSON.stringify({
                            facets: [], sortAttribute: '',
                            query: 'hot wheels', search_results_offset: '0',
                            page_type: 'INSTAMART_AUTO_SUGGEST_PAGE',
                            is_pre_search_tag: false
                        })
                    }
                );
                return { status: r.status, ok: r.ok };
            } catch(e) { return { ok: false, status: 0, error: e.toString() }; }
        }
    """
    try:
        vr = await page.evaluate(val_js)
        log(f"Validation response: HTTP {vr.get('status')} ok={vr.get('ok')}", "DATA")
    except Exception as e:
        log(f"Validation error: {e}", "WARN")
        vr = {"ok": False}

    log("Closing browser...", "STEP")
    await browser.close()
    await pw.stop()
    log("Browser closed", "OK")

    # ── Build and save store_map ──────────────────────────────────
    store_map = {}
    if captured["store_id"]:
        # Try to build a lat/lng-keyed entry — but cookie values can be garbled
        if lat and lng:
            try:
                key = f"{float(lat):.4f},{float(lng):.4f}"
                store_map[key] = captured["store_id"]
            except (ValueError, TypeError) as e:
                log(f"Could not parse lat/lng from cookies ({e}) — skipping lat/lng key", "WARN")
        # Always keep a fallback "default" key
        store_map["default"] = str(captured["store_id"])
        log(f"Store map: {store_map}", "DATA")
    else:
        log("No store ID captured — store_map will be empty", "WARN")
        log("HOW TO FIX: Open Swiggy in Chrome DevTools → Network tab", "WARN")
        log("  → Filter 'instamart/search' → check URL params for storeId", "WARN")

    data = {
        "cookies": cookies,
        "api_headers": captured["headers"],
        "user_agent": ua,
        "store_map": store_map,
        "layout_id": 6021,
        "validated": vr.get("ok", False),
        "saved_at": time.time(),
    }
    SESSION_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    print("\n" + "═" * 62)
    log(f"Session saved to {SESSION_FILE}", "OK")

    if captured["store_id"]:
        log(f"Store ID found: {captured['store_id']}", "OK")
        log("Add this to config.yaml under instamart.locations:", "INFO")
        print(f"\n    store_id: {captured['store_id']}  # for all Bangalore locations\n")
    else:
        log("No store ID. Manually find it (see instructions above).", "WARN")

    print("  Next steps:")
    print("  1. Edit tracker/config.yaml → set instamart.enabled: true")
    print("  2. Add store_id to each instamart location")
    print("  3. python tracker/main.py --dry-run")
    print("═" * 62 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto", action="store_true", help="Auto-refresh (no manual inputs)")
    args = parser.parse_args()
    
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(extract(auto=args.auto))
