"""
session_extractor.py — One-time interactive session setup

Uses a VISIBLE browser (headless=False) for login — this always works.
After login, intercepts the real Blinkit API request to capture:
    - All session cookies
    - All custom Blinkit headers (auth_key, access_token, session_uuid, device_id, etc.)
    - user-agent

These are saved to session.json. The main tracker uses curl-cffi with
Chrome TLS impersonation + these exact headers — bypasses Cloudflare completely.
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Always resolve to project root regardless of where the script is run from
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BROWSER_PROFILE_DIR = _PROJECT_ROOT / "browser_profile"
SESSION_FILE = _PROJECT_ROOT / "session.json"
SEARCH_API_PATH = "/v1/layout/search"

# Headers we DON'T want to save (browser-managed, not replayable)
SKIP_HEADERS = {
    "accept-encoding", "connection", "content-length",
    "host", "origin", "sec-fetch-dest", "sec-fetch-mode",
    "sec-fetch-site",
}


def log(msg, tag="INFO"):
    syms = {"INFO": "ℹ️ ", "OK": "✅", "WARN": "⚠️ ", "ERR": "❌", "STEP": "👉", "DATA": "📦"}
    print(f"  {syms.get(tag, '   ')}  {msg}", flush=True)


async def extract_session(auto: bool = False):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log("Playwright not installed. Run: pip install playwright && playwright install chromium", "ERR")
        sys.exit(1)

    print("\n" + "═" * 62)
    print("  🎯 Blinkit Session Extractor")
    print("═" * 62 + "\n")
    if not auto:
        input("  Press Enter to open the browser → ")

    log("Starting Playwright...", "STEP")
    pw = await async_playwright().start()

    log(f"Launching browser: {BROWSER_PROFILE_DIR.resolve()}", "STEP")
    browser = await pw.chromium.launch_persistent_context(
        user_data_dir=str(BROWSER_PROFILE_DIR),
        headless=False,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
        ],
        ignore_default_args=["--enable-automation"],
        viewport={"width": 1280, "height": 800},
    )
    await browser.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3, 4, 5] });
        window.chrome = { runtime: {} };
    """)
    log(f"Browser launched. Pages: {len(browser.pages)}", "OK")

    page = browser.pages[0] if browser.pages else await browser.new_page()

    # ── Request interceptor — capture the real API headers ────────
    captured = {"headers": {}, "done": False}

    def on_request(req):
        if SEARCH_API_PATH in req.url and not captured["done"]:
            hdrs = dict(req.headers)
            captured["headers"] = {
                k: v for k, v in hdrs.items()
                if k.lower() not in SKIP_HEADERS
            }
            captured["done"] = True
            log(f"Intercepted: {req.url[:70]}", "OK")
            log(f"Headers captured: {list(captured['headers'].keys())}", "DATA")

    page.on("request", on_request)
    log("Request interceptor live", "OK")

    log("Loading blinkit.com...", "STEP")
    try:
        await page.goto("https://blinkit.com/", timeout=30_000)
        log("blinkit.com loaded", "OK")
    except Exception as e:
        log(f"Navigation warning: {e}", "WARN")

    print("\n" + "─" * 62)
    print("  👉 Browser is open. Log in with mobile + OTP.")
    print("  👉 Return here once you are on the Blinkit home page.")
    print("─" * 62)
    if auto:
        log("Auto mode — waiting 10s for page to settle...", "STEP")
        await page.wait_for_timeout(10000)
    else:
        input("\n  Press Enter AFTER you are fully logged in → ")

    log("Navigating to search page (this triggers the real API call)...", "STEP")
    try:
        await page.goto(
            "https://blinkit.com/s/?q=hot+wheels",
            timeout=30_000,
            wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(3000)
        log("Search page loaded", "OK")
    except Exception as e:
        log(f"Search navigation warning: {e}", "WARN")

    if not captured["done"]:
        log("API request not intercepted yet — waiting 3s more...", "WARN")
        await page.wait_for_timeout(3000)

    if not captured["done"]:
        log("Still no intercept — will save cookies only (headers empty)", "WARN")

    # ── Extract cookies ───────────────────────────────────────────
    log("Extracting cookies...", "STEP")
    raw_cookies = await browser.cookies("https://blinkit.com")
    cookies = {c["name"]: c["value"] for c in raw_cookies}
    log(f"Cookies: {list(cookies.keys())}", "DATA")

    ua = await page.evaluate("navigator.userAgent")
    log(f"User-Agent: {ua[:80]}", "DATA")

    # ── Validate from within the browser (real Chrome TLS) ────────
    log("Validating session from within browser...", "STEP")
    val_js = """
        async () => {
            try {
                const getCookie = (n) => {
                    const m = document.cookie.match('(^|;\\\\s*)' + n + '=([^;]*)');
                    return m ? decodeURIComponent(m[2]) : '';
                };
                const h = {
                    'content-type': 'application/json',
                    'lat': '12.956695', 'lon': '77.7000965',
                    'app_client': 'consumer_web',
                    'web_app_version': '1000076',
                };
                const at = getCookie('gr_1_accessToken');
                const di = getCookie('gr_1_deviceId');
                if (at) h['access_token'] = at;
                if (di) h['device_id'] = di;
                const r = await fetch(
                    'https://blinkit.com/v1/layout/search?q=hot+wheels&search_type=type_to_search',
                    { method: 'POST', credentials: 'include', headers: h }
                );
                const data = await r.json();
                return {
                    ok: r.ok, status: r.status,
                    is_success: data.is_success,
                    snippets: ((data.response || {}).snippets || []).length,
                };
            } catch(e) { return { ok: false, status: 0, error: e.toString() }; }
        }
    """
    result = await page.evaluate(val_js)
    log(f"Validation: {result}", "DATA")

    # ── Save everything ───────────────────────────────────────────
    data = {
        "cookies": cookies,
        "api_headers": captured["headers"],   # real browser headers for curl-cffi
        "user_agent": ua,
        "validated": result.get("ok", False),
        "saved_at": time.time(),
    }
    SESSION_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    log("Closing browser...", "STEP")
    await browser.close()
    await pw.stop()
    log("Browser closed", "OK")

    print("\n" + "═" * 62)
    if result.get("ok"):
        log(f"Session valid! {result.get('snippets', 0)} snippets returned", "OK")
        log(f"Saved: {len(cookies)} cookies + {len(captured['headers'])} API headers", "OK")
    else:
        log(f"Validation result: {result}", "WARN")
        log("Cookies saved — tracker will try anyway", "WARN")

    print("\n  Run the tracker:")
    print("  python tracker/main.py --dry-run   # test")
    print("  python tracker/main.py              # live")
    print("═" * 62 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto", action="store_true", help="Auto-refresh (no manual inputs)")
    args = parser.parse_args()
    
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(extract_session(auto=args.auto))
