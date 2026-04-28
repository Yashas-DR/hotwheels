"""
firstcry/api.py — FirstCry product listing API client (curl-cffi edition)

Confirmed API (from HAR capture):

    GET https://www.firstcry.com/svcs/SearchResult.svc/GetSearchResultProductsPaging
        ?PageNo=1
        &PageSize=20
        &SortExpression=popularity
        &OnSale=5
        &SearchString=brand
        &MasterBrand=113       ← Hot Wheels brand ID
        &pcode=560037          ← pincode — the ONLY location signal
        &isclub=0
        (all other filter params = empty string)

Response structure (outer JSON):
    {
        "ProductResponse": "<JSON string — double-encoded!>"
    }

Inner JSON (after second json.loads()):
    {
        "Products": [
            {
                "PId":        "22597417",
                "PNm":        "Hot Wheels Color Shifters Blade Raider ...",
                "BNm":        "Hot Wheels",
                "MRP":        "369",
                "Disc":       "0",
                "discprice":  "369",
                "CrntStock":  "209",   ← THIS is the stock signal
                "shippingdate": "Sunday, Apr 26"
            },
            ...
        ],
        "PerProd": []
    }

In-stock criteria:
    int(CrntStock) > 0

Pagination:
    PageNo increments from 1.
    Stop when Products is empty OR len(Products) < PageSize.

MasterBrand=113:
    Scopes results strictly to Hot Wheels — avoids noise from other toy brands.
    Do not change this unless FirstCry reassigns brand IDs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Optional

from core.rate_limiter import RateLimiter
from core.connectivity import connectivity

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.firstcry.com/svcs/SearchResult.svc/GetSearchResultProductsPaging"
HOT_WHEELS_BRAND_ID = 113
PAGE_SIZE = 20

_TIMEOUT_RETRIES = 2
_TIMEOUT_BACKOFF = [10, 25]  # seconds between retries


class FCClient:
    """
    Async FirstCry listing API client using curl-cffi (Chrome TLS).

    One client instance handles all pincodes — location is a per-call param.
    Must be used as an async context manager.
    """

    def __init__(self, session, rate_limiter: RateLimiter, config: dict):
        self._session = session
        self.rate_limiter = rate_limiter
        fc_cfg = config.get("firstcry", {})
        self.max_pages: int = fc_cfg.get("max_pages", 5)
        self.master_brand: int = fc_cfg.get("master_brand", HOT_WHEELS_BRAND_ID)
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

    async def search_all_pages(
        self,
        query: str,
        pincode: str,
        location_name: str = "Unknown",
    ) -> list[dict]:
        """
        Paginate through FirstCry brand listing for Hot Wheels.
        `query` is unused in the URL (brand filter does the work) but kept
        for interface symmetry with BigBasket/Instamart clients.
        Returns in-stock products only.
        """
        all_products: list[dict] = []

        logger.info(
            "🔵 [FirstCry] Searching Hot Wheels @ %s (pincode=%s, max %d pages)",
            location_name, pincode, self.max_pages,
        )

        for page_num in range(1, self.max_pages + 1):
            products = await self._fetch_page(pincode=pincode, page=page_num)

            if products is None:
                logger.warning("[FirstCry] Page %d: API error — aborting", page_num)
                break

            if not products:
                logger.debug("[FirstCry] Page %d: empty — stopping", page_num)
                break

            all_products.extend(products)
            logger.debug(
                "[FirstCry] Page %d: %d products (total: %d)",
                page_num, len(products), len(all_products),
            )

            # Stop if this page was short — no more pages
            if len(products) < PAGE_SIZE:
                break

            if page_num < self.max_pages:
                await asyncio.sleep(random.uniform(1.5, 3.5))

        logger.info(
            "  └─ [FirstCry] %d in-stock product(s) @ %s (pincode=%s)",
            len(all_products), location_name, pincode,
        )
        return all_products

    async def _fetch_page(self, pincode: str, page: int) -> Optional[list[dict]]:
        """Fetch one page. Returns None on hard error, [] on empty page."""
        await self.rate_limiter.acquire()

        headers, cookies = self._session.get_request_args()

        # All unused filter params must be sent as empty strings — FirstCry requires them
        params = {
            "PageNo":          page,
            "PageSize":        PAGE_SIZE,
            "SortExpression":  "popularity",
            "OnSale":          5,
            "SearchString":    "brand",
            "SubCatId":        "",
            "BrandId":         "",
            "Price":           "",
            "Age":             "",
            "Color":           "",
            "OptionalFilter":  "",
            "OutOfStock":      "",
            "Type1":           "", "Type2":  "", "Type3":  "", "Type4":  "",
            "Type5":           "", "Type6":  "", "Type7":  "", "Type8":  "",
            "Type9":           "", "Type10": "", "Type11": "", "Type12": "",
            "Type13":          "", "Type14": "", "Type15": "",
            "combo":           "",
            "discount":        "",
            "searchwithincat": "",
            "ProductidQstr":   "",
            "searchrank":      "",
            "pmonths":         "",
            "cgen":            "",
            "PriceQstr":       "",
            "DiscountQstr":    "",
            "sorting":         "",
            "MasterBrand":     self.master_brand,
            "Rating":          "",
            "Offer":           "",
            "skills":          "",
            "material":        "",
            "curatedcollections": "",
            "measurement":     "",
            "gender":          "",
            "exclude":         "",
            "premium":         "",
            "pcode":           pincode,
            "isclub":          0,
            "deliverytype":    "",
            "author":          "", "booktype": "", "character": "",
            "collection":      "", "format":   "", "genre":     "",
            "booklanguage":    "", "publication": "", "skill":  "",
        }

        last_exc: Exception | None = None
        r = None
        for attempt in range(1 + _TIMEOUT_RETRIES):
            try:
                r = await self._curl_session.get(
                    SEARCH_URL,
                    params=params,
                    headers=headers,
                    cookies=cookies,
                    timeout=20,
                )
                break  # success
            except Exception as e:
                last_exc = e
                err_str = str(e)
                is_timeout = "timed out" in err_str.lower() or "Operation timed out" in err_str
                if is_timeout and attempt < _TIMEOUT_RETRIES:
                    backoff = _TIMEOUT_BACKOFF[attempt]
                    logger.warning(
                        "[FirstCry] Timeout page %d (attempt %d/%d) — retrying in %ds",
                        page, attempt + 1, 1 + _TIMEOUT_RETRIES, backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
                connectivity.report_error(err_str)
                logger.warning("[FirstCry] Request error (page %d): %s", page, e)
                return None
        else:
            connectivity.report_error(str(last_exc))
            logger.warning("[FirstCry] All retries failed (page %d): %s", page, last_exc)
            return None

        connectivity.report_ok()

        if r.status_code in (401, 403):
            logger.warning("[FirstCry] HTTP %d — session may have expired", r.status_code)
            self._session.is_valid = False
            return None

        if r.status_code != 200:
            logger.warning("[FirstCry] HTTP %d (page %d)", r.status_code, page)
            return []

        try:
            outer = r.json()
        except Exception:
            body_preview = r.text[:120] if r.text else ""
            logger.warning("[FirstCry] Invalid JSON (page %d): %s", page, body_preview)
            return None

        return self._extract_products(outer)

    def _extract_products(self, outer: dict) -> list[dict]:
        """
        Parse double-encoded ProductResponse.
        outer["ProductResponse"] is a JSON string that must be decoded again.
        """
        raw_str = outer.get("ProductResponse", "")
        if not raw_str:
            return []

        try:
            inner = json.loads(raw_str)
        except Exception as e:
            logger.warning("[FirstCry] Failed to decode inner ProductResponse: %s", e)
            return []

        products_raw = inner.get("Products") or []
        result = []
        for p in products_raw:
            normalized = self._normalize(p)
            if normalized:
                result.append(normalized)
        return result

    def _normalize(self, p: dict) -> Optional[dict]:
        """
        Normalize a FirstCry product dict.
        Returns None if out of stock or missing name.

        Stock check: int(CrntStock) > 0
        """
        if not isinstance(p, dict):
            return None

        name = (p.get("PNm") or "").strip()
        if not name:
            return None

        # Stock check — the CrntStock field is a string in the API
        try:
            stock = int(p.get("CrntStock", 0))
        except (ValueError, TypeError):
            stock = 0

        if stock <= 0:
            logger.debug("[FirstCry] OOS: '%s' (CrntStock=%s)", name[:50], p.get("CrntStock"))
            return None

        # Price extraction
        try:
            price = float(p.get("discprice", 0) or 0)
        except (ValueError, TypeError):
            price = None

        try:
            mrp = float(p.get("MRP", 0) or 0)
        except (ValueError, TypeError):
            mrp = None

        try:
            discount = int(p.get("Disc", 0) or 0)
        except (ValueError, TypeError):
            discount = 0

        return {
            "name":         name,
            "brand":        (p.get("BNm") or "").strip(),
            "product_id":   str(p.get("PId", "")),
            "price":        price,
            "mrp":          mrp,
            "discount":     discount,
            "shipping_date": p.get("shippingdate", ""),
        }
