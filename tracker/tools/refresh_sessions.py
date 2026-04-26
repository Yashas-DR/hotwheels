"""
refresh_sessions.py — Run all session extractors in one go (sequentially)

Usage:
    python tracker/tools/refresh_sessions.py               # refresh all 5
    python tracker/tools/refresh_sessions.py --skip blinkit
    python tracker/tools/refresh_sessions.py --only instamart bigbasket firstcry zepto

Each extractor opens ONE browser window at a time.
You interact with it, then press ENTER → it closes and the next one opens.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # tracker/
sys.path.insert(0, str(BASE_DIR))

# ── Colours (no deps) ───────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RED    = "\033[91m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def banner(text: str, color: str = BOLD) -> None:
    width = 62
    print()
    print(color + "═" * width + RESET)
    print(color + f"  {text}" + RESET)
    print(color + "═" * width + RESET)
    print()


def step(n: int, total: int, label: str, color: str = BOLD) -> None:
    print(color + f"\n  [{n}/{total}] {label}" + RESET)
    print()


ALL_PLATFORMS: list[str] = ["blinkit", "instamart", "bigbasket", "firstcry", "zepto"]


async def run_blinkit(auto: bool = False) -> bool:
    """Import and run the Blinkit session extractor inline."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "session_extractor",
            BASE_DIR / "tools" / "session_extractor.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        await mod.extract_session(auto=auto)
        return True
    except Exception as e:
        print(RED + f"  ❌ Blinkit extractor error: {e}" + RESET)
        return False


async def run_instamart(auto: bool = False) -> bool:
    """Import and run the Instamart session extractor inline."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "instamart_extractor",
            BASE_DIR / "tools" / "instamart_extractor.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # The instamart extractor uses extract() not main()
        await mod.extract(auto=auto)
        return True
    except Exception as e:
        print(RED + f"  ❌ Instamart extractor error: {e}" + RESET)
        return False


async def run_bigbasket(auto: bool = False) -> bool:
    """Import and run the BigBasket session extractor inline."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "bigbasket_extractor",
            BASE_DIR / "tools" / "bigbasket_extractor.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        await mod.main(auto=auto)
        return True
    except Exception as e:
        print(RED + f"  ❌ BigBasket extractor error: {e}" + RESET)
        return False


async def run_firstcry(auto: bool = False) -> bool:
    """Import and run the FirstCry session extractor inline."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "firstcry_extractor",
            BASE_DIR / "tools" / "firstcry_extractor.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        await mod.main(auto=auto)
        return True
    except Exception as e:
        print(RED + f"  ❌ FirstCry extractor error: {e}" + RESET)
        return False


async def run_zepto(auto: bool = False) -> bool:
    """Import and run the Zepto session extractor inline."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "zepto_extractor",
            BASE_DIR / "tools" / "zepto_extractor.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        await mod.main(auto=auto)
        return True
    except Exception as e:
        print(RED + f"  ❌ Zepto extractor error: {e}" + RESET)
        return False


RUNNERS = {
    "blinkit":   (run_blinkit,   "Blinkit",          GREEN),
    "instamart": (run_instamart, "Swiggy Instamart",  YELLOW),
    "bigbasket": (run_bigbasket, "BigBasket",         CYAN),
    "firstcry":  (run_firstcry,  "FirstCry",          BOLD),
    "zepto":     (run_zepto,     "Zepto",             "\033[95m"),  # purple
}


async def main(platforms: list[str], auto: bool = False) -> None:
    banner("🔑  Session Refresh Tool  —  Hot Wheels Tracker", BOLD)
    print(f"  Platforms: {', '.join(platforms)}")
    print(f"  Runs one browser at a time — interact and press ENTER each time.\n")

    results: dict[str, bool] = {}
    total = len(platforms)

    for i, name in enumerate(platforms, 1):
        fn, label, color = RUNNERS[name]
        step(i, total, f"{label} session", color)
        ok = await fn(auto=auto)
        results[name] = ok

        if i < total and not auto:
            print()
            input(f"  [{i}/{total} done] Ready for next platform? Press ENTER to continue… ")

    # ── Summary ──────────────────────────────────────────────────────
    banner("📋  Summary", BOLD)
    for name, ok in results.items():
        icon = GREEN + "✅" + RESET if ok else RED + "❌" + RESET
        print(f"  {icon}  {RUNNERS[name][1]}")
    print()

    failed = [n for n, ok in results.items() if not ok]
    if failed:
        print(RED + f"  ⚠️  Failed: {', '.join(failed)} — check output above" + RESET)
    else:
        print(GREEN + "  All sessions refreshed successfully!" + RESET)

    print()
    print(BOLD + "  Run tracker:" + RESET)
    print("    python tracker/main.py --once")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Refresh Hot Wheels tracker sessions")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--skip",
        nargs="+",
        metavar="PLATFORM",
        choices=ALL_PLATFORMS,
        help="Platforms to skip (e.g. --skip blinkit)",
    )
    group.add_argument(
        "--only",
        nargs="+",
        metavar="PLATFORM",
        choices=ALL_PLATFORMS,
        help="Only refresh these (e.g. --only instamart bigbasket)",
    )
    parser.add_argument("-a", "--auto", action="store_true", help="Auto-refresh without manual intervention")
    args = parser.parse_args()

    if args.only:
        platforms = [p for p in ALL_PLATFORMS if p in args.only]
    elif args.skip:
        platforms = [p for p in ALL_PLATFORMS if p not in args.skip]
    else:
        platforms = list(ALL_PLATFORMS)

    if not platforms:
        print(RED + "No platforms selected." + RESET)
        sys.exit(1)

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    asyncio.run(main(platforms, auto=args.auto))
