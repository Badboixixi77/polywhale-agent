"""PolyWhale v2 — autonomous Solana DEX trading bot.

Two sleeves inside a risk cage:
- majors: weekly $2 DCA into SOL, gated by a 20-day SMA trend filter
- meme:   fixed $2 momentum entries on safety-screened tokens, mechanical exits

Run: python src/agent.py   (DRY_RUN=true in .env until you are ready)
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

from config import SOL_MINT, USDC_MINT, Config
from state import Ledger
from perception import PerceptionLayer
from cognition import MajorsEngine, MemeEngine
from risk import RiskEngine
from execution import ExecutionLayer
from notify import Notifier
from report import Reporter
from metrics import MetricsEngine
from commands import CommandHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler("polywhale.log"), logging.StreamHandler()],
)
logger = logging.getLogger("PolyWhale")

DASHBOARD_EVERY = 15  # cycles (~15 min)
RECONCILE_EVERY = 60  # cycles (~hourly on-chain balance check in live mode)
PRICE_JUMP_LIMIT = 3.0  # reject price marks moving >3x per cycle (bad data guard)


class PolyWhaleAgent:
    def __init__(self, cfg: Config = None, ledger_path: str = "polywhale_ledger.db"):
        self.cfg = cfg or Config.from_env()
        self.ledger = Ledger(ledger_path)
        self.perception = PerceptionLayer(self.cfg)
        self.execution = ExecutionLayer(self.cfg, self.ledger, self.perception.http)
        self.risk = RiskEngine(self.cfg, self.ledger)
        self.majors = MajorsEngine(self.cfg)
        self.memes = MemeEngine(self.cfg)
        self.notifier = Notifier(self.perception.http, self.cfg.telegram_bot_token, self.cfg.telegram_chat_id)
        self.reporter = Reporter(self.cfg, self.ledger)
        self.metrics = MetricsEngine(self.cfg, self.ledger)
        self.commands = CommandHandler(self.perception.http, self.cfg, self, self.notifier, self.ledger)
        self.cycle = 0
        self._last_day = None
        self._was_halted = False
        self._was_week_locked = False
        self._was_killed = False
        self._last_status = None

    # ---- accounting ----
    def equity(self) -> float:
        open_positions = self.ledger.open_positions()
        cash = self.cfg.bankroll + self.ledger.realized_pnl_total() - self.ledger.invested_usd()
        market_value = sum(p["amount"] * p["current_price"] for p in open_positions)
        return cash + market_value

    # ---- lifecycle ----
    async def bootstrap(self):
        if self.ledger.price_history(limit=1):
            return
        history = await self.perception.bootstrap_sol_history(days=self.cfg.sma_window + 5)
        for ts, price in history:
            self.ledger.append_price(ts, price)
        logger.info(f"Seeded {len(history)} daily SOL closes for the trend filter")

    async def manage_exits(self):
        sol_price = await self.perception.sol_price()
        for pos in self.ledger.open_positions():
            if pos["sleeve"] not in ("meme", "satellite"):
                continue
            price = await self._guarded_price(pos)
            if price <= 0:
                continue
            self.ledger.update_price(pos["mint"], price)
            pos["current_price"] = price
            if pos["sleeve"] == "satellite":
                await self._satellite_exit_check(pos, sol_price)
                continue
            decision = self.memes.exit_decision(pos)
            if decision is None:
                continue
            fraction, reason = decision
            fill = await self.execution.sell("meme", pos["mint"], pos["symbol"], pos["amount"] * fraction, sol_price)
            if fill is None:
                logger.warning(f"exit deferred for {pos['symbol']}: swap failed")
                continue
            pnl = self.ledger.reduce_position(pos["id"], fraction, fill["price"])
            if "take_profit_half" in reason:
                self.ledger.mark_tp_half_done(pos["id"])
            logger.info(f"EXIT {pos['symbol']} [{reason}] fraction={fraction:.0%} pnl=${pnl:+.3f}")
            await self.notifier.send(f"EXIT {pos['symbol']} [{reason}] pnl ${pnl:+.2f}")

    async def _guarded_price(self, pos: dict) -> float:
        """Fresh mark for a position, rejected if it jumps wildly without a
        second source agreeing (bad-data guard)."""
        price = await self.perception.token_price(pos["mint"])
        if price <= 0 or pos["current_price"] <= 0:
            return price
        if price / pos["current_price"] > PRICE_JUMP_LIMIT or price / pos["current_price"] < 1 / PRICE_JUMP_LIMIT:
            cross = await self.perception.token_price_dexscreener(pos["mint"])
            if cross <= 0 or (cross / pos["current_price"] > PRICE_JUMP_LIMIT
                              or cross / pos["current_price"] < 1 / PRICE_JUMP_LIMIT):
                logger.warning(f"price guard: {pos['symbol']} mark rejected "
                               f"(${pos['current_price']:.8g} -> ${price:.8g})")
                return 0.0
            price = cross
        return price

    async def _satellite_exit_check(self, pos: dict, sol_price: float):
        decision = self.memes.satellite_exit(pos)
        if decision is None:
            return
        fraction, reason = decision
        fill = await self.execution.sell("satellite", pos["mint"], pos["symbol"], pos["amount"] * fraction, sol_price)
        if fill is None:
            logger.warning(f"satellite exit deferred for {pos['symbol']}: swap failed")
            return
        pnl = self.ledger.reduce_position(pos["id"], fraction, fill["price"])
        capital = max(fill["usd"], 0.0)  # full compounding: proceeds become the next slice
        self.ledger.set_meta("satellite_capital", capital)
        logger.info(f"SATELLITE EXIT {pos['symbol']} [{reason}] pnl=${pnl:+.3f} slice now ${capital:.2f}")
        await self.notifier.send(f"SATELLITE EXIT {pos['symbol']} [{reason}] pnl ${pnl:+.2f} — slice now ${capital:.2f}")

    async def majors_tick(self, sol_price: float):
        last_dca_raw = self.ledger.get_meta("last_dca_ts")
        last_dca = float(last_dca_raw) if last_dca_raw else None
        prices = [p for _, p in self.ledger.price_history(limit=self.cfg.sma_window + 5)]
        decision = self.majors.decide(last_dca, prices)
        if decision is None:
            return
        approved, reason = self.risk.approve_entry("majors", decision.usd)
        if not approved:
            logger.info(f"Majors DCA blocked: {reason}")
            return
        fill = await self.execution.buy("majors", SOL_MINT, SOL_MINT, "SOL", decision.usd)
        if fill is None:
            return
        self.ledger.open_or_add_position("majors", SOL_MINT, "SOL", fill["usd"], fill["amount"], fill["price"])
        self.ledger.set_meta("last_dca_ts", time.time())
        logger.info(f"Majors DCA complete: +{fill['amount']:.6f} SOL")
        await self.notifier.send(f"DCA: bought {fill['amount']:.6f} SOL for ${decision.usd:.2f}")

    async def meme_tick(self):
        held = {p["mint"] for p in self.ledger.open_positions()}
        candidates = []
        if self.cfg.discovery in ("meme", "both"):
            candidates += await self.perception.discover_candidates(limit=15)
        if self.cfg.discovery in ("market", "both"):
            candidates += await self.perception.discover_market_candidates(limit=30)
        # Old momentum meme sleeve is RETIRED (owner decision): all new entries
        # flow through the judgment-gated satellite sleeve only. Any legacy
        # meme positions still open are managed out by the monitor loop
        # (trailing/stops/TP) until they close naturally.
        sat_pool = candidates + await self.perception.discover_trending_candidates(limit=15)
        await self._satellite_entry(sat_pool, held)

    async def _satellite_entry(self, candidates: list, held: set):
        """Aggression sleeve: one coin, whole slice, loose gate. The slice
        compounds — wins roll fully into the next entry, losses shrink it."""
        if not self.cfg.satellite_enabled or self.ledger.position_count("satellite") >= 1:
            return
        capital = float(self.ledger.get_meta("satellite_capital", self.cfg.satellite_budget_usd))
        if capital < 0.50:
            logger.info("satellite slice depleted — idling")
            return
        gated = []
        for c in candidates:
            ok, reason = self.memes.satellite_gate(c)
            if ok and c.mint not in held:
                gated.append(c)
            elif not ok:
                logger.debug(f"satellite gate reject {c.symbol}: {reason}")
        if not gated:
            return
        gated.sort(key=self.memes.score, reverse=True)

        for c in gated[:3]:
            # judgment engine: evidence → verdict → fire. Certain losses
            # (honeypot/authority) are vetoed; real risks are scored, and a
            # TAKE enters the full slice without hesitation.
            veto, flags, top10, lp_locked = await self.memes.safety_flags(self.perception.http, c.mint)
            if veto:
                logger.info(f"satellite veto {c.symbol}: {veto} (certain loss, not a risk)")
                continue
            verdict, jscore, breakdown = self.memes.risk_judgment(c, flags, top10, lp_locked)
            logger.info(f"satellite judgment {c.symbol}: {verdict} {jscore} ({breakdown})")
            if verdict != "TAKE":
                continue
            approved, reason = self.risk.approve_entry("satellite", capital)
            if not approved:
                logger.info(f"Satellite entry blocked: {reason}")
                return
            fill = await self.execution.buy("satellite", SOL_MINT, c.mint, c.symbol, capital)
            if fill is None:
                continue
            self.ledger.open_or_add_position("satellite", c.mint, c.symbol, fill["usd"], fill["amount"], fill["price"])
            logger.info(f"SATELLITE entry: {c.symbol} ${fill['usd']:.2f} judgment={jscore}")
            await self.notifier.send(
                f"🎯 RISK TAKEN: {c.symbol} ${fill['usd']:.2f} | verdict TAKE (score {jscore})\n{breakdown}")
            return

    # ---- operator commands (Telegram) ----
    def _status(self) -> dict:
        return self._last_status or self.risk.watchdog_tick(self.equity())

    def ops_status(self) -> str:
        s = self._status()
        flags = ((" HALTED" if s["halted"] else "") + (" KILLED" if s["killed"] else "")
                 + (" WEEK-LOCKED" if s.get("weekly_halted") else ""))
        halt = "ENGAGED" if self.risk.manual_halt_active() else "off"
        lines = [
            f"PolyWhale status | mode={'PAPER' if self.cfg.dry_run else 'LIVE'}{flags}",
            f"Equity: ${s['equity']:.2f} | Day PnL: ${s['daily_pnl']:+.2f} | "
            f"Week PnL: ${s.get('weekly_pnl', 0.0):+.2f} | Drawdown: {s['drawdown']:.1%}",
            f"Manual halt: {halt}",
        ]
        positions = self.ledger.open_positions()
        lines.append(f"Open positions ({len(positions)}):")
        if not positions:
            lines.append("- none")
        for p in positions:
            value = p["amount"] * p["current_price"]
            ret = (p["current_price"] / p["entry_price"] - 1) if p["entry_price"] > 0 else 0.0
            lines.append(f"- [{p['sleeve']}] {p['symbol']}: ${value:.2f} ({ret:+.1%})")
        return "\n".join(lines)

    def ops_halt(self) -> str:
        self.risk.set_manual_halt(True)
        return "Manual halt ENGAGED — no new entries until /resume. Open positions still follow exit rules."

    def ops_resume(self) -> str:
        self.risk.set_manual_halt(False)
        return "Manual halt cleared — trading resumed."

    def ops_reset_kill(self) -> str:
        self.risk.reset_kill_switch()
        return "Kill switch reset — trading re-enabled. Review what caused it before scaling up."

    def ops_report(self) -> str:
        summary = self.reporter.build_summary(self._status())
        path = self.reporter.write_report(summary)
        return f"Report written to {path}"

    def ops_metrics(self) -> str:
        self.metrics.write_dashboard()
        return self.metrics.render_text()

    async def _watch_events(self, status: dict):
        """Alert on guardrail transitions and emit the daily report at UTC rollover."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._last_day is None:
            self._last_day = today
        elif self._last_day != today:
            summary = self.reporter.build_summary(status)
            path = self.reporter.write_report(summary, self._last_day)
            await self.notifier.send(
                f"Daily report {self._last_day} -> {path} | equity ${status['equity']:.2f}, "
                f"day PnL ${status['daily_pnl']:+.2f}"
            )
            self._last_day = today
        if status["halted"] and not self._was_halted:
            await self.notifier.send("WATCHDOG: daily loss limit hit — new entries halted for 24h")
        if status.get("weekly_halted") and not self._was_week_locked:
            await self.notifier.send(
                f"WEEKLY PROFIT LOCK: ${status['weekly_pnl']:+.2f} banked this week — "
                f"new entries paused until Monday UTC. Exits still managed."
            )
        if status["killed"] and not self._was_killed:
            await self.notifier.send("WATCHDOG: KILL SWITCH ENGAGED — trading stopped, manual reset required")
        self._was_halted, self._was_killed = status["halted"], status["killed"]
        self._was_week_locked = status.get("weekly_halted", False)

    # ---- main loop ----
    async def run(self):
        logger.info(f"PolyWhale v2 activated | mode={self.execution.mode.upper()} | "
                    f"bankroll=${self.cfg.bankroll:.0f} (majors ${self.cfg.majors_budget:.0f} / "
                    f"meme ${self.cfg.meme_budget:.0f} / satellite ${self.cfg.satellite_budget_usd:.0f})")
        if not self.cfg.dry_run:
            logger.warning("LIVE MODE: real funds at risk. Kill switch resets manually only.")
        await self.notifier.send(
            f"PolyWhale v2 started | mode={'PAPER' if self.cfg.dry_run else 'LIVE'} | "
            f"bankroll ${self.cfg.bankroll:.0f} (majors ${self.cfg.majors_budget:.0f} / "
            f"meme ${self.cfg.meme_budget:.0f} / satellite ${self.cfg.satellite_budget_usd:.0f})"
        )
        await self.bootstrap()
        if self.cfg.satellite_enabled and self.ledger.get_meta("satellite_capital") is None:
            self.ledger.set_meta("satellite_capital", self.cfg.satellite_budget_usd)

        if self.commands.enabled:
            asyncio.create_task(self.commands.run())

        while True:
            self.cycle += 1
            self.ledger.set_meta("last_heartbeat", time.time())
            try:
                if not self.cfg.dry_run and (self.cycle == 1 or self.cycle % RECONCILE_EVERY == 0):
                    try:
                        ok, notes = await self.execution.reconcile()
                        if not ok:
                            await self.notifier.send(
                                "RECONCILE DRIFT: on-chain balances below ledger — entries halted. "
                                + " | ".join(notes)
                            )
                            self.risk.set_manual_halt(True)
                    except Exception as e:
                        logger.error(f"reconcile error: {e}")

                sol_price = await self.perception.sol_price()
                if sol_price > 0:
                    self.ledger.append_price(time.time(), sol_price)
                    self.ledger.update_price(SOL_MINT, sol_price)

                await self.manage_exits()
                await self.majors_tick(sol_price)
                if self.cycle % self.cfg.meme_scan_every_cycles == 1:
                    await self.meme_tick()

                status = self.risk.watchdog_tick(self.equity())
                self._last_status = status
                self.ledger.record_equity(time.time(), status["equity"])
                if self.cycle % DASHBOARD_EVERY == 0:
                    self.metrics.write_dashboard()
                await self._watch_events(status)
                n_open = len(self.ledger.open_positions())
                flags = ((" HALTED" if status["halted"] else "") + (" KILLED" if status["killed"] else "")
                         + (" WEEK-LOCKED" if status.get("weekly_halted") else ""))
                logger.info(
                    f"Cycle {self.cycle}: SOL=${sol_price:.2f} equity=${status['equity']:.2f} "
                    f"dayPnL=${status['daily_pnl']:+.3f} weekPnL=${status['weekly_pnl']:+.3f} "
                    f"dd={status['drawdown']:.1%} open={n_open}{flags}"
                )
                if status["killed"]:
                    logger.critical("Kill switch engaged — idling. Reset manually to resume.")
            except Exception as e:
                logger.error(f"cycle error: {e}")
            await asyncio.sleep(60)


async def main():
    agent = PolyWhaleAgent()
    try:
        await agent.run()
    finally:
        await agent.perception.close()
        agent.ledger.close()


if __name__ == "__main__":
    asyncio.run(main())
