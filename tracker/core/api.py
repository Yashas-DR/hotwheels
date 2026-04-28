"""
api.py — Blinkit search API client (curl-cffi edition)

Uses curl-cffi AsyncSession with Chrome TLS impersonation.
Cloudflare cannot distinguish these requests from real Chrome.

Pagination (confirmed from real traffic):
    Page 1:  POST /v1/layout/search?q=hot+wheels&search_type=type_to_search
    Page 2+: adds offset, limit, search_method=similarity, page_index

Response structure (confirmed):
    {
        "is_success": true,
        "response": {
            "snippets": [
                {
                    "widget_type": "product_card_snippet_type_2",
                    "data": {
                        "name":          { "text": "Hot Wheels 1:64 Scale..." },
                        "inventory":     7,
                        "product_id":    "444563",
                        "merchant_id":   "45744",
                        "is_sold_out":   false,
                        "product_state": "available"
                    }
                }
            ]
        }
    }
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

from core.rate_limiter import RateLimiter
from core.locations import Location
from core.connectivity import connectivity

logger = logging.getLogger(__name__)

SEARCH_URL = "https://blinkit.com/v1/layout/search"
LIMIT = 12
PRODUCT_WIDGET_TYPE = "product_card_snippet_type_2"


class BlinkitClient:
    """
    Async Blinkit search client using curl-cffi (Chrome TLS impersonation).
    All requests are strictly sequential — never concurrent.
    """

    def __init__(
        self,
        session_manager,
        rate_limiter: RateLimiter,
        config: dict,
    ):
        self.session_manager = session_manager
        self.rate_limiter = rate_limiter
        self.max_pages = config.get("search", {}).get("max_pages", 4)
        self._curl_session = None

    async def __aenter__(self):
        try:
            from curl_cffi.requests import AsyncSession
            self._curl_session = AsyncSession(impersonate="chrome120")
        except ImportError:
            raise RuntimeError(
                "curl-cffi not installed. Run: pip install curl-cffi"
            )
        return self

    async def __aexit__(self, *_):
        if self._curl_session:
            await self._curl_session.close()
            self._curl_session = None

    async def search_all_pages(
        self,
        query: str,
        location: Location,
    ) -> list[dict]:
        """
        Paginate through all search results. Returns in-stock products only.
        Sequential — never concurrent.
        """
        all_products: list[dict] = []
        consecutive_empty = 0

        logger.info(
            "🔍 Searching '%s' @ %s (max %d pages)",
            query, location.name, self.max_pages,
        )

        for page_num in range(self.max_pages):
            offset = page_num * LIMIT

            products = await self._fetch_page(
                query=query,
                offset=offset,
                page_index=page_num + 1,
                location=location,
            )

            if products is None:
                logger.warning("Page %d: API error — aborting", page_num + 1)
                break

            if not products:
                consecutive_empty += 1
                logger.debug("Page %d: empty (%d consecutive)", page_num + 1, consecutive_empty)
                if consecutive_empty >= 2:
                    break
            else:
                consecutive_empty = 0
                all_products.extend(products)
                logger.debug(
                    "Page %d: %d products (total: %d)",
                    page_num + 1, len(products), len(all_products),
                )

            # Jitter between pages
            if page_num < self.max_pages - 1 and consecutive_empty < 2:
                await asyncio.sleep(random.uniform(1.5, 3.5))

        logger.info(
            "  └─ %d in-stock product(s) for '%s' @ %s",
            len(all_products), query, location.name,
        )
        return all_products

    async def _fetch_page(
        self,
        query: str,
        offset: int,
        page_index: int,
        location: Location,
    ) -> Optional[list[dict]]:
        """Fetch one page of results. Returns None on hard error."""
        await self.rate_limiter.acquire()

        headers, cookies = self.session_manager.get_request_args(location)

        params: dict = {
            "q": query,
            "search_type": "type_to_search",
        }
        if page_index > 1:
            params.update({
                "offset": offset,
                "limit": LIMIT,
                "search_method": "similarity",
                "page_index": page_index,
            })

        try:
            r = await self._curl_session.post(
                SEARCH_URL,
                params=params,
                headers=headers,
                cookies=cookies,
                timeout=15,
            )
        except Exception as e:
            connectivity.report_error(str(e))
            logger.warning("Request error (page %d): %s", page_index, e)
            return None

        connectivity.report_ok()

        if r.status_code in (401, 403):
            logger.warning("HTTP %d — session expired, marking invalid", r.status_code)
            self.session_manager._valid = False
            return None

        if r.status_code != 200:
            logger.warning("HTTP %d (page %d)", r.status_code, page_index)
            return None

        try:
            data = r.json()
        except Exception:
            logger.warning("Invalid JSON (page %d)", page_index)
            return None

        if not data.get("is_success"):
            logger.debug("is_success=False (page %d)", page_index)
            return []

        snippets = data.get("response", {}).get("snippets", [])
        if not snippets:
            return []

        logger.debug("Page %d: %d snippets total", page_index, len(snippets))
        return self._extract_products(snippets)

    def _extract_products(self, snippets: list) -> list[dict]:
        """Filter snippets to product_card_snippet_type_2, return in-stock only."""
        products = []
        for snippet in snippets:
            if not isinstance(snippet, dict):
                continue
            if snippet.get("widget_type") != PRODUCT_WIDGET_TYPE:
                continue
            product = self._normalize_snippet(snippet)
            if product:
                products.append(product)
        return products

    def _normalize_snippet(self, snippet: dict) -> Optional[dict]:
        """
        Normalize a product snippet. Returns None if OOS or missing name.

        Stock check: inventory > 0 AND not is_sold_out AND product_state == "available"
        Name:        data.name.text (or data.display_name.text)
        """
        data = snippet.get("data", {})
        if not data:
            return None

        name_obj = data.get("name") or data.get("display_name") or {}
        if isinstance(name_obj, dict):
            name = name_obj.get("text", "").strip()
        elif isinstance(name_obj, str):
            name = name_obj.strip()
        else:
            name = ""

        if not name:
            return None

        inventory = data.get("inventory", 0)
        is_sold_out = data.get("is_sold_out", False)
        product_state = data.get("product_state", "")

        if not (inventory > 0 and not is_sold_out and product_state == "available"):
            logger.debug(
                "OOS: '%s' inv=%d sold_out=%s state='%s'",
                name[:50], inventory, is_sold_out, product_state,
            )
            return None

        brand_obj = data.get("brand_name", {})
        brand = brand_obj.get("text", "") if isinstance(brand_obj, dict) else ""

        return {
            "name": name,
            "brand": brand,
            "product_id": str(data.get("product_id", "")),
            "merchant_id": str(data.get("merchant_id", "")),
            "inventory": inventory,
            "product_state": product_state,
        }
