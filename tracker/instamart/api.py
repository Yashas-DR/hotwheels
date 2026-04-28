"""
instamart/api.py — Swiggy Instamart search API client

Endpoint:
    POST https://www.swiggy.com/api/instamart/search/v2

Query params (URL):
    offset, ageConsent=false, layoutId, storeId, primaryStoreId, secondaryStoreId

Request body:
    {
        "facets": [],
        "sortAttribute": "",
        "query": "hot wheels",
        "search_results_offset": "0",
        "page_type": "INSTAMART_AUTO_SUGGEST_PAGE",
        "is_pre_search_tag": False
    }

Response path to products:
    data.cards[] → card.card → @type=GridWidget
        → gridElements.infoWithStyle.items[]
            → displayName, brand, inStock, isAvail, inventory.inStock, variations[]

Stock signals (ALL three must be true):
    inStock == true
    isAvail == true
    inventory.inStock == true  (from first variation)

Pagination:
    data.pageOffset.nextOffset → use as next offset value ("" or null = done)

FAULT ISOLATION:
    All exceptions are caught and logged — never propagate to Blinkit's loop.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

from core.rate_limiter import RateLimiter
from core.connectivity import connectivity

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.swiggy.com/api/instamart/search/v2"
GRID_WIDGET_TYPE = "GridWidget"


class InstaClient:
    """
    Async Swiggy Instamart search client using curl-cffi (Chrome TLS).
    All requests sequential — never concurrent, never parallel.

    Fault-isolated: every method catches exceptions internally.
    Caller (main.py) should also wrap in try/except for full isolation.
    """

    def __init__(
        self,
        session,           # InstaSession
        rate_limiter: RateLimiter,
        config: dict,
    ):
        self.session = session
        self.rate_limiter = rate_limiter
        self.max_pages = config.get("search", {}).get("max_pages", 4)
        self._curl_session = None

    async def __aenter__(self):
        try:
            from curl_cffi.requests import AsyncSession
            # MUST match the Playwright browser version that generated the aws-waf-token.
            # HAR confirms browser is Chromium 147 (Edge 147). curl-cffi's chrome146
            # is the closest available profile — same JA3/JA4 fingerprint family.
            self._curl_session = AsyncSession(impersonate="chrome146")
        except ImportError:
            raise RuntimeError("curl-cffi not installed. Run: pip install curl-cffi")
        return self

    async def __aexit__(self, *_):
        if self._curl_session:
            try:
                await self._curl_session.close()
            except Exception:
                pass
            self._curl_session = None

    async def search_all_pages(
        self,
        query: str,
        location,  # Location dataclass with .lat, .lng, .name
        store_id: Optional[int] = None,
        secondary_store_id: str = "",
    ) -> list[dict]:
        """
        Paginate through Instamart results for a query at a location.
        Returns in-stock products only.
        Never raises — returns [] on any error.
        """
        if not self.session.is_valid:
            logger.debug("Instamart session invalid — skipping search")
            return []

        if store_id is None:
            store_id, secondary_store_id = self.session.get_store_ids(location.name)
            
        if store_id is None:
            logger.warning(
                "No storeId for %s — skipping Instamart. "
                "Add store_id to config.yaml → instamart.locations.",
                location.name,
            )
            return []

        all_products: list[dict] = []
        next_offset: Optional[str] = "0"
        pages_fetched = 0
        products_fetched_so_far = 0  # body's search_results_offset = product count, not page#

        logger.info(
            "🟠 [Instamart] Searching '%s' @ %s (store=%s sec=%s)",
            query, location.name, store_id, secondary_store_id or "-",
        )

        while next_offset is not None and pages_fetched < self.max_pages:
            try:
                products, next_offset = await self._fetch_page(
                    query=query,
                    offset=next_offset,
                    store_id=store_id,
                    secondary_store_id=secondary_store_id,
                    location=location,
                    is_first_page=(pages_fetched == 0),
                    products_fetched_so_far=products_fetched_so_far,
                )
            except Exception as e:
                logger.error("Instamart fetch error (page %d): %s", pages_fetched + 1, e)
                break

            if products is None:
                break

            if products:
                all_products.extend(products)
                products_fetched_so_far += len(products)
                logger.debug(
                    "  Page %d: %d products (total %d)",
                    pages_fetched + 1, len(products), len(all_products),
                )

            pages_fetched += 1

            # Jitter between pages
            if next_offset and pages_fetched < self.max_pages:
                await asyncio.sleep(random.uniform(1.5, 3.5))

        logger.info(
            "  └─ [Instamart] %d in-stock product(s) for '%s' @ %s",
            len(all_products), query, location.name,
        )
        return all_products

    async def _fetch_page(
        self,
        query: str,
        offset: str,
        store_id: int,
        secondary_store_id: str,
        location,
        is_first_page: bool = True,
        products_fetched_so_far: int = 0,
    ) -> tuple[Optional[list[dict]], Optional[str]]:
        """
        Fetch one page of Instamart results. Retries up to 3× on
        transient network/DNS errors (curl error 6 on Windows).

        Returns:
            (products, next_offset) — products=None on hard error
            next_offset=None means no more pages
        """
        await self.rate_limiter.acquire()

        headers, cookies = self.session.get_request_args(location)

        params = {
            "offset": offset,
            "ageConsent": "false",
            "layoutId": self.session.layout_id,
            "voiceSearchTrackingId": "",
            "storeId": store_id,
            "primaryStoreId": store_id,
            "secondaryStoreId": secondary_store_id,
            # clientId NOT sent by real browser (confirmed via HAR)
        }

        body = {
            "facets": [],
            "sortAttribute": "",
            "query": query,
            # search_results_offset = count of already-fetched products, NOT page number
            "search_results_offset": str(products_fetched_so_far),
            # HAR shows INSTAMART_PRE_SEARCH_PAGE, not AUTO_SUGGEST
            "page_type": "INSTAMART_PRE_SEARCH_PAGE",
            # is_pre_search_tag = True only on the FIRST page request
            "is_pre_search_tag": is_first_page,
        }

        # Retry up to 3× on transient DNS/connection failures
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                r = await self._curl_session.post(
                    SEARCH_URL,
                    params=params,
                    json=body,
                    headers=headers,
                    cookies=cookies,
                timeout=15,
            )
                break  # success — exit retry loop
            except Exception as e:
                err_str = str(e)
                is_dns = "resolve host" in err_str.lower() or "curl: (6)" in err_str.lower()
                if is_dns and attempt < max_attempts:
                    logger.warning(
                        "Instamart DNS error (attempt %d/%d): %s — retrying in 2s",
                        attempt, max_attempts, err_str[:80],
                    )
                    await asyncio.sleep(2.0)
                    continue
                connectivity.report_error(err_str)
                logger.warning("Instamart request error: %s", e)
                return None, None

        connectivity.report_ok()

        if r.status_code in (401, 403):
            logger.warning(
                "Instamart HTTP %d for store %s — this store may be inactive.",
                r.status_code, params.get("storeId", "?"),
            )
            # Don't kill session here — only mark invalid after ALL stores/fallbacks fail.
            # main.py will call _auto_recover_platform if session.is_valid is still False
            # after the full location loop completes.
            self._consecutive_403s = getattr(self, "_consecutive_403s", 0) + 1
            if self._consecutive_403s >= 3:
                logger.warning(
                    "Instamart: %d consecutive 403s — session is dead. Triggering recovery.",
                    self._consecutive_403s,
                )
                self.session._valid = False
            return None, None

        if r.status_code == 202:
            content_type = r.headers.get("content-type", "").lower()
            body_start = (r.text or "")[:40].lstrip()
            if body_start.startswith("<") or "text/html" in content_type:
                logger.warning(
                    "Instamart session blocked (HTTP 202 + HTML body) — "
                    "Swiggy is serving a bot-challenge page."
                )
                self._consecutive_403s = getattr(self, "_consecutive_403s", 0) + 1
                if self._consecutive_403s >= 3:
                    self.session._valid = False
                return None, None
        
        # Successful response resets the failure counter
        self._consecutive_403s = 0

        if r.status_code not in (200, 202):
            logger.warning("Instamart unexpected HTTP %d", r.status_code)
            return [], None

        try:
            data = r.json()
        except Exception:
            logger.warning("Instamart response is not valid JSON (HTTP %d, body=%s)",
                           r.status_code, r.text[:200])
            return [], None

        if not isinstance(data, dict):
            logger.warning("Instamart unexpected response type: %s", type(data))
            return [], None

        # DEBUG: log top-level keys so we can see actual response shape
        top_keys = list(data.keys()) if data else []
        logger.debug("[Instamart] Response top-level keys: %s", top_keys)

        # Navigate to cards — data["data"] CAN be None, handle it
        inner = data.get("data") or {}

        if not isinstance(inner, dict):
            logger.warning(
                "[Instamart] Unexpected 'data' field type=%s. Top keys: %s. Snippet: %s",
                type(inner).__name__, top_keys, str(data)[:500],
            )
            return [], None

        cards = inner.get("cards") or []

        # Log when cards is empty — helps diagnose wrong response structure
        inner_keys = list(inner.keys()) if inner else []
        if not cards:
            logger.info(
                "[Instamart] No 'cards' in response. inner_keys=%s statusCode=%s",
                inner_keys, data.get("statusCode", "?"),
            )

        # Extract next page offset
        page_offset = inner.get("pageOffset") or {}
        raw_next = page_offset.get("nextOffset", "")
        next_offset = str(raw_next) if raw_next not in (None, "", "0", 0) else None

        if offset != "0" and next_offset == "0":
            next_offset = None

        products = self._extract_products(cards)
        return products, next_offset


    def _extract_products(self, cards: list) -> list[dict]:
        """
        Navigate: cards[] → card_wrapper.card.card → @type=GridWidget
            → gridElements.infoWithStyle.items[]

        NOTE: The Swiggy response wraps cards twice:
            cards[i] = { "card": { "card": { "@type": "...", ... } } }
        """
        products = []
        for card_wrapper in cards:
            if not isinstance(card_wrapper, dict):
                continue

            # First level: card_wrapper["card"] → {"card": {"@type": ...}}
            card_outer = card_wrapper.get("card", {})
            if not isinstance(card_outer, dict):
                continue

            # Second level: card_outer["card"] → {"@type": "GridWidget", ...}
            card = card_outer.get("card", {})
            if not isinstance(card, dict):
                continue

            widget_type = card.get("@type", "")
            if GRID_WIDGET_TYPE not in widget_type:
                continue

            # Navigate into grid
            grid_elements = (
                card
                .get("gridElements", {})
                .get("infoWithStyle", {})
                .get("items", [])
            )

            for item in grid_elements:
                if not isinstance(item, dict):
                    continue
                product = self._normalize_item(item)
                if product:
                    products.append(product)

        return products

    def _normalize_item(self, item: dict) -> Optional[dict]:
        """
        Normalize an Instamart product item.

        Stock: inStock AND isAvail AND inventory.inStock (from first variation)
        Name:  displayName
        Price: variations[0].price.offerPrice.units (or mrp.units)
        """
        name = item.get("displayName", "").strip()
        if not name:
            return None

        # === Stock signals — all three must be true ===
        top_in_stock = item.get("inStock", False)
        top_is_avail = item.get("isAvail", False)

        # Check variation-level inventory (deeper signal)
        variations = item.get("variations", [])
        variation_in_stock = True  # default assume ok if no variations
        price_str = ""
        if variations:
            v = variations[0]
            inv = v.get("inventory", {})
            variation_in_stock = inv.get("inStock", False)

            # Price from first variation
            price_info = v.get("price", {})
            offer = price_info.get("offerPrice", {}).get("units", "")
            mrp = price_info.get("mrp", {}).get("units", "")
            price_str = offer or mrp

        in_stock = top_in_stock and top_is_avail and variation_in_stock

        if not in_stock:
            logger.debug(
                "[Instamart] OOS: '%s' (inStock=%s, isAvail=%s, var_inStock=%s)",
                name[:50], top_in_stock, top_is_avail, variation_in_stock,
            )
            return None

        brand = item.get("brand", "")
        product_id = str(item.get("productId", ""))

        try:
            price = float(price_str) if price_str else None
        except ValueError:
            price = None

        return {
            "name": name,
            "brand": brand,
            "product_id": product_id,
            "price": price,
            "inventory": 1,        # Instamart doesn't expose count
            "product_state": "available",
            "source": "instamart",
        }
