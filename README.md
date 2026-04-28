# 🏎️ Hot Wheels Universal Tracker

An ultra-resilient, asynchronous inventory tracker that monitors Hot Wheels stock across **five** major quick-commerce and e-commerce platforms in India: **Blinkit, Swiggy Instamart, BigBasket, FirstCry, and Zepto**.

This system is built to bypass enterprise-grade bot protection (AWS WAF, Cloudflare, Akamai) by leveraging real browser contexts to harvest session tokens, combined with `curl_cffi` to execute cryptographically precise TLS-impersonated API calls at scale.

---

## 🏗️ Core Architecture & Philosophy

Modern e-commerce platforms actively block Python automation tools using sophisticated edge security. This tracker is built on a hybrid architecture to solve this:

1. **The Extraction Layer (Playwright)**: We use persistent Chromium profiles via Playwright to physically log into the services. This allows the browser to natively solve JS challenges, captchas, and WAF fingerprinting. We then extract the exact HTTP headers, Auth Tokens, AWS cookies, and CSRF secrets.
2. **The Execution Layer (`curl_cffi`)**: Traditional `requests` or `aiohttp` libraries have distinct SSL/TLS handshakes that are instantly flagged by Cloudflare/AWS. This tracker uses `curl_cffi` to mimic the exact structural TLS fingerprint of `Chrome 124`. Because we combine a mathematically perfect Chrome TLS handshake with the authentic headers captured in step 1, the edge servers believe the tracker is a legitimate browser.

---

## 📁 Repository Structure

```text
├── tracker/
│   ├── main.py                   # The core asynchronous infinite loop and orchestrator
│   ├── config.yaml               # Master configuration (Locations, Pincodes, Telegram API)
│   ├── core/                     
│   │   ├── rate_limiter.py       # State-aware token-bucket rate limiting to prevent bans
│   │   ├── utils.py              # Fuzzy string matching algorithm configuration
│   │   └── alert.py              # Telegram API integration and sound alerts
│   ├── tools/                    # Hardware-based UI extraction layer
│   │   ├── session_extractor.py  # Blinkit auth & header extraction
│   │   ├── instamart_extractor.py# Swiggy Instamart session baking
│   │   ├── bigbasket_extractor.py# BigBasket location bakes
│   │   ├── firstcry_extractor.py # Firstcry stateless auth extraction
│   │   ├── zepto_extractor.py    # Zepto CSRF and store_id extractions
│   │   └── refresh_sessions.py   # Bulk orchestration wizard for all extractors
│   ├── blinkit/                  # Blinkit search API and response parsing
│   ├── instamart/                # Swiggy API, 202 Bot-Challenge handling
│   ├── bigbasket/                # BigBasket domain-aware REST handlers
│   ├── firstcry/                 # FirstCry multi-pincode payload constructors
│   └── zepto/                    # Zepto token-refresh and geo-fenced API calls
└── README.md
```

---

## ⚙️ Platform-Specific Technical Details

Each platform operates on highly distinct architectures. Here is how this tracker handles them:

### 1. Blinkit
- **Infrastructure**: Cloudflare Zero Trust.
- **Mechanism**: Highly dependent on precise JSON payloads and `session_uuid` headers. 
- **Methodology**: `session_extractor.py` intercepts the live XHR API requests made by the frontend, capturing the exact dynamically generated headers. The tracker replays these over TLS impersonation.

### 2. Swiggy Instamart
- **Infrastructure**: AWS WAF.
- **Mechanism**: Strictly validates `aws-waf-token` and browser User-Agent integrity. If anomalies are detected, it returns an `HTTP 202 Accepted` response serving a HTML Bot-Challenge (forcing JS execution) instead of JSON.
- **Methodology**: The `instamart_extractor.py` extracts the `aws-waf-token`. Because Swiggy checks TLS hashes against the User-Agent, the tracker natively scrubs out old UAs from the session cache and allows `curl_cffi` to dynamically inject the exact UA that matches the fake TLS cipher suite.

### 3. Zepto
- **Infrastructure**: Custom React Native / Web Edge.
- **Mechanism**: Requires precise location-based `store_id` pairs and heavily cross-references JWT `accessToken` cookies with `x-xsrf-token` application headers.
- **Methodology**: The tracker automatically refreshes the short-lived `accessToken` using the persistent `refreshToken`. It leverages HAR data models to map arbitrary coordinates to backend `store_ids`.

### 4. BigBasket
- **Infrastructure**: Akamai / Custom WAF.
- **Mechanism**: Relies strongly on domain-grade cookies rather than authorization headers. Highly susceptible to IP-based rate limiting (`HTTP 429`).
- **Methodology**: Heavily relies on the `RateLimiter` class to inject jittered cooldowns between scans. Separate delivery addresses often require entirely separate `.json` session configurations.

