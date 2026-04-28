"""
telegram_bot.py — Async Telegram alert sender

Uses python-telegram-bot (v21+, async-native).
Sends formatted alert messages when Hot Wheels are found.
Also sends status alerts (session expired, tracker started/stopped).
"""

from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class TelegramAlerter:
    """Sends Telegram messages for product alerts and system events."""

    def __init__(self, config: dict):
        alerts_cfg = config.get("telegram", {})
        self.bot_token: str = alerts_cfg.get("bot_token", "")
        self.chat_id: str = str(alerts_cfg.get("chat_id", ""))
        self.enabled: bool = alerts_cfg.get("enabled", True)
        self._bot = None

        if self.enabled and (not self.bot_token or "YOUR_BOT_TOKEN" in self.bot_token):
            logger.warning(
                "Telegram bot_token not configured. "
                "Edit config.yaml → telegram.bot_token. Alerts disabled."
            )
            self.enabled = False

    async def _get_bot(self):
        """Lazy-initialize the Telegram Bot instance."""
        if self._bot is None and self.enabled:
            try:
                from telegram import Bot
                self._bot = Bot(token=self.bot_token)
            except ImportError:
                logger.error("python-telegram-bot not installed. Run: pip install python-telegram-bot>=21")
                self.enabled = False
        return self._bot

    async def send_product_alert(
        self,
        product_name: str,
        watchlist_target: str,
        match_score: float,
        location_name: str,
        price=None,
        in_stock: bool = True,
        platform: str = "Blinkit",
    ) -> bool:
        """
        Send a product found alert.

        Example message:
        🔥 HOT WHEELS ALERT!

        📦 Product: Hot Wheels Bone Shaker Die Cast Car
        🎯 Matched: "Bone Shaker" (confidence: 91%)
        📍 Location: Koramangala
        💰 Price: ₹399
        📊 Status: In Stock
        🛒 blinkit.com/s/?q=hot+wheels

        ⏰ 02:31 AM
        """
        if not self.enabled:
            return False

        timestamp = datetime.now().strftime("%I:%M %p")
        price_str = f"₹{price:.0f}" if price else "N/A"
        stock_str = "✅ In Stock" if in_stock else "⚠️ Limited Stock"

        text = (
            f"🔥 *HOT WHEELS ALERT\\!*\n\n"
            f"🏪 *Platform:* {self._escape(platform)}\n"
            f"📦 *Product:* {self._escape(product_name)}\n"
            f"🎯 *Matched:* \"{self._escape(watchlist_target)}\" "
            f"\\({match_score:.0f}% confidence\\)\n"
            f"📍 *Location:* {self._escape(location_name)}\n"
            f"💰 *Price:* {price_str}\n"
            f"📊 *Status:* {stock_str}\n"
            f"🛒 [Search Blinkit](https://blinkit\\.com/s/?q\\=hot\\+wheels)\n\n"
            f"⏰ {timestamp}"
        )

        return await self._send(text, parse_mode="MarkdownV2")

    async def send_session_expired_alert(self, platform: str = "Blinkit") -> bool:
        """Alert that a platform session has expired and re-login is needed."""
        cmd = (
            "`python tracker/tools/session_extractor.py`"
            if platform == "Blinkit"
            else "`python tracker/tools/instamart_extractor.py`"
        )
        text = (
            f"⚠️ *{self._escape(platform)} Session Expired*\n\n"
            f"The {self._escape(platform)} session is no longer valid\\.\n"
            f"Please re\\-run the session extractor:\n\n"
            f"{cmd}\n\n"
            "Scanning for this platform has been paused\\."
        )
        return await self._send(text, parse_mode="MarkdownV2")

    async def send_platform_error(
        self,
        platform: str,
        error_type: str,
        detail: str = "",
    ) -> bool:
        """
        Send a platform error/warning notification.

        error_type: short label e.g. "Scan Error", "Session Invalid", "Request Failed"
        detail:     brief human-readable description (first line of exception etc.)
        """
        timestamp = datetime.now().strftime("%I:%M %p")
        emoji = "🔴" if platform == "Blinkit" else "🟠"
        detail_line = f"\n__{self._escape(detail[:120])}__" if detail else ""
        text = (
            f"{emoji} *{self._escape(platform)} \\— {self._escape(error_type)}*"
            f"{detail_line}\n\n"
            f"⏰ {timestamp}"
        )
        return await self._send(text, parse_mode="MarkdownV2")

    async def send_startup_alert(self, locations: list, watchlist: list) -> bool:
        """Alert that the tracker has started."""
        loc_str = "\n".join(f"  • {loc}" for loc in locations)
        watch_str = "\n".join(f"  • {item}" for item in watchlist)
        text = (
            f"🚀 *Hot Wheels Tracker Started*\n\n"
            f"📍 *Locations:*\n{self._escape(loc_str)}\n\n"
            f"🎯 *Watchlist:*\n{self._escape(watch_str)}\n\n"
            f"Scanning every 35–75 seconds\\."
        )
        return await self._send(text, parse_mode="MarkdownV2")

    async def send_internet_down_alert(self, err_detail: str = "") -> bool:
        """
        Alert that the internet connection has been lost.
        Sent ONCE when connectivity drops — not on every failed request.
        """
        timestamp = datetime.now().strftime("%I:%M %p")
        # Extract the short curl error code line for readability
        detail_short = err_detail.split("\n")[0][:120] if err_detail else ""
        detail_line = f"\n`{self._escape(detail_short)}`" if detail_short else ""
        text = (
            f"\U0001f4f5 *Internet Connection Lost\\!*{detail_line}\n\n"
            f"The tracker is running but cannot reach any platform\\.\n"
            f"Scans will resume automatically when connectivity returns\\.\n\n"
            f"\u23f0 {timestamp}"
        )
        return await self._send(text, parse_mode="MarkdownV2")

    async def send_internet_up_alert(self, down_seconds: int = 0) -> bool:
        """
        Alert that the internet connection has been restored.
        Sent ONCE when connectivity recovers.
        """
        timestamp = datetime.now().strftime("%I:%M %p")
        mins, secs = divmod(down_seconds, 60)
        duration = f"{mins}m {secs}s" if mins else f"{secs}s"
        text = (
            f"\u2705 *Internet Connection Restored*\n\n"
            f"Was down for approximately {self._escape(duration)}\\.\n"
            f"Tracker is scanning normally again\\.\n\n"
            f"\u23f0 {timestamp}"
        )
        return await self._send(text, parse_mode="MarkdownV2")

    async def send_status(self, message: str) -> bool:
        """Send a plain status message."""
        return await self._send(self._escape(message), parse_mode="MarkdownV2")

    async def _send(self, text: str, parse_mode: str = "MarkdownV2") -> bool:
        """Internal: send a message via Telegram Bot API."""
        bot = await self._get_bot()
        if not bot:
            return False

        try:
            await bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=parse_mode,
                disable_web_page_preview=False,
            )
            logger.debug("Telegram message sent.")
            return True
        except Exception as e:
            logger.error("Telegram send failed: %s", e)
            return False

    @staticmethod
    def _escape(text: str) -> str:
        """Escape special characters for MarkdownV2."""
        special = r"\_*[]()~`>#+-=|{}.!"
        for ch in special:
            text = text.replace(ch, f"\\{ch}")
        return text
