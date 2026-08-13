"""Telegram command channel: control and inspect the bot from your phone.

Supported commands (authorized chat ID only):
    /status      equity, day PnL, guardrails, open positions
    /halt        pause all new entries (manual halt)
    /resume      clear the manual halt
    /reset_kill  reset the drawdown kill switch (manual reset by design)
    /report      write today's report and return its path
    /metrics     win rate, profit factor, max drawdown
    /help        command list

Design notes:
- Polls getUpdates with a persisted offset, so restarts never re-handle
  old messages and the stale /start from setup is skipped on first run.
- Anything from a chat other than the configured one is logged and ignored.
- Like the notifier, the command loop never raises into the trading loop.
"""
import asyncio
import logging

logger = logging.getLogger("PolyWhale.commands")

HELP_TEXT = (
    "PolyWhale commands:\n"
    "/status - equity, day PnL, guardrails, open positions\n"
    "/halt - pause all new entries\n"
    "/resume - clear the manual halt\n"
    "/reset_kill - reset the drawdown kill switch\n"
    "/report - write today's report, return its path\n"
    "/metrics - win rate, profit factor, max drawdown\n"
    "/help - this list"
)


class CommandHandler:
    def __init__(self, http, cfg, ops, notifier, ledger):
        self.http = http
        self.cfg = cfg
        self.ops = ops  # the agent: exposes ops_* methods
        self.notifier = notifier
        self.ledger = ledger

    @property
    def enabled(self) -> bool:
        return self.notifier.enabled

    # ---- update stream ----
    async def poll_once(self) -> int:
        """Fetch pending updates, dispatch authorized commands. Returns handled count."""
        offset = self.ledger.get_meta("tg_offset", "")
        url = f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/getUpdates"
        params = {"timeout": 0}
        if offset:
            params["offset"] = int(offset)
        resp = await self.http.get(url, params=params, timeout=15.0)
        data = resp.json()
        if not data.get("ok"):
            logger.warning(f"getUpdates returned: {str(data)[:200]}")
            return 0
        updates = data.get("result", [])
        handled = 0
        first_run = not offset
        for u in updates:
            uid = u.get("update_id", 0)
            self.ledger.set_meta("tg_offset", uid + 1)
            msg = u.get("message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            if str(chat_id) != str(self.cfg.telegram_chat_id):
                logger.warning(f"unauthorized command from chat {chat_id} ignored")
                continue
            if first_run:
                # Skip setup backlog (e.g. the original /start) on first poll.
                continue
            handled += 1
            await self.dispatch(text)
        return handled

    async def dispatch(self, text: str):
        cmd = text.split()[0].split("@")[0].lower()
        logger.info(f"command received: {cmd}")
        try:
            reply = await self.handle(cmd)
        except Exception as e:
            logger.error(f"command {cmd} failed: {e}")
            reply = f"{cmd} failed: {e}"
        if reply:
            await self.notifier.send(reply)

    async def handle(self, cmd: str):
        if cmd == "/help":
            return HELP_TEXT
        if cmd == "/status":
            return self.ops.ops_status()
        if cmd == "/halt":
            return self.ops.ops_halt()
        if cmd == "/resume":
            return self.ops.ops_resume()
        if cmd == "/reset_kill":
            return self.ops.ops_reset_kill()
        if cmd == "/report":
            return self.ops.ops_report()
        if cmd == "/metrics":
            return self.ops.ops_metrics()
        return f"Unknown command. {HELP_TEXT}"

    async def run(self, interval: float = 10.0):
        """Polling loop; runs as a sibling task to the trading loop."""
        logger.info("Telegram command channel active")
        while True:
            try:
                await self.poll_once()
            except Exception as e:
                logger.warning(f"command poll failed: {e}")
            await asyncio.sleep(interval)
