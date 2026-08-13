"""Risk engine: the cage. Every order must pass approve_entry before it
reaches execution, and the watchdog enforces portfolio-level stops.
"""
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger("PolyWhale.risk")


class RiskEngine:
    def __init__(self, cfg, ledger):
        self.cfg = cfg
        self.ledger = ledger

    # ---- kill switch (manual reset only) ----
    def kill_switch_active(self) -> bool:
        return self.ledger.get_meta("kill_switch", "0") == "1"

    def reset_kill_switch(self):
        self.ledger.set_meta("kill_switch", "0")
        logger.warning("Kill switch manually reset")

    # ---- daily loss halt ----
    def daily_halt_active(self) -> bool:
        until = self.ledger.get_meta("daily_halt_until", "0")
        return time.time() < float(until)

    # ---- per-order approval ----
    def approve_entry(self, sleeve: str, usd: float) -> tuple:
        """Returns (approved, reason). The cage fails closed."""
        if self.kill_switch_active():
            return False, "kill switch active"
        if self.daily_halt_active():
            return False, "daily loss halt active"
        if usd <= 0:
            return False, "non-positive size"

        if sleeve == "meme":
            if usd > self.cfg.meme_trade_cap_usd + 1e-9:
                return False, f"${usd:.2f} exceeds per-trade cap ${self.cfg.meme_trade_cap_usd:.2f}"
            if self.ledger.position_count("meme") >= self.cfg.max_meme_positions:
                return False, f"max meme positions ({self.cfg.max_meme_positions}) reached"
            if self.ledger.invested_usd("meme") + usd > self.cfg.meme_budget + 1e-9:
                return False, f"meme sleeve budget ${self.cfg.meme_budget:.2f} exhausted"
            return True, "approved"

        if sleeve == "majors":
            if self.ledger.invested_usd("majors") + usd > self.cfg.majors_budget + 1e-9:
                return False, f"majors sleeve budget ${self.cfg.majors_budget:.2f} exhausted"
            return True, "approved"

        return False, f"unknown sleeve: {sleeve}"

    def approve_slippage(self, bps: float) -> tuple:
        if bps > self.cfg.slippage_bps:
            return False, f"slippage {bps}bps exceeds cap {self.cfg.slippage_bps}bps"
        return True, "ok"

    # ---- portfolio watchdog ----
    def watchdog_tick(self, equity: float) -> dict:
        """Called every cycle. Rolls the day anchor, enforces daily loss and drawdown."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.ledger.get_meta("day_key") != today:
            self.ledger.set_meta("day_key", today)
            self.ledger.set_meta("day_start_equity", equity)
            logger.info(f"Watchdog: new day {today}, start equity ${equity:.2f}")

        peak = float(self.ledger.get_meta("peak_equity", "0") or 0)
        if equity > peak:
            peak = equity
            self.ledger.set_meta("peak_equity", equity)

        day_start = float(self.ledger.get_meta("day_start_equity", equity) or equity)
        daily_pnl = equity - day_start
        daily_limit = self.cfg.bankroll * self.cfg.daily_loss_limit_pct

        if not self.daily_halt_active() and daily_pnl < -daily_limit:
            self.ledger.set_meta("daily_halt_until", time.time() + 24 * 3600)
            logger.warning(f"WATCHDOG: daily loss ${daily_pnl:.2f} breached -${daily_limit:.2f}, halting entries 24h")

        drawdown = (peak - equity) / peak if peak > 0 else 0.0
        if not self.kill_switch_active() and drawdown >= self.cfg.drawdown_kill_pct:
            self.ledger.set_meta("kill_switch", "1")
            logger.critical(
                f"WATCHDOG: drawdown {drawdown:.1%} breached kill level "
                f"{self.cfg.drawdown_kill_pct:.0%}, KILL SWITCH ENGAGED (manual reset required)"
            )

        return {
            "equity": equity,
            "daily_pnl": daily_pnl,
            "peak_equity": peak,
            "drawdown": drawdown,
            "halted": self.daily_halt_active(),
            "killed": self.kill_switch_active(),
        }