### 5. FirstCry
- **Infrastructure**: Standard Web WAF.
- **Mechanism**: Stateless e-commerce catalog. Uses a singular authentication cookie (`_$FC_SID$`) alongside active user IDs.
- **Methodology**: The session only needs to be baked once. The tracker then rapidly rotates `pincode` parameters inside the POST payload to check inventory universally without needing multiple sessions.

---

## 🤖 The Auto-Recovery System

API tokens eventually burn out, and AWS WAF tokens frequently expire.

Historically, this would crash automated scripts entirely. **This tracker is completely self-healing.**

1. During the execution of `main.py`, if an HTTP handler receives an `HTTP 401`, `HTTP 403`, or Swiggy's `HTTP 202 Bot Challenge`, it toggles the internal `session.is_valid` state to `False`.
2. The core loop intercepts this state change mid-flight.
3. It immediately halts the scan for that specific platform and spawns a background asyncio subprocess: `python tracker/tools/refresh_sessions.py --auto --only <platform>`.
4. Playwright natively opens the specific platform in a headless state, allowing the site's JS engines to legitimately renegotiate the AWS WAF keys, JWT tokens, and CSRF secrets.
5. Since the `--auto` flag is passed, the script waits exactly 10-12 seconds without requiring any keyboard input, snapshots the new cookies/headers to the JSON cache, and closes.
6. `main.py` detects the successful subprocess exit, hot-reloads the new JSON into memory, dispatches a success alert to Telegram, and flawlessly resumes the scan loop.

---

## 💻 Setup & Installation Guide

### 1. Environment Requirements
Ensure you have Python 3.10+ installed. A virtual environment is highly recommended.

```bash
python -m venv venv
# Windows:
.\venv\Scripts\activate
# Mac/Linux:
source venv/bin/activate
```

### 2. Dependencies
Install the required network and automation frameworks:

```bash
pip install curl-cffi playwright rich thefuzz pyyaml
playwright install chromium
pip install pygame --quiet
```

### 3. Configuration (`config.yaml`)

You must construct your `tracker/config.yaml` file. This is the master ledger for the application.

```yaml
telegram:
  bot_token: "YOUR_TELEGRAM_BOT_TOKEN"
  chat_id: "YOUR_TELEGRAM_CHAT_ID"

settings:
  fuzzy_threshold: 80       # Matching strictness (0-100)
  loc_gap_min: 2.0          # Minimum jitter seconds between platforms
  loc_gap_max: 5.0          # Maximum jitter seconds

bigbasket:
  enabled: true
  locations:
    - name: "Home"
      session_file: "bigbasket_session.json"
# ... format continues for zepto, instamart, firstcry
```

### 4. Priming the System (Baking Sessions)

Before the headless tracker can run, you must securely bake your hardware sessions into the `browser_profile` cache. 

You can launch the orchestration wizard to authenticate across all platforms sequentially:

```bash
python tracker/tools/refresh_sessions.py
```

*For each platform, a visible browser will open. Log into the service via OTP, set your delivery coordinates to your desired location, search "Hot Wheels" to trigger the API interception, and press `ENTER` in your terminal to save.*

> **Tip**: Need to force-refresh all platforms simultaneously in the background without pressing enter? Run `python tracker/tools/refresh_sessions.py --auto`

### 5. Running the Tracker

**Validation Run (Single Pass):**
Test everything by running a single cycle. This will ping the servers once and terminate.
```bash
python tracker/main.py --once
```

**Live Infinite Tracking:**
```bash
python tracker/main.py
```
This will run infinitely. It orchestrates fuzzy matching, prints colorized console tables showing inventory percentages, triggers global OS sounds on matches, and seamlessly self-heals expired sessions in the background.

---

## ⚠️ Operation Warnings & Rate-limiting Safe Practices

1. **Aggression**: This software queries production systems. If you set your configuration jitter (`loc_gap_min`) too low, you **will** be permanently IP banned by Akamai.
2. **Fuzzy Matching**: Different apps call the same car different things (e.g., `"Hot Wheels Porsche 911"` vs `"Mattel Hotwheels 911 Porsche"`). The tracker utilizes the `thefuzz` token-sort-ratio algorithms to mathematically determine similarity instead of relying on exact regex patterns. Adjust `fuzzy_threshold` in your `config.yaml` to dial in accuracy.
3. **Storage**: Never commit `*_session.json` or `browser_profile` directories to version control. They contain hyper-sensitive persistent authentication tokens granting full access to your E-commerce profiles.
