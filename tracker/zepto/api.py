"""
zepto/api.py — Zepto search API client

Confirmed endpoint (from HAR):
    POST https://bff-gateway.zepto.com/user-search-service/api/v3/search

Request body:
    {
        "query":       "hot wheels",
        "pageNumber":  0,           ← 0-indexed, increment per page
        "intentId":    "<uuid4>",   ← fresh UUID per request
        "mode":        "GENERAL",
        "userSessionId": "<session_id from session_headers>"
    }

Response — layout[] of widgets. Extract from PRODUCT_GRID widgets:
    layout[i].widgetId == "PRODUCT_GRID"
    → data.resolver.data.items[]
        → productResponse
            → product.name              ← product name
            → product.brand             ← brand string
            → productVariant.id         ← variant UUID
            → outOfStock                ← bool — false = in stock
            → availableQuantity         ← int
            → discountedSellingPrice    ← int in PAISE (÷100 = ₹)
            → mrp                       ← int in PAISE (÷100 = ₹)
            → discountPercent           ← int 0-100

Stock signal:
    outOfStock == False  AND  availableQuantity > 0

Prices are in PAISE — divide by 100 to get rupees.

Pagination:
    Increment pageNumber (0,1,2...).
    Stop when a PRODUCT_GRID widget has endOfList==True OR products list is empty.

Confirmed from HAR: pages 0-3 all returned results for Novel Office.
max_pages=4 covers the full Hot Wheels catalog (~60-80 SKUs on Zepto).

Token refresh:
    On HTTP 401/403, attempt POST to /ums/api/v1/token/refresh with the
    refreshToken cookie. On success update accessToken in session and retry once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from typing import Optional

from core.rate_limiter import RateLimiter
from core.connectivity import connectivity

logger = logging.getLogger(__name__)

SEARCH_URL  = "https://bff-gateway.zepto.com/user-search-service/api/v3/search"
REFRESH_URL = "https://bff-gateway.zepto.com/ums/api/v1/token/refresh"

_TIMEOUT_RETRIES = 2
_TIMEOUT_BACKOFF = [8, 20]


class ZeptoClient:
    """
    Async Zepto search client using curl-cffi (Chrome TLS).

    Uses store_id/store_ids/store_etas headers for location — these are
    captured per address by the extractor. All monetary values are in paise.
    """

    def __init__(self, session, rate_limiter: RateLimiter, config: dict):
        self._session     = session
        self.rate_limiter = rate_limiter
        z_cfg             = config.get("zepto", {})
        self.max_pages: int = z_cfg.get("max_pages", 4)
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
            try:
                await self._curl_session.close()
            except Exception:
                pass
            self._curl_session = None

    # ── Public interface ──────────────────────────────────────────────────

    async def search_all_pages(
        self,
        query: str,
        location_name: str,
    ) -> list[dict]:
        """
        Paginate through Zepto search results for `query` at `location_name`.
        Returns in-stock products only. Never raises — returns [] on error.
        """
        all_products: list[dict] = []

        logger.info(
            "🟣 [Zepto] Searching '%s' @ %s (max %d pages)",
            query, location_name, self.max_pages,
        )

        for page_num in range(self.max_pages):
            products, end_of_list = await self._fetch_page(
                query=query,
                location_name=location_name,
                page_number=page_num,
            )

            if products is None:
                logger.warning("[Zepto] Page %d: API error — aborting", page_num)
                break

            if products:
                all_products.extend(products)
                logger.debug(
                    "[Zepto] Page %d: %d products (total: %d)",
                    page_num, len(products), len(all_products),
                )

            if end_of_list:
                logger.debug("[Zepto] Page %d: endOfList — stopping", page_num)
                break

            if not products and page_num > 0:
                break  # empty page past page 0 = done

            if page_num < self.max_pages - 1:
                await asyncio.sleep(random.uniform(1.5, 3.0))

        logger.info(
            "  └─ [Zepto] %d in-stock product(s) @ %s",
            len(all_products), location_name,
        )
        return all_products

    # ── Internal ──────────────────────────────────────────────────────────

    async def _fetch_page(
        self,
        query: str,
        location_name: str,
        page_number: int,
    ) -> tuple[Optional[list[dict]], bool]:
        """
        Fetch one page.
        Returns (products, end_of_list).
        products=None means hard error. products=[] means empty page.
        end_of_list=True means stop paginating.
        """
        await self.rate_limiter.acquire()

        headers, cookies = self._session.get_request_args(location_name)
        session_id = headers.get("session_id", str(uuid.uuid4()))

        body = {
            "query":         query,
            "pageNumber":    page_number,
            "intentId":      str(uuid.uuid4()),
            "mode":          "RECENT_SEARCH",
            "userSessionId": session_id,
        }

        # Per-request IDs (fresh UUID each time)
        req_id = str(uuid.uuid4())
        headers["request_id"] = req_id
        headers["requestid"]  = req_id

        last_exc: Exception | None = None
        r = None

        for attempt in range(1 + _TIMEOUT_RETRIES):
            try:
                r = await self._curl_session.post(
                    SEARCH_URL,
                    json=body,
                    headers=headers,
                    cookies=cookies,
                    timeout=20,
                )
                break
            except Exception as e:
                last_exc = e
                is_timeout = "timed out" in str(e).lower() or "Operation timed out" in str(e)
                if is_timeout and attempt < _TIMEOUT_RETRIES:
                    backoff = _TIMEOUT_BACKOFF[attempt]
                    logger.warning(
                        "[Zepto] Timeout page %d (attempt %d) — retry in %ds",
                        page_number, attempt + 1, backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
                connectivity.report_error(str(e))
                logger.warning("[Zepto] Request error (page %d): %s", page_number, e)
                return None, False
        else:
            connectivity.report_error(str(last_exc))
            logger.warning("[Zepto] All retries failed (page %d): %s", page_number, last_exc)
            return None, False

        connectivity.report_ok()

        # Token expired — attempt refresh once
        if r.status_code in (401, 403):
            logger.info("[Zepto] HTTP %d — attempting token refresh", r.status_code)
            refreshed = await self._refresh_token(headers, cookies)
            if refreshed:
                # Retry with updated cookies
                headers, cookies = self._session.get_request_args(location_name)
                try:
                    r = await self._curl_session.post(
                        SEARCH_URL,
                        json=body,
                        headers=headers,
                        cookies=cookies,
                        timeout=20,
                    )
                except Exception as e:
                    logger.warning("[Zepto] Post-refresh request failed: %s", e)
                    return None, False
                if r.status_code not in (200, 201):
                    logger.warning(
                        "[Zepto] Still HTTP %d after refresh — session expired. "
                        "Re-run: python tracker/tools/zepto_extractor.py",
                        r.status_code,
                    )
                    self._session.is_valid = False
                    return None, False
            else:
                logger.warning(
                    "[Zepto] Token refresh failed — session expired. "
                    "Re-run: python tracker/tools/zepto_extractor.py"
                )
                self._session.is_valid = False
                return None, False

        if r.status_code not in (200, 201):
            logger.warning("[Zepto] Unexpected HTTP %d (page %d)", r.status_code, page_number)
            return [], False

        try:
            data = r.json()
        except Exception:
            logger.warning(
                "[Zepto] Invalid JSON (page %d): %s",
                page_number, (r.text or "")[:100],
            )
            return None, False

        return self._parse_layout(data)

    async def _refresh_token(self, headers: dict, cookies: dict) -> bool:
        """Attempt to refresh the Zepto access token using the refresh token."""
        refresh_token = self._session.get_refresh_token()
        if not refresh_token:
            return False
        try:
            r = await self._curl_session.post(
                REFRESH_URL,
                json={"refreshToken": refresh_token},
                headers={
                    "content-type":  "application/json",
                    "origin":        "https://www.zepto.com",
                    "referer":       "https://www.zepto.com/",
                    "tenant":        "ZEPTO",
                    "platform":      "WEB",
                    "x-without-bearer": "true",
                },
                cookies=cookies,
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json()
                # Try common response shapes
                new_token = (
                    data.get("accessToken")
                    or data.get("data", {}).get("accessToken")
                    or data.get("token")
                    or data.get("access_token")
                )
                if new_token:
                    self._session.update_access_token(new_token)
                    logger.info("[Zepto] Access token refreshed successfully")
                    return True
            logger.warning("[Zepto] Refresh returned HTTP %d", r.status_code)
            return False
        except Exception as e:
            logger.warning("[Zepto] Token refresh request failed: %s", e)
            return False

    def _parse_layout(self, data: dict) -> tuple[list[dict], bool]:
        """
        Navigate layout[] → PRODUCT_GRID widgets → items[] → productResponse.
        Returns (products, end_of_list).
        """
        if not isinstance(data, dict):
            return [], False

        layout = data.get("layout") or []
        all_products: list[dict] = []
        end_of_list = False

        for widget in layout:
            if not isinstance(widget, dict):
                continue
            if widget.get("widgetId") != "PRODUCT_GRID":
                continue

            widget_data = widget.get("data", {})
            if widget_data.get("endOfList"):
                end_of_list = True

            items = (
                widget_data
                .get("resolver", {})
                .get("data", {})
                .get("items", [])
            )

            for item in items:
                normalized = self._normalize(item)
                if normalized:
                    all_products.append(normalized)

        return all_products, end_of_list

    def _normalize(self, item: dict) -> Optional[dict]:
        """
        Normalize a Zepto product item.
        Stock: outOfStock==False AND availableQuantity>0.
        Prices are in paise — divide by 100.
        """
        if not isinstance(item, dict):
            return None

        pr = item.get("productResponse", {})
        if not isinstance(pr, dict):
            return None

        # Stock check
        out_of_stock     = pr.get("outOfStock", True)
        avail_qty        = pr.get("availableQuantity", 0)

        if out_of_stock or avail_qty <= 0:
            product_name = pr.get("product", {}).get("name", "?")[:50]
            logger.debug(
                "[Zepto] OOS: '%s' (outOfStock=%s, availQty=%s)",
                product_name, out_of_stock, avail_qty,
            )
            return None

        product  = pr.get("product", {})
        variant  = pr.get("productVariant", {})

        name  = (product.get("name") or "").strip()
        brand = (product.get("brand") or "").strip()

        if not name:
            return None

        # Prices from paise → rupees
        mrp_p   = pr.get("mrp", 0) or 0
        dsp_p   = pr.get("discountedSellingPrice", mrp_p) or mrp_p
        disc    = pr.get("discountPercent", 0) or 0

        price   = round(dsp_p / 100, 2)
        mrp     = round(mrp_p  / 100, 2)

        return {
            "name":         name,
            "brand":        brand,
            "product_id":   pr.get("id", ""),
            "variant_id":   variant.get("id", ""),
            "price":        price,
            "mrp":          mrp,
            "discount":     disc,
            "available_qty": avail_qty,
        }
