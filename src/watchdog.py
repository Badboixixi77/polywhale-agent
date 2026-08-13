"""Dead-man's switch: watches the agent's heartbeat from the outside.

Runs as a SEPARATE process (see ops/com.polywhale.watchdog.plist). The
trading agent stamps `last_heartbeat` into the ledger every cycle; if the
bot freezes, hangs, or dies without being restarted, this process alerts
you via Telegram. A frozen bot cannot silence its own executioner — that
is the entire point of making this a separate process.

Run manually: python src/watchdog.py
"""
import logging
import os
import sqlite3
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

STALE_AFTER = 180.0      # seconds without a heartbeat before alerting
CHECK_EVERY = 60.0       # how often this process looks at the ledger
ALERT_COOLDOWN = 600.0   # while stale, re-alert at most every 10 min
NEVER_GRACE = 600.0      # don't alarm for a never-stamped ledger in the first 10 min

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("PolyWhale.watchdog")


def read_heartbeat(db_path: str):
    """Read the agent's last heartbeat stamp. None if absent or unreadable."""
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        row = conn.execute("SELECT value FROM meta WHERE key='last_heartbeat'").fetchone()
        conn.close()
        return float(row[0]) if row else None
    except Exception:
        return None


def evaluate(now: float, last_heartbeat, stale_after: float = STALE_AFTER) -> str:
    """'ok' | 'stale' | 'never' — pure function, tested directly."""
    if last_heartbeat is None:
        return "never"
    return "ok" if now - last_heartbeat <= stale_after else "stale"


class Watchdog:
    def __init__(self, bot_token: str, chat_id: str, db_path: str = "polywhale_ledger.db", sender=None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.db_path = db_path
        self.sender = sender or self._send_telegram
        self.stale_alerted = False
        self.last_alert_ts = 0.0
        self.started_at = time.time()

    def _send_telegram(self, text: str):
        if not (self.bot_token and self.chat_id):
            return
        try:
            httpx.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text},
                timeout=10.0,
            )
        except Exception as e:
            logger.warning(f"alert send failed: {e}")

    def tick(self, now: float = None) -> str:
        """One check. Returns 'ok' | 'alerted' | 'recovered'."""
        now = now if now is not None else time.time()
        hb = read_heartbeat(self.db_path)
        status = evaluate(now, hb)
        if status == "ok":
            if self.stale_alerted:
                self.stale_alerted = False
                self.sender("WATCHDOG: PolyWhale heartbeat restored — bot is responsive again.")
                return "recovered"
            return "ok"
        if status == "never" and now - self.started_at < NEVER_GRACE:
            return "ok"  # grace period: the bot may simply not have started yet
        if not self.stale_alerted or now - self.last_alert_ts >= ALERT_COOLDOWN:
            why = ("heartbeat never stamped (bot may not have started)"
                   if status == "never" else f"heartbeat stale for {now - hb:.0f}s")
            self.sender(f"WATCHDOG: PolyWhale HEARTBEAT LOST — {why}. Check the bot.")
            logger.warning(f"heartbeat lost: {why}")
            self.stale_alerted = True
            self.last_alert_ts = now
            return "alerted"
        return "ok"

    def run(self):
        logger.info(f"watchdog active: stale after {STALE_AFTER:.0f}s, checking every {CHECK_EVERY:.0f}s")
        while True:
            try:
                self.tick()
            except Exception as e:
                logger.warning(f"watchdog tick failed: {e}")
            time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    Watchdog(
        os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        os.getenv("TELEGRAM_CHAT_ID", "").strip(),
    ).run()
