"""
bigbasket/api.py — BigBasket listing API client (curl-cffi edition)

Confirmed API (from HAR capture):

    GET https://www.bigbasket.com/listing-svc/v2/products
        ?type=ps
        &slug=hot+wheels
        &page=1
        &bucket_id=92

Response structure:
    {
        "tabs": [{
            "product_info": {
                "products": [
                    {
                        "id": "40361516",
                        "desc": "HW Euro VW ID. Buzz Bomb Toy Car ...",
                        "availability": {
                            "avail_status": "001",   ← "001" = in-stock
                            "not_for_sale": false,
                            "button": "Add"
                        },
                        "pricing": {
                            "discount": {
                                "mrp": "167",
                                "prim_price": { "sp": "160" }
                            }
                        },
                        "brand": { "name": "Hot wheels " }
                    }
                ]
            }
        }]
    }

In-stock criteria:
    avail_status == "001"   AND
    not_for_sale == false   AND
    button in ("Add", "Add to cart")

Pagination:
    page param increments from 1.
    Stop when tabs[0].product_info.products is empty or missing.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

_TIMEOUT_RETRIES = 2          # extra attempts after first timeout
_TIMEOUT_BACKOFF = [10, 25]   # seconds to wait between retries

from core.rate_limiter import RateLimiter
from core.connectivity import connectivity

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.bigbasket.com/listing-svc/v2/products"
AVAIL_IN_STOCK = "001"


class BBClient:
    """
    Async BigBasket listing API client using curl-cffi (Chrome TLS).
    Location context is baked into session cookies — no params needed.
    All requests are strictly sequential.
    """

    def __init__(self, session, rate_limiter: RateLimiter, config: dict):
        self._session = session
        self.rate_limiter = rate_limiter
        bb_cfg = config.get("bigbasket", {})
        self.max_pages: int = config.get("search", {}).get("max_pages", 4)
        self.bucket_id: int = bb_cfg.get("bucket_id", 92)
        self._curl_session = None

    async def __aenter__(self):
        try:
            from curl_cffi.requests import AsyncSession
            self._curl_session = AsyncSession(impersonate="chrome124")
        except ImportError:
            raise RuntimeError("curl-cffi not installed. Run: pip install curl-cffi")
        return self

    async def __aexit__(self, *_):
        if self._curl_session:
            await self._curl_session.close()
            self._curl_session = None

    async def search_all_pages(self, query: str, location_name: str = "Home") -> list[dict]:
        """
        Paginate through BigBasket listing results.
        Returns in-stock products only.
        """
        all_products: list[dict] = []
        consecutive_empty = 0

        logger.info(
            "🟢 [BigBasket] Searching '%s' @ %s (max %d pages)",
            query, location_name, self.max_pages,
        )

        for page_num in range(1, self.max_pages + 1):
            products = await self._fetch_page(query=query, page=page_num)

            if products is None:
                logger.warning("[BigBasket] Page %d: API error — aborting", page_num)
                break

            if not products:
                consecutive_empty += 1
                logger.debug("[BigBasket] Page %d: empty (%d consecutive)", page_num, consecutive_empty)
                if consecutive_empty >= 2:
                    break
            else:
                consecutive_empty = 0
                all_products.extend(products)
                logger.debug(
                    "[BigBasket] Page %d: %d products (total: %d)",
                    page_num, len(products), len(all_products),
                )

            if page_num < self.max_pages and consecutive_empty < 2:
                await asyncio.sleep(random.uniform(1.5, 3.5))

        logger.info(
            "  └─ [BigBasket] %d in-stock product(s) for '%s' @ %s",
            len(all_products), query, location_name,
        )
        return all_products

    async def _fetch_page(self, query: str, page: int) -> Optional[list[dict]]:
        """Fetch one page. Returns None on hard error, [] on empty."""
        await self.rate_limiter.acquire()

        headers, cookies = self._session.get_request_args()
        params = {
            "type": "ps",
            "slug": query,
            "page": page,
            "bucket_id": self.bucket_id,
        }

        last_exc: Exception | None = None
        for attempt in range(1 + _TIMEOUT_RETRIES):
            try:
                r = await self._curl_session.get(
                    SEARCH_URL,
                    params=params,
                    headers=headers,
                    cookies=cookies,
                    timeout=20,
                )
                break  # success — fall through to status checks
            except Exception as e:
                last_exc = e
                err_str = str(e)
                is_timeout = "timed out" in err_str.lower() or "Operation timed out" in err_str
                if is_timeout and attempt < _TIMEOUT_RETRIES:
                    backoff = _TIMEOUT_BACKOFF[attempt]
                    logger.warning(
                        "[BigBasket] Timeout on page %d (attempt %d/%d) — retrying in %ds",
                        page, attempt + 1, 1 + _TIMEOUT_RETRIES, backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
                connectivity.report_error(err_str)
                logger.warning("[BigBasket] Request error (page %d): %s", page, e)
                return None
        else:
            # All retries exhausted
            connectivity.report_error(str(last_exc))
            logger.warning("[BigBasket] All retries failed (page %d): %s", page, last_exc)
            return None

        connectivity.report_ok()

        if r.status_code in (401, 403):
            logger.warning("[BigBasket] HTTP %d — session expired", r.status_code)
            self._session.is_valid = False
            return None

        if r.status_code != 200:
            logger.warning("[BigBasket] HTTP %d (page %d)", r.status_code, page)
            # BB sometimes 302s or 503s transiently — non-fatal
            return []

        try:
            data = r.json()
        except Exception:
            body_preview = r.text[:120] if r.text else ""
            logger.warning("[BigBasket] Invalid JSON (page %d): %s", page, body_preview)
            return None

        return self._extract_products(data)

    def _extract_products(self, data: dict) -> list[dict]:
        """Parse tabs[0].product_info.products → in-stock products."""
        try:
            tabs = data.get("tabs") or []
            if not tabs:
                return []
            product_info = (tabs[0] or {}).get("product_info") or {}
            products = product_info.get("products") or []
        except Exception as e:
            logger.debug("[BigBasket] Parse error: %s", e)
            return []

        result = []
        for p in products:
            normalized = self._normalize(p)
            if normalized:
                result.append(normalized)
        return result

    def _normalize(self, p: dict) -> Optional[dict]:
        """
        Normalize a BigBasket product. Returns None if OOS or missing name.

        Stock check:
            availability.avail_status == "001"
            AND availability.not_for_sale == False
        """
        if not isinstance(p, dict):
            return None

        name = (p.get("desc") or "").strip()
        if not name:
            return None

        avail = p.get("availability") or {}
        avail_status = avail.get("avail_status", "")
        not_for_sale = avail.get("not_for_sale", True)

        if avail_status != AVAIL_IN_STOCK or not_for_sale:
            logger.debug(
                "[BigBasket] OOS: '%s' status=%s not_for_sale=%s",
                name[:50], avail_status, not_for_sale,
            )
            return None

        # Price extraction
        try:
            price_str = (
                p.get("pricing", {})
                .get("discount", {})
                .get("prim_price", {})
                .get("sp", "")
            )
            price = float(price_str) if price_str else None
        except (ValueError, TypeError):
            price = None

        brand = (p.get("brand") or {}).get("name", "").strip()
        fc_id = (p.get("visibility") or {}).get("fc_id")

        return {
            "name": name,
            "brand": brand,
            "product_id": str(p.get("id", "")),
            "price": price,
            "fc_id": fc_id,
        }
