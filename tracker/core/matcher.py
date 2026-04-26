"""
matcher.py — Fuzzy product matching engine

Uses rapidfuzz (C extension, ~10x faster than fuzzywuzzy) with a
multi-scorer approach tailored for Blinkit's verbose product names.

Strategy:
  - token_set_ratio (70% weight): Ignores word order and extra words.
    "Hot Wheels Basic Donut Drifter Toy Car" ↔ "Donut Drifter" = high score
  - partial_ratio (30% weight): Catches clean substring matches.

token_set_ratio is the primary scorer because product names are verbose
and model names are typically substrings embedded in a longer description.

Brand filter (brand_filter=True, default):
  Products must contain "hot wheels" in the name before fuzzy scoring.
  This prevents LEGO, Thinkerplace, etc. from matching watchlist targets
  even when they share a model name (e.g. "McLaren", "Optimus Prime").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

# Case-insensitive substrings that must appear in the product name.
# Any product missing ALL of these is silently skipped.
BRAND_TOKENS: list[str] = ["hot wheels", "hotwheels"]


@dataclass
class MatchResult:
    """Result of a successful fuzzy match."""
    product_name: str       # Full product name
    watchlist_target: str   # The watchlist item it matched
    score: float            # Composite match score (0–100)
    product_id: Optional[str] = None
    price: Optional[float] = None
    image_url: Optional[str] = None


def _is_hot_wheels(product_name: str) -> bool:
    """Return True if the product name contains a Hot Wheels brand token."""
    name_lower = product_name.lower()
    return any(tok in name_lower for tok in BRAND_TOKENS)


def compute_match_score(product_name: str, target: str) -> float:
    """
    Compute a composite fuzzy match score between a product name and a target.

    Returns a score between 0–100.
    """
    name_lower = product_name.lower().strip()
    target_lower = target.lower().strip()

    # token_set_ratio: best for partial matches with lots of extra words
    score_tsr = fuzz.token_set_ratio(name_lower, target_lower)

    # partial_ratio: catches clean substring containment
    score_pr = fuzz.partial_ratio(target_lower, name_lower)

    # Weighted composite: TSR dominant
    return round(score_tsr * 0.70 + score_pr * 0.30, 2)


def match_product(
    product_name: str,
    watchlist: list[str],
    threshold: int = 65,
    product_id: Optional[str] = None,
    price: Optional[float] = None,
    image_url: Optional[str] = None,
    brand_filter: bool = True,
) -> Optional[MatchResult]:
    """
    Try to match a product name against all watchlist targets.

    brand_filter (default True): skip products that don't contain
    "hot wheels" / "hotwheels" in the name. Set to False only for
    platforms where non-HW matches are expected.

    Returns the best MatchResult if any target exceeds the threshold,
    or None if no match is found.
    """
    # ── Brand gate ───────────────────────────────────────────────
    if brand_filter and not _is_hot_wheels(product_name):
        logger.debug(
            "SKIP (no 'hot wheels' in name): '%s'", product_name[:80]
        )
        return None

    best: Optional[MatchResult] = None

    for target in watchlist:
        score = compute_match_score(product_name, target)

        if score >= threshold:
            if best is None or score > best.score:
                best = MatchResult(
                    product_name=product_name,
                    watchlist_target=target,
                    score=score,
                    product_id=product_id,
                    price=price,
                    image_url=image_url,
                )

    if best:
        logger.debug(
            "MATCH: '%s' → '%s' (score: %.1f)",
            product_name[:60],
            best.watchlist_target,
            best.score,
        )
    return best


def match_products(
    products: list[dict],
    watchlist: list[str],
    threshold: int = 65,
    brand_filter: bool = True,
) -> list[MatchResult]:
    """
    Match a list of product dicts against the watchlist.

    Expects each product dict to have at minimum a 'name' key.
    Additional keys (id, price, image_url) are used if present.

    brand_filter (default True): requires "hot wheels" in product name.

    Returns a list of MatchResult for all products that matched.
    """
    results = []

    for product in products:
        name = product.get("name") or product.get("title") or ""
        if not name:
            continue

        result = match_product(
            product_name=name,
            watchlist=watchlist,
            threshold=threshold,
            product_id=str(product.get("id", "")),
            price=product.get("price") or product.get("mrp"),
            image_url=product.get("image_url") or product.get("thumb_url"),
            brand_filter=brand_filter,
        )
        if result:
            results.append(result)

    return results
