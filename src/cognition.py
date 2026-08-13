"""Cognition layer: the two strategy engines. No order sizing or limits here —
that belongs to the risk engine. Engines only decide WHAT and WHEN.
"""
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from config import RUGCHECK_BASE
from perception import CandidateToken

logger = logging.getLogger("PolyWhale.cognition")


@dataclass
class TradeDecision:
    sleeve: str
    mint: str
    symbol: str
    usd: float
    reason: str


class MajorsEngine:
    """Weekly DCA into SOL, gated by a 20-day SMA trend filter."""

    def __init__(self, cfg):
        self.cfg = cfg

    def dca_due(self, last_dca_ts: Optional[float]) -> bool:
        if last_dca_ts is None:
            return True
        return (time.time() - last_dca_ts) >= self.cfg.dca_interval_hours * 3600

    def trend_ok(self, prices: list) -> bool:
        """Buy only while SOL trades above its SMA. Too little history -> allow (bootstrap)."""
        if len(prices) < 2:
            return True
        window = prices[-self.cfg.sma_window:]
        sma = sum(window) / len(window)
        return prices[-1] > sma

    def decide(self, last_dca_ts: Optional[float], prices: list) -> Optional[TradeDecision]:
        if not self.dca_due(last_dca_ts):
            return None
        if not self.trend_ok(prices):
            logger.info("Majors: SOL below SMA, pausing DCA")
            return None
        from config import SOL_MINT
        return TradeDecision("majors", SOL_MINT, "SOL", self.cfg.dca_amount_usd, "dca_trend_ok")


class MemeEngine:
    """Momentum scoring with a strict safety gate. Fixed entries, mechanical exits."""

    MAX_PUMP_H6 = 300.0    # never chase a blow-off top
    MAX_DUMP_H1 = -30.0    # never catch a falling knife

    def __init__(self, cfg):
        self.cfg = cfg

    # ---- entry ----
    def passes_gate(self, c: CandidateToken, now: float = None) -> tuple:
        """Cheap statistical gate before we spend an API call on RugCheck."""
        if c.liquidity_usd < self.cfg.min_token_liquidity_usd:
            return False, f"liquidity ${c.liquidity_usd:.0f} < ${self.cfg.min_token_liquidity_usd:.0f}"
        if c.age_hours(now) < self.cfg.min_token_age_hours:
            return False, f"age {c.age_hours(now):.1f}h < {self.cfg.min_token_age_hours}h"
        if c.price_change_h6 > self.MAX_PUMP_H6:
            return False, f"blow-off top risk: +{c.price_change_h6:.0f}% in 6h"
        if c.price_change_h1 < self.MAX_DUMP_H1:
            return False, f"falling knife: {c.price_change_h1:.0f}% in 1h"
        if c.price_usd <= 0:
            return False, "no price"
        return True, "gate_ok"

    def score(self, c: CandidateToken) -> float:
        """Liquidity turnover weighted by positive momentum."""
        vol_liq = min(c.volume_h24 / max(c.liquidity_usd, 1.0), 10.0)
        momentum = 0.6 * c.price_change_h6 + 0.4 * c.price_change_h1
        return vol_liq * (1.0 + max(momentum, 0.0) / 100.0)

    async def safety_check(self, http: httpx.AsyncClient, mint: str) -> tuple:
        """On-chain safety report via RugCheck. Fails closed on any error."""
        try:
            resp = await http.get(f"{RUGCHECK_BASE}/v1/tokens/{mint}/report")
            resp.raise_for_status()
            report = resp.json()
            if report.get("mintAuthority"):
                return False, "mint authority active"
            if report.get("freezeAuthority"):
                return False, "freeze authority active"
            if report.get("score_normalised", 0) > 60:
                return False, f"rugcheck score {report.get('score_normalised')}"
            for risk in report.get("risks", []) or []:
                if risk.get("level") == "danger":
                    return False, f"danger risk: {risk.get('name')}"
            holders = report.get("topHolders", []) or []
            top10 = sum(h.get("pct", 0) for h in holders[:10])
            if top10 > 30.0:
                return False, f"top-10 holders own {top10:.1f}%"
            lp_locked = max(
                ((m.get("lp") or {}).get("lpLockedPct") or 0) for m in report.get("markets", []) or [{}]
            )
            if lp_locked < 90.0:
                return False, f"LP locked only {lp_locked:.0f}%"
            return True, "safety_ok"
        except Exception as e:
            return False, f"safety check error: {e}"

    # ---- exit ----
    def exit_decision(self, pos: dict, now: float = None) -> Optional[tuple]:
        """Returns (fraction_to_sell, reason) or None to hold."""
        now = now if now is not None else time.time()
        entry, current = pos["entry_price"], pos["current_price"]
        if entry <= 0 or current <= 0:
            return None
        ret = current / entry - 1.0
        peak = max(pos.get("peak_price", 0), current)
        hold_hours = (now - pos["opened_at"]) / 3600

        if ret <= self.cfg.stop_loss_pct:
            return 1.0, f"stop_loss ({ret:+.0%})"
        if hold_hours >= self.cfg.max_hold_hours:
            return 1.0, f"max_hold ({hold_hours:.0f}h)"
        tp_level = entry * (1.0 + self.cfg.take_profit_pct)
        if peak >= tp_level and current <= peak * (1.0 - self.cfg.trailing_stop_pct):
            return 1.0, f"trailing_stop (peak ${peak:.8g})"
        if ret >= self.cfg.take_profit_pct and not pos.get("tp_half_done"):
            return 0.5, f"take_profit_half ({ret:+.0%})"
        return None
