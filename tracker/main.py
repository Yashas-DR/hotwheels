"""
main.py — Blinkit Hot Wheels Tracker — Entry Point

State machine:
    STARTING → RUNNING → PAUSED (session expired) → RUNNING (after refresh)
                       ↘ STOPPED (Ctrl+C)

Scan cycle (per iteration):
    For each location (sequential):
        For each query:
            Paginate through search results
            Fuzzy match against watchlist
            Alert on new matches (Telegram + sound)
        Wait location_gap seconds
    Wait scan_interval seconds
    Check if session refresh is due

Anti-detection enforced:
    ✅ Zero parallel requests
    ✅ Hard rate cap (RateLimiter)
    ✅ Random jitter between all requests
    ✅ Randomized session refresh (not fixed interval)

Run modes:
    python main.py              — normal mode
    python main.py --dry-run    — one cycle, no alerts, print results
    python main.py --once       — one full scan cycle, then exit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table
from rich import box

# ── Path setup ──────────────────────────────────────────────────
# Allow running from project root: python tracker/main.py
sys.path.insert(0, str(Path(__file__).parent))

from core.rate_limiter import RateLimiter
from core.session_mgr import SessionManager
from core.api import BlinkitClient
from core.matcher import match_products, MatchResult
from core.locations import load_locations, Location
from core.connectivity import connectivity
from alerts.telegram_bot import TelegramAlerter
from alerts.sound_alert import SoundAlerter

# Instamart (Swiggy) — optional, isolated
try:
    from instamart.session import InstaSession
    from instamart.api import InstaClient
    _INSTAMART_AVAILABLE = True
except ImportError:
    _INSTAMART_AVAILABLE = False

# BigBasket — optional, isolated
try:
    from bigbasket.session import BBSession
    from bigbasket.api import BBClient
    _BIGBASKET_AVAILABLE = True
except ImportError:
    _BIGBASKET_AVAILABLE = False

# FirstCry — optional, isolated
try:
    from firstcry.session import FCSession
    from firstcry.api import FCClient
    _FIRSTCRY_AVAILABLE = True
except ImportError:
    _FIRSTCRY_AVAILABLE = False

# Zepto — optional, isolated
try:
    from zepto.session import ZeptoSession
    from zepto.api import ZeptoClient
    _ZEPTO_AVAILABLE = True
except ImportError:
    _ZEPTO_AVAILABLE = False

console = Console()

SEEN_PRODUCTS_FILE = Path("seen_products.json")
CONFIG_FILE = Path("config.yaml")
LOGS_DIR = Path("logs")


def _make_location(cfg: dict) -> Optional[Location]:
    """Convert an instamart location config dict to a Location object."""
    try:
        return Location(
            name=cfg.get("name", "Unknown"),
            lat=float(cfg["lat"]),
            lng=float(cfg["lng"]),
        )
    except (KeyError, TypeError, ValueError) as e:
        logger.warning("Invalid instamart location config %s: %s", cfg, e)
        return None



# ─────────────────────────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────────────────────────

def setup_logging(config: dict) -> None:
    log_cfg = config.get("logging", {})
    level_str = log_cfg.get("level", "INFO").upper()
    level = getattr(logging, level_str, logging.INFO)

    handlers = [RichHandler(console=console, rich_tracebacks=True, show_path=False)]

    if log_cfg.get("log_to_file", True):
        LOGS_DIR.mkdir(exist_ok=True)
        log_file = LOGS_DIR / log_cfg.get("log_file", "logs/tracker.log").split("/")[-1]
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        handlers.append(fh)

    logging.basicConfig(
        level=level,
        handlers=handlers,
        format="%(message)s",
        datefmt="[%H:%M:%S]",
    )

logger = logging.getLogger("tracker")


# ─────────────────────────────────────────────────────────────────
# Seen products dedup cache
# ─────────────────────────────────────────────────────────────────

class SeenProducts:
    """
    In-memory + file-backed dedup cache for matched products.
    Prevents re-alerting for the same product within the TTL window.
    """

    def __init__(self, ttl_minutes: int = 120):
        self.ttl = timedelta(minutes=ttl_minutes)
        self._seen: dict[str, float] = {}  # key → timestamp
        self._load()

    def _cache_key(self, product_name: str, location_name: str) -> str:
        return f"{location_name}::{product_name.lower().strip()}"

    def is_new(self, product_name: str, location_name: str) -> bool:
        """Return True if this product hasn't been seen recently."""
        key = self._cache_key(product_name, location_name)
        ts = self._seen.get(key)
        if ts is None:
            return True
        age = timedelta(seconds=time.time() - ts)
        return age > self.ttl

    def mark_seen(self, product_name: str, location_name: str) -> None:
        key = self._cache_key(product_name, location_name)
        self._seen[key] = time.time()
        self._save()

    def _purge_expired(self) -> None:
        """Remove entries older than TTL."""
        cutoff = time.time() - self.ttl.total_seconds()
        self._seen = {k: v for k, v in self._seen.items() if v > cutoff}

    def _save(self) -> None:
        self._purge_expired()
        try:
            SEEN_PRODUCTS_FILE.write_text(
                json.dumps(self._seen, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.debug("Could not save seen_products.json: %s", e)

    def _load(self) -> None:
        if not SEEN_PRODUCTS_FILE.exists():
            return
        try:
            self._seen = json.loads(SEEN_PRODUCTS_FILE.read_text(encoding="utf-8"))
            self._purge_expired()
        except Exception:
            self._seen = {}


# ─────────────────────────────────────────────────────────────────
# Main Tracker
# ─────────────────────────────────────────────────────────────────

class HotWheelsTracker:
    """
    Main tracker orchestrator.

    Runs the scan loop, coordinates all components, and manages state.
    """

    def __init__(self, config: dict, dry_run: bool = False, only_platforms: list = None, only_locations: list = None):
        self.config = config
        self.dry_run = dry_run
        self._only = {p.lower() for p in (only_platforms or [])}
        self._only_locs = {loc.lower() for loc in (only_locations or [])}

        timing = config.get("timing", {})
        self.scan_min = timing.get("scan_interval_min", 35)
        self.scan_max = timing.get("scan_interval_max", 75)
        self.loc_gap_min = timing.get("location_gap_min", 5)
        self.loc_gap_max = timing.get("location_gap_max", 12)

        limits = config.get("limits", {})
        max_rpm = limits.get("max_requests_per_minute", 20)

        matching = config.get("matching", {})
        self.fuzzy_threshold = matching.get("fuzzy_threshold", 65)
        dedup_ttl = matching.get("dedup_ttl_minutes", 120)

        search_cfg = config.get("search", {})
        self.queries: list[str] = search_cfg.get("queries", ["hot wheels"])
        self.watchlist: list[str] = config.get("watchlist", [])

        all_locations = load_locations(config)
        if self._only_locs:
            self.locations = [loc for loc in all_locations if loc.name.lower() in self._only_locs]
        else:
            self.locations = all_locations
        self.rate_limiter = RateLimiter(max_per_minute=max_rpm)
        self.session_mgr = SessionManager(config)
        self.telegram = TelegramAlerter(config)
        self.sound = SoundAlerter(config)
        self.seen = SeenProducts(ttl_minutes=dedup_ttl)

        # Attach alerter + sound to the connectivity monitor (module-level singleton)
        connectivity.attach_alerter(self.telegram)
        connectivity.attach_sound(self.sound)

        # ── Instamart (optional, isolated) ──────────────────────
        instamart_cfg = config.get("instamart", {})
        self.instamart_enabled = (
            _INSTAMART_AVAILABLE and instamart_cfg.get("enabled", False)
            and (not self._only or "instamart" in self._only)
        )
        self.insta_session: Optional["InstaSession"] = None
        self.insta_locations: list[dict] = []
        self.insta_watchlist: list[str] = []  # combined main + extras
        if self.instamart_enabled:
            self.insta_session = InstaSession(config)
            all_insta = instamart_cfg.get("locations", [])
            if self._only_locs:
                self.insta_locations = [loc for loc in all_insta if loc.get("name", "").lower() in self._only_locs]
            else:
                self.insta_locations = all_insta
            extra = [w.lower() for w in instamart_cfg.get("additional_watchlist", [])]
            # Union: main watchlist + Instamart extras, deduplicated, order preserved
            seen_w: set[str] = set(self.watchlist)
            self.insta_watchlist = list(self.watchlist) + [w for w in extra if w not in seen_w]

        # ── BigBasket (optional, isolated) ──────────────────────
        bb_cfg = config.get("bigbasket", {})
        self.bigbasket_enabled = (
            _BIGBASKET_AVAILABLE and bb_cfg.get("enabled", False)
            and (not self._only or "bigbasket" in self._only)
        )
        # List of (location_name, BBSession) — one per address in config
        self.bb_locations: list[tuple[str, "BBSession"]] = []
        self.bb_watchlist: list[str] = []
        if self.bigbasket_enabled:
            extra_bb = [w.lower() for w in bb_cfg.get("additional_watchlist", [])]
            seen_bb: set[str] = set(self.watchlist)
            self.bb_watchlist = list(self.watchlist) + [w for w in extra_bb if w not in seen_bb]
            for loc_cfg in bb_cfg.get("locations", []):
                sf = loc_cfg.get("session_file", "bigbasket_session.json")
                name = loc_cfg.get("name", sf)
                if not self._only_locs or name.lower() in self._only_locs:
                    self.bb_locations.append((name, BBSession(config, session_file_override=sf)))
            # Fallback: if no locations list, use legacy session_file key
            if not self.bb_locations and not self._only_locs:
                sf = bb_cfg.get("session_file", "bigbasket_session.json")
                self.bb_locations.append(("BigBasket", BBSession(config, session_file_override=sf)))

        # ── FirstCry (optional, isolated) ───────────────────────
        fc_cfg = config.get("firstcry", {})
        self.firstcry_enabled = (
            _FIRSTCRY_AVAILABLE and fc_cfg.get("enabled", False)
            and (not self._only or "firstcry" in self._only)
        )
        # Single session; list of (location_name, pincode) pairs
        self.fc_session: Optional["FCSession"] = None
        self.fc_locations: list[tuple[str, str]] = []  # (name, pincode)
        self.fc_watchlist: list[str] = []
        if self.firstcry_enabled:
            extra_fc = [w.lower() for w in fc_cfg.get("additional_watchlist", [])]
            seen_fc: set[str] = set(self.watchlist)
            self.fc_watchlist = list(self.watchlist) + [w for w in extra_fc if w not in seen_fc]
            self.fc_session = FCSession(config)
            for loc_cfg in fc_cfg.get("locations", []):
                name = loc_cfg.get("name", "FirstCry")
                pincode = str(loc_cfg.get("pincode", "")).strip()
                if pincode:
                    if not self._only_locs or name.lower() in self._only_locs:
                        self.fc_locations.append((name, pincode))
            if not self.fc_locations:
                logger.warning("FirstCry enabled but no locations configured — add pincodes to config.yaml")
                self.firstcry_enabled = False

        # ── Zepto (optional, isolated) ───────────────────────────
        z_cfg = config.get("zepto", {})
        self.zepto_enabled = (
            _ZEPTO_AVAILABLE and z_cfg.get("enabled", False)
            and (not self._only or "zepto" in self._only)
        )
        # Single session shared across all locations
        self.zepto_session: Optional["ZeptoSession"] = None
        self.zepto_locations: list[str] = []          # list of location names
        self.zepto_watchlist: list[str] = []
        if self.zepto_enabled:
            extra_z = [w.lower() for w in z_cfg.get("additional_watchlist", [])]
            seen_z: set[str] = set(self.watchlist)
            self.zepto_watchlist = list(self.watchlist) + [w for w in extra_z if w not in seen_z]
            self.zepto_session = ZeptoSession(config)
            for loc_cfg in z_cfg.get("locations", []):
                name = loc_cfg.get("name", "").strip()
                if name:
                    if not self._only_locs or name.lower() in self._only_locs:
                        self.zepto_locations.append(name)
            if not self.zepto_locations:
                logger.warning("Zepto enabled but no locations configured in config.yaml")
                self.zepto_enabled = False

        self._running = False
        self._cycle_count = 0
        self._total_matches = 0

    async def start(self, run_once: bool = False) -> None:
        """Start the tracking loop."""
        self._running = True

        console.rule("[bold green]🎯 Hot Wheels Tracker Starting[/bold green]")
        logger.info("Watchlist: %s", self.watchlist)
        logger.info("Locations: %s", [str(l) for l in self.locations])
        logger.info("Queries: %s", self.queries)
        logger.info("Dry run: %s", self.dry_run)

        if not self.watchlist:
            logger.error("Watchlist is empty! Add model names to config.yaml → watchlist")
            return

        # ── Blinkit session ────────────────────────────────────
        valid = await self.session_mgr.start(headless=True)
        if not valid:
            logger.error(
                "Cannot start — Blinkit session invalid. "
                "Run: python tracker/tools/session_extractor.py"
            )
            await self.session_mgr.stop()
            return

        # ── Instamart session (non-fatal) ──────────────────────
        if self.instamart_enabled and self.insta_session:
            try:
                ok = await self.insta_session.start()
                if ok:
                    logger.info(
                        "🟠 Instamart ready — watchlist: %d items (%d main + %d extras)",
                        len(self.insta_watchlist),
                        len(self.watchlist),
                        len(self.insta_watchlist) - len(self.watchlist),
                    )
                else:
                    logger.warning(
                        "🟠 Instamart session failed to load — "
                        "Instamart scanning DISABLED for this run. "
                        "Blinkit continues normally."
                    )
                    self.instamart_enabled = False
            except Exception as e:
                logger.warning("🟠 Instamart setup error (non-fatal): %s", e)
                self.instamart_enabled = False


        # ── BigBasket sessions (non-fatal, per location) ────────────
        if self.bigbasket_enabled and self.bb_locations:
            ready: list[tuple[str, "BBSession"]] = []
            for loc_name, sess in self.bb_locations:
                try:
                    ok = await sess.start()
                    if ok:
                        ready.append((loc_name, sess))
                        logger.info("🟢 BigBasket [%s] session ready", loc_name)
                    else:
                        logger.warning(
                            "🟢 BigBasket [%s] session failed — skipping this location", loc_name
                        )
                except Exception as e:
                    logger.warning("🟢 BigBasket [%s] setup error: %s", loc_name, e)
            self.bb_locations = ready  # keep only valid sessions
            if ready:
                logger.info(
                    "🟢 BigBasket ready — %d/%d locations, watchlist: %d items",
                    len(ready), len(self.bb_locations) + (len(self.bb_locations) - len(ready)),
                    len(self.bb_watchlist),
                )
            else:
                logger.warning("🟢 BigBasket: no valid sessions — scanning disabled")
                self.bigbasket_enabled = False

        # ── FirstCry session (non-fatal, single session) ─────────────
        if self.firstcry_enabled and self.fc_session:
            try:
                ok = await self.fc_session.start()
                if ok:
                    logger.info(
                        "🔵 FirstCry ready — %d location(s), watchlist: %d items",
                        len(self.fc_locations), len(self.fc_watchlist),
                    )
                else:
                    logger.warning(
                        "🔵 FirstCry session failed to load — "
                        "FirstCry scanning DISABLED for this run. "
                        "Run: python tracker/tools/firstcry_extractor.py"
                    )
                    self.firstcry_enabled = False
            except Exception as e:
                logger.warning("🔵 FirstCry setup error (non-fatal): %s", e)
                self.firstcry_enabled = False

        # ── Zepto session (non-fatal, single session) ─────────────────
        if self.zepto_enabled and self.zepto_session:
            try:
                ok = await self.zepto_session.start()
                if ok:
                    logger.info(
                        "🟣 Zepto ready — %d location(s), watchlist: %d items",
                        len(self.zepto_locations), len(self.zepto_watchlist),
                    )
                else:
                    logger.warning(
                        "🟣 Zepto session failed to load — "
                        "Zepto scanning DISABLED for this run. "
                        "Run: python tracker/tools/zepto_extractor.py"
                    )
                    self.zepto_enabled = False
            except Exception as e:
                logger.warning("🟣 Zepto setup error (non-fatal): %s", e)
                self.zepto_enabled = False

        try:
            # Main loop
            async with BlinkitClient(self.session_mgr, self.rate_limiter, self.config) as client:
                while self._running:
                    try:
                        await self._run_cycle(client)
                        self._cycle_count += 1

                        if run_once:
                            break

                        # Check if session refresh is due
                        if self.session_mgr.needs_refresh():
                            await self._refresh_session()

                        # Sleep between cycles (random jitter)
                        sleep_for = random.uniform(self.scan_min, self.scan_max)
                        next_at = datetime.now() + timedelta(seconds=sleep_for)
                        logger.info(
                            "💤 Sleeping %.0fs — next scan at %s",
                            sleep_for,
                            next_at.strftime("%H:%M:%S"),
                        )
                        await asyncio.sleep(sleep_for)

                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        logger.error("Unexpected error in scan cycle: %s", e, exc_info=True)
                        await asyncio.sleep(30)
        finally:
            # Always close the browser cleanly
            await self.session_mgr.stop()

        console.rule("[bold red]🛑 Tracker Stopped[/bold red]")
        logger.info(
            "Total cycles: %d | Total matches: %d | Total requests: %d",
            self._cycle_count,
            self._total_matches,
            self.rate_limiter.stats["total_requests"],
        )

    async def _fire_alerts(
        self,
        matches: list[tuple],
        cycle_matches: list[tuple],
        platform: str,
    ) -> None:
        """Immediately fire Telegram + sound for any NEW matches in `matches`.
        Also appends them to cycle_matches for the end-of-cycle table."""
        for match, location in matches:
            cycle_matches.append((match, location, platform))
            if self.seen.is_new(match.product_name, location.name):
                self._total_matches += 1
                if not self.dry_run:
                    self.seen.mark_seen(match.product_name, location.name)
                logger.info(
                    "🔥 NEW MATCH [%s]: '%s' → '%s' (%.0f%%) @ %s",
                    platform,
                    match.product_name[:50],
                    match.watchlist_target,
                    match.score,
                    location.name,
                )
                if not self.dry_run:
                    await asyncio.gather(
                        self.telegram.send_product_alert(
                            product_name=match.product_name,
                            watchlist_target=match.watchlist_target,
                            match_score=match.score,
                            location_name=location.name,
                            price=match.price,
                            platform=platform,
                        ),
                        self.sound.play(),
                    )

    async def _run_cycle(self, client: BlinkitClient) -> None:
        """Run one full scan cycle across all locations and queries."""
        cycle_start = time.monotonic()
        cycle_matches: list[tuple[MatchResult, Location, str]] = []

        # ── BLINKIT ────────────────────────────────────────────────
        if not self._only or "blinkit" in self._only:
            try:
                for i, location in enumerate(self.locations):
                    logger.info("─── 📍 [Blinkit] %s (%d/%d)", location.name, i + 1, len(self.locations))

                    for query in self.queries:
                        products = await client.search_all_pages(query=query, location=location)
                        matches = match_products(
                            products=products,
                            watchlist=self.watchlist,
                            threshold=self.fuzzy_threshold,
                        )
                        await self._fire_alerts(
                            [(m, location) for m in matches], cycle_matches, "Blinkit"
                        )

                    if i < len(self.locations) - 1:
                        gap = random.uniform(self.loc_gap_min, self.loc_gap_max)
                        await asyncio.sleep(gap)

            except Exception as e:
                logger.error("Blinkit scan error: %s", e, exc_info=True)


        # ── BIGBASKET (completely isolated, per location) ──────────
        if self.bigbasket_enabled and self.bb_locations:
            for loc_idx, (loc_name, bb_sess) in enumerate(self.bb_locations):
                # Cooldown between locations — BB rate-limits same-account rapid requests
                if loc_idx > 0:
                    gap = random.uniform(15.0, 20.0)
                    logger.debug("🟢 BigBasket inter-location cooldown %.0fs", gap)
                    await asyncio.sleep(gap)

                try:
                    async with BBClient(bb_sess, self.rate_limiter, self.config) as bb:
                        for i, query in enumerate(self.queries):
                            logger.info("─── 🟢 [BigBasket] %s | query='%s'", loc_name, query)
                            products = await bb.search_all_pages(
                                query=query, location_name=loc_name
                            )
                            matches = match_products(
                                products=products,
                                watchlist=self.bb_watchlist,
                                threshold=self.fuzzy_threshold,
                            )
                            bb_loc = Location(name=loc_name, lat=0.0, lng=0.0)
                            await self._fire_alerts(
                                [(m, bb_loc) for m in matches], cycle_matches, "BigBasket"
                            )

                            if i < len(self.queries) - 1:
                                gap = random.uniform(self.loc_gap_min, self.loc_gap_max)
                                await asyncio.sleep(gap)

                except Exception as e:
                    logger.error(
                        "🟢 BigBasket [%s] scan error (other platforms unaffected): %s",
                        loc_name, e, exc_info=True,
                    )

        # ── FIRSTCRY (completely isolated, per pincode) ─────────────
        if self.firstcry_enabled and self.fc_session and self.fc_session.is_valid:
            try:
                async with FCClient(self.fc_session, self.rate_limiter, self.config) as fc:
                    for i, (loc_name, pincode) in enumerate(self.fc_locations):
                        if i > 0:
                            gap = random.uniform(8.0, 15.0)
                            logger.debug("🔵 FirstCry inter-pincode cooldown %.0fs", gap)
                            await asyncio.sleep(gap)

                        logger.info(
                            "─── 🔵 [FirstCry] %s (pincode=%s)", loc_name, pincode
                        )
                        products = await fc.search_all_pages(
                            query="hot wheels",
                            pincode=pincode,
                            location_name=loc_name,
                        )
                        matches = match_products(
                            products=products,
                            watchlist=self.fc_watchlist,
                            threshold=self.fuzzy_threshold,
                        )
                        fc_loc = Location(name=loc_name, lat=0.0, lng=0.0)
                        await self._fire_alerts(
                            [(m, fc_loc) for m in matches], cycle_matches, "FirstCry"
                        )

            except Exception as e:
                logger.error(
                    "🔵 FirstCry scan error (other platforms unaffected): %s",
                    e, exc_info=True,
                )

        if self.firstcry_enabled and self.fc_session and not self.fc_session.is_valid:
            if not await self._auto_recover_platform("firstcry", self.fc_session):
                self.firstcry_enabled = False

        # ── ZEPTO (completely isolated) ────────────────────────────────
        if self.zepto_enabled and self.zepto_session and self.zepto_session.is_valid:
            try:
                async with ZeptoClient(self.zepto_session, self.rate_limiter, self.config) as zc:
                    for i, loc_name in enumerate(self.zepto_locations):
                        if i > 0:
                            gap = random.uniform(1.5, 3.0)
                            await asyncio.sleep(gap)

                        logger.info(
                            "─── 🟣 [Zepto] %s (%d/%d)",
                            loc_name, i + 1, len(self.zepto_locations),
                        )
                        products = await zc.search_all_pages(
                            query="hot wheels",
                            location_name=loc_name,
                        )
                        matches = match_products(
                            products=products,
                            watchlist=self.zepto_watchlist,
                            threshold=self.fuzzy_threshold,
                        )
                        z_loc = Location(name=loc_name, lat=0.0, lng=0.0)
                        await self._fire_alerts(
                            [(m, z_loc) for m in matches], cycle_matches, "Zepto"
                        )

            except Exception as e:
                logger.error(
                    "🟣 Zepto scan error (other platforms unaffected): %s",
                    e, exc_info=True,
                )

        if self.zepto_enabled and self.zepto_session and not self.zepto_session.is_valid:
            if not await self._auto_recover_platform("zepto", self.zepto_session):
                self.zepto_enabled = False

        # ── INSTAMART (completely isolated) ───────────────────────
        if self.instamart_enabled and self.insta_session and self.insta_session.is_valid:
            try:
                async with InstaClient(self.insta_session, self.rate_limiter, self.config) as insta:
                    for i, loc_cfg in enumerate(self.insta_locations):
                        # Build a minimal Location-like object
                        loc = _make_location(loc_cfg)
                        if loc is None:
                            continue

                        # Read primary and fallbacks from config manually
                        primary_sid = loc_cfg.get("store_id")
                        primary_sec = str(loc_cfg.get("secondary_store_id", "") or "")
                        fallbacks = loc_cfg.get("fallback_store_ids", [])
                        
                        all_combinations = [(primary_sid, primary_sec)]
                        if isinstance(fallbacks, list):
                            for fb in fallbacks:
                                all_combinations.append((fb.get("store_id"), str(fb.get("secondary_store_id", "") or "")))

                        for combo_idx, (sid, sec) in enumerate(all_combinations):
                            if sid is None:
                                continue
                                
                            logger.info(
                                "─── 📍 [Instamart] %s (%d/%d)%s store=%s",
                                loc.name, i + 1, len(self.insta_locations),
                                f" [Fallback {combo_idx}]" if combo_idx > 0 else "",
                                sid,
                            )

                            store_found_items = False
                            for query in self.queries:
                                products = await insta.search_all_pages(
                                    query=query,
                                    location=loc,
                                    store_id=sid,
                                    secondary_store_id=sec
                                )
                                if products:
                                    store_found_items = True

                                matches = match_products(
                                    products=products,
                                    watchlist=self.insta_watchlist,
                                    threshold=self.fuzzy_threshold,
                                )
                                await self._fire_alerts(
                                    [(m, loc) for m in matches], cycle_matches, "Instamart"
                                )
                                
                            # If this store combination successfully found ANY hot wheels products,
                            # we stop testing fallbacks for this location!
                            if store_found_items:
                                break
                            elif combo_idx < len(all_combinations) - 1:
                                logger.info("      └─ Primary yielded 0 items, trying fallback...")
                                await asyncio.sleep(1.5)
                                
                        if i < len(self.insta_locations) - 1:
                            gap = random.uniform(self.loc_gap_min, self.loc_gap_max)
                            await asyncio.sleep(gap)

            except Exception as e:
                logger.error(
                    "🟠 Instamart scan error (Blinkit unaffected): %s", e, exc_info=True
                )

        if self.instamart_enabled and self.insta_session and not self.insta_session.is_valid:
            if not await self._auto_recover_platform("instamart", self.insta_session):
                self.instamart_enabled = False

        # Display end-of-cycle summary table
        if cycle_matches:
            # new_matches are already tracked/alerted — just show the table
            new_keys = {(m.product_name, l.name) for m, l, _ in cycle_matches
                        if not self.seen.is_new(m.product_name, l.name)}
            self._print_matches_table(cycle_matches, 
                [(m, l, p) for m, l, p in cycle_matches if (m.product_name, l.name) not in new_keys])
        else:
            logger.info("No matches this cycle.")

        elapsed = time.monotonic() - cycle_start
        logger.info(
            "Cycle #%d done in %.1fs — %d match(es)",
            self._cycle_count + 1,
            elapsed,
            len(cycle_matches),
        )

    def _print_matches_table(
        self,
        all_matches: list[tuple[MatchResult, Location, str]],
        new_matches: list[tuple[MatchResult, Location, str]],
    ) -> None:
        """Print a rich table of all matches found this cycle."""
        new_keys = {(m.product_name, l.name) for m, l, _ in new_matches}

        table = Table(
            title=f"Matches — Cycle #{self._cycle_count + 1}",
            box=box.ROUNDED,
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Product", style="white", max_width=40)
        table.add_column("Target", style="yellow")
        table.add_column("Score", justify="right")
        table.add_column("Location", style="cyan")
        table.add_column("Platform", style="magenta")
        table.add_column("Status", justify="center")

        for match, loc, platform in all_matches:
            is_new = (match.product_name, loc.name) in new_keys
            status = "[bold green]NEW ✨[/bold green]" if is_new else "[dim]seen[/dim]"
            score_color = "green" if match.score >= 80 else "yellow" if match.score >= 65 else "red"
            plat_color = (
                "bright_green"  if platform == "Blinkit"
                else "bright_yellow" if platform == "Instamart"
                else "cyan"         if platform == "BigBasket"
                else "blue"         # FirstCry
            )
            table.add_row(
                match.product_name[:39],
                match.watchlist_target,
                f"[{score_color}]{match.score:.0f}%[/{score_color}]",
                loc.name,
                f"[{plat_color}]{platform}[/{plat_color}]",
                status,
            )

        console.print(table)

    async def _ensure_session(self) -> None:
        """Load saved session or run a fresh refresh."""
        loaded = await self.session_mgr.load_from_file()

        if loaded:
            logger.info("Validating existing session...")
            valid = await self.session_mgr.validate()
            if valid:
                logger.info("✅ Existing session is valid.")
                return
            logger.warning("Saved session is no longer valid — refreshing...")

        valid = await self.session_mgr.refresh(headless=True)
        if not valid:
            logger.warning(
                "Auto-refresh failed. You may need to re-run the session extractor:\n"
                "  python tracker/tools/session_extractor.py"
            )

    async def _refresh_session(self) -> None:
        """Periodically refresh session. Navigates in existing browser."""
        logger.info("⏰ Scheduled session refresh...")
        valid = await self.session_mgr.refresh(headless=True)

        if not valid:
            logger.warning("❌ Blinkit session refresh failed — session has expired.")
            if getattr(self, "telegram", None):
                await self.telegram.send_session_expired_alert(platform="Blinkit")

            logger.warning("⏸  Pausing scans. Retrying refresh every 60s...")
            while not self.session_mgr.is_valid and self._running:
                await asyncio.sleep(60)
                logger.info("Retrying session refresh...")
                await self.session_mgr.refresh(headless=True)

            if self.session_mgr.is_valid:
                logger.info("▶️  Session recovered — resuming scans.")
        else:
            logger.info("✅ Session refreshed successfully.")

    async def _auto_recover_platform(self, platform_name: str, session_obj: any) -> bool:
        """Spawn the background extractor to recover an expired session (WAF block / token expiry)."""
        logger.warning("🔄 %s session invalid. Attempting background auto-recovery...", platform_name.title())
        try:
            import sys
            import os
            from pathlib import Path
            
            # Subprocess needs to know how to print unicode when capturing pipes on Windows
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            
            script_path = str(Path(__file__).resolve().parent / "tools" / "refresh_sessions.py")
            
            proc = await asyncio.create_subprocess_exec(
                sys.executable, script_path, "--auto", "--only", platform_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                logger.info("✅ %s background auto-recovery completed. Reloading session...", platform_name.title())
                # Reload session
                ok = await session_obj.start()
                if ok and session_obj.is_valid:
                    logger.info("▶️ %s session recovered successfully!", platform_name.title())
                    return True
            
            err_text = ""
            if stderr:
                err_text = f" | STDERR: {stderr.decode('utf-8', errors='ignore').strip()}"
            logger.error("❌ Auto-recovery failed for %s. return_code=%s%s", platform_name, proc.returncode, err_text)
        except Exception as e:
            logger.error("❌ Auto-recovery exception for %s: %s", platform_name, e)
        return False

    def stop(self) -> None:
        self._running = False


# ─────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────

def load_config(path: Path = CONFIG_FILE) -> dict:
    if not path.exists():
        print(f"❌ Config file not found: {path}")
        print("   Make sure you run this from the hotwheels/ project root.")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Blinkit Hot Wheels Tracker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tracker/main.py                # Normal continuous mode
  python tracker/main.py --dry-run      # One cycle, no alerts
  python tracker/main.py --once         # One cycle, with alerts
  python tracker/main.py --config custom.yaml
        """,
    )
    parser.add_argument("--dry-run", action="store_true", help="Run one cycle without sending alerts")
    parser.add_argument("--once", action="store_true", help="Run one cycle with alerts, then exit")
    parser.add_argument("--config", default="tracker/config.yaml", help="Path to config.yaml")
    parser.add_argument(
        "--only", nargs="+",
        metavar="PLATFORM",
        help="Only scan these platforms, e.g.: --only instamart  or  --only blinkit zepto"
    )
    parser.add_argument(
        "--location", nargs="+",
        metavar="LOC_NAME",
        help="Only scan these specific locations (by name), e.g.: --location abdulee Soundary"
    )
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()

    # Change working dir to tracker/ so relative paths work
    tracker_dir = Path(__file__).parent
    os.chdir(tracker_dir)

    config_path = Path(args.config)
    if not config_path.is_absolute():
        # Try relative to project root
        project_root = tracker_dir.parent
        config_path = project_root / args.config
        if not config_path.exists():
            config_path = tracker_dir / "config.yaml"

    config = load_config(config_path)
    setup_logging(config)

    tracker = HotWheelsTracker(
        config=config,
        dry_run=args.dry_run,
        only_platforms=args.only,
        only_locations=args.location
    )

    try:
        await tracker.start(run_once=args.dry_run or args.once)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user (Ctrl+C)[/yellow]")
        tracker.sound.stop_all()   # stop any looping audio immediately
        tracker.stop()


if __name__ == "__main__":
    asyncio.run(async_main())
