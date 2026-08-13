"""Alerts: Telegram when configured, log-only otherwise.

Never blocks the trading loop, never raises — a dead messenger must not
kill the bot or, worse, silently swallow a kill-switch event.
"""
import logging

logger = logging.getLogger("PolyWhale.notify")


class Notifier:
    def __init__(self, http, bot_token: str = "", chat_id: str = ""):
        self.http = http
        self.bot_token = bot_token
        self.chat_id = chat_id

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def send(self, message: str):
        logger.info(f"ALERT: {message}")
        if not self.enabled:
            return
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            resp = await self.http.post(
                url, json={"chat_id": self.chat_id, "text": message}, timeout=10.0
            )
            if resp.status_code != 200:
                logger.warning(f"telegram send returned {resp.status_code}")
        except Exception as e:
            logger.warning(f"telegram send failed: {e}")
