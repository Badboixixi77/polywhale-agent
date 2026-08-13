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
        """Cheap statistical gate before we spend an API call on RugCheck.
        Market-sourced candidates face market-tuned thresholds."""
        if c.source == "market":
            min_liq, min_age = self.cfg.min_market_liquidity_usd, self.cfg.min_market_age_hours
            # Backtested 2026-08: entries on flat tape (cbBTC 0/10, BONK 0/17)
            # bleed to max_hold. Require real h6 momentum for market picks.
            # 0 (or negative) disables the floor.
            if self.cfg.market_min_h6_pct > 0 and c.price_change_h6 < self.cfg.market_min_h6_pct:
                return False, f"dead tape: h6 {c.price_change_h6:+.1f}% < +{self.cfg.market_min_h6_pct:.0f}%"
        else:
            min_liq, min_age = self.cfg.min_token_liquidity_usd, self.cfg.min_token_age_hours
        if c.liquidity_usd < min_liq:
            return False, f"liquidity ${c.liquidity_usd:.0f} < ${min_liq:.0f}"
        if c.age_hours(now) < min_age:
            return False, f"age {c.age_hours(now):.1f}h < {min_age:.0f}h"
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

    async def safety_check(self, http: httpx.AsyncClient, mint: str, source: str = "meme") -> tuple:
        """On-chain safety report via RugCheck. Fails closed on any error.
        Meme candidates face the full strict checklist; market candidates
        (already filtered by liquidity/age/volume) keep the honeypot
        vectors — live freeze authority and non-authority danger risks —
        but tolerate CEX custody concentration and LP-lock reporting
        quirks that established tokens legitimately have."""
        try:
            resp = await http.get(f"{RUGCHECK_BASE}/v1/tokens/{mint}/report")
            resp.raise_for_status()
            report = resp.json()
            mint_live = self._authority_live(report.get("mintAuthority"))
            freeze_live = self._authority_live(report.get("freezeAuthority"))
            if source == "meme" and mint_live:
                return False, "mint authority active"
            if freeze_live:
                return False, "freeze authority active"
            if source == "meme" and report.get("score_normalised", 0) > 60:
                return False, f"rugcheck score {report.get('score_normalised')}"
            for risk in report.get("risks", []) or []:
                if risk.get("level") != "danger":
                    continue
                name = (risk.get("name") or "")
                if source == "market" and "authority" in name.lower():
                    continue  # parsed authorities above are ground truth; RugCheck's
                    # heuristic flags can contradict them on established tokens
                return False, f"danger risk: {name}"
            holders = report.get("topHolders", []) or []
            top10 = sum(h.get("pct", 0) for h in holders[:10])
            # wrapped/custodial tokens (cbBTC & co.) concentrate holdings at the
            # issuer by design, so market candidates get a much wider cap
            holder_cap = 30.0 if source == "meme" else 80.0
            if top10 > holder_cap:
                return False, f"top-10 holders own {top10:.1f}%"
            if source == "meme":
                lp_locked = max(
                    ((m.get("lp") or {}).get("lpLockedPct") or 0) for m in report.get("markets", []) or [{}]
                )
                if lp_locked < 90.0:
                    return False, f"LP locked only {lp_locked:.0f}%"
            return True, "safety_ok"
        except Exception as e:
            return False, f"safety check error: {e}"

    @staticmethod
    def _authority_live(authority) -> bool:
        """RugCheck returns an account object, not null/true. An authority
        owned by the System Program (all 1s) is renounced and harmless."""
        SYSTEM_PROGRAM = "11111111111111111111111111111111"
        if not authority:
            return False
        if isinstance(authority, dict):
            return authority.get("owner", SYSTEM_PROGRAM) != SYSTEM_PROGRAM
        return bool(authority)

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

    # ---- satellite sleeve (aggression mode) ----
    def satellite_gate(self, c: CandidateToken, now: float = None) -> tuple:
        """Deliberately loose: fresh momentum is the point. Only hard floors —
        tradable liquidity, not literally seconds old, not a blow-off top,
        not a falling knife."""
        if c.liquidity_usd < self.cfg.satellite_min_liquidity_usd:
            return False, f"liquidity ${c.liquidity_usd:.0f} < ${self.cfg.satellite_min_liquidity_usd:.0f}"
        if c.age_hours(now) < 1.0:
            return False, f"age {c.age_hours(now):.2f}h < 1h"
        if c.price_change_h6 > self.MAX_PUMP_H6:
            return False, f"blow-off top risk: +{c.price_change_h6:.0f}% in 6h"
        if c.price_change_h1 < self.MAX_DUMP_H1:
            return False, f"falling knife: {c.price_change_h1:.0f}% in 1h"
        if c.price_usd <= 0:
            return False, "no price"
        return True, "satellite_gate_ok"

    def satellite_exit(self, pos: dict, now: float = None) -> Optional[tuple]:
        """Ride winners: no take-profit, no time limit. Wide stop while
        underwater; a trailing stop locks gains once the slice is in profit."""
        now = now if now is not None else time.time()
        entry, current = pos["entry_price"], pos["current_price"]
        if entry <= 0 or current <= 0:
            return None
        ret = current / entry - 1.0
        peak = max(pos.get("peak_price", 0), current)

        if ret <= self.cfg.satellite_stop_pct:
            return 1.0, f"satellite_stop ({ret:+.0%})"
        if peak > entry and current <= peak * (1.0 - self.cfg.satellite_trail_pct):
            return 1.0, f"satellite_trail (peak ${peak:.8g})"
        return None
