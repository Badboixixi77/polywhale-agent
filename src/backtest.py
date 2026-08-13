"""Backtester: replay historical OHLCV bars through the real strategy code.

The engine feeds bar-derived features (h1/h6 momentum, 24h volume, pool age)
through the SAME gates and exit rules the live bot uses (MemeEngine), so
results measure the actual mechanics — thresholds, TP/SL/trail, hold limits —
not an approximation of them.

Known limitations (by design, be honest about them):
- Liquidity is the pool's CURRENT reserve, not historical; the universe is
  today's leaderboard, so results carry survivorship bias.
- Entry assumes the pool was being watched for the whole window.
- Fills are simulated at bar close with a flat cost haircut (fees+slippage).
"""
import asyncio
import logging
from dataclasses import dataclass

from config import GECKOTERMINAL_BASE
from cognition import MemeEngine
from perception import CandidateToken

logger = logging.getLogger("PolyWhale.backtest")

WARMUP_BARS = 24  # need history for h24 volume / h6 momentum features


@dataclass
class BtTrade:
    entry_ts: float
    exit_ts: float
    entry_price: float
    exit_price: float
    size_usd: float
    pnl_usd: float
    pnl_pct: float
    reason: str
    bars_held: int


class BacktestEngine:
    """Single-position simulator. mode='meme' uses the disciplined rules,
    mode='satellite' uses the ride-winner rules — both straight from
    MemeEngine, no reimplementation."""

    def __init__(self, cfg, mode: str = "meme", cost_pct: float = 0.015, size_usd: float = None):
        self.cfg = cfg
        self.mode = mode
        self.cost_pct = cost_pct
        self.size_usd = size_usd or cfg.meme_trade_cap_usd
        self.memes = MemeEngine(cfg)

    # ---- data ----
    @staticmethod
    async def fetch_ohlcv(http, pool_id: str, timeframe: str = "hour",
                          aggregate: int = 1, limit: int = 1000) -> list:
        """GeckoTerminal OHLCV, ascending (ts, o, h, l, c, v). Empty on error.
        One retry after backoff on rate-limit — free tier is tight."""
        for attempt in (1, 2):
            try:
                resp = await http.get(
                    f"{GECKOTERMINAL_BASE}/networks/solana/pools/{pool_id}/ohlcv/{timeframe}",
                    params={"aggregate": aggregate, "limit": min(limit, 1000),
                            "currency": "usd", "token": "base"},
                    headers={"Accept": "application/json"},
                )
                if resp.status_code == 429 and attempt < 2:
                    await asyncio.sleep(10)
                    continue
                resp.raise_for_status()
                rows = resp.json().get("data", {}).get("attributes", {}).get("ohlcv_list", []) or []
                bars = sorted((int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                               float(r[4]), float(r[5])) for r in rows)
                return [b for b in bars if b[4] > 0]  # drop zero-close junk
            except Exception as e:
                logger.warning(f"ohlcv fetch failed for {pool_id}: {e}")
                if attempt < 2:
                    await asyncio.sleep(10)
        return []

    # ---- simulation ----
    def run(self, bars: list, liquidity_usd: float, created_ms: int,
            source: str = "market") -> dict:
        """Replay ascending bars (ts_s, o, h, l, c, v). Returns stats + trades."""
        trades, equity_curve = [], []
        pos = None          # exit-engine position dict
        state = None        # open-trade bookkeeping
        cash = self.size_usd

        for i in range(WARMUP_BARS, len(bars)):
            ts, _o, h, _l, c, _v = bars[i]

            if pos is not None:
                pos["peak_price"] = max(pos["peak_price"], h)  # intra-bar peak
                pos["current_price"] = c
                decision = (self.memes.satellite_exit(pos, ts) if self.mode == "satellite"
                            else self.memes.exit_decision(pos, ts))
                if decision is not None:
                    fraction, reason = decision
                    sell_amount = state["amount"] * fraction
                    proceeds = sell_amount * c * (1.0 - self.cost_pct)
                    cost_basis = state["size_remaining"] * fraction
                    pnl = proceeds - cost_basis
                    cash += proceeds
                    state["size_remaining"] -= cost_basis
                    state["amount"] -= sell_amount
                    state["pnl"] += pnl
                    if "take_profit_half" in reason:
                        pos["tp_half_done"] = True
                    if fraction >= 1.0 or state["amount"] <= 0:
                        entry_fill = state["entry_price"]
                        trades.append(BtTrade(
                            entry_ts=state["entry_ts"], exit_ts=ts,
                            entry_price=entry_fill, exit_price=c * (1.0 - self.cost_pct),
                            size_usd=self.size_usd, pnl_usd=state["pnl"],
                            pnl_pct=state["pnl"] / self.size_usd, reason=reason,
                            bars_held=i - state["entry_bar"],
                        ))
                        pos, state = None, None
                        equity_curve.append(cash)  # flat this bar, no same-bar re-entry
                        continue

            if pos is None:
                cand = self._candidate_from_bars(bars, i, liquidity_usd, created_ms, source)
                if cand is not None:
                    gate = (self.memes.satellite_gate(cand, ts) if self.mode == "satellite"
                            else self.memes.passes_gate(cand, ts))
                    if gate[0]:
                        entry_fill = c * (1.0 + self.cost_pct)
                        pos = {
                            "entry_price": entry_fill, "current_price": c,
                            "peak_price": max(c, h), "opened_at": ts, "tp_half_done": False,
                        }
                        state = {
                            "entry_ts": ts, "entry_bar": i, "entry_price": entry_fill,
                            "amount": self.size_usd / entry_fill,
                            "size_remaining": self.size_usd, "pnl": 0.0,
                        }
                        cash -= self.size_usd

            mark = state["amount"] * c if state else 0.0
            equity_curve.append(cash + mark)

        # liquidate any still-open position at the last close for accounting
        if state is not None and bars:
            ts, _o, _h, _l, c, _v = bars[-1]
            proceeds = state["amount"] * c * (1.0 - self.cost_pct)
            state["pnl"] += proceeds - state["size_remaining"]
            cash += proceeds
            trades.append(BtTrade(
                entry_ts=state["entry_ts"], exit_ts=ts,
                entry_price=state["entry_price"], exit_price=c * (1.0 - self.cost_pct),
                size_usd=self.size_usd, pnl_usd=state["pnl"],
                pnl_pct=state["pnl"] / self.size_usd, reason="end_of_window",
                bars_held=len(bars) - 1 - state["entry_bar"],
            ))
            if equity_curve:
                equity_curve[-1] = cash

        return self._summarize(trades, equity_curve, self.size_usd)

    def _candidate_from_bars(self, bars, i, liquidity_usd, created_ms, source):
        ts, _o, _h, _l, c, _v = bars[i]
        prev1, prev6 = bars[i - 1][4], bars[i - 6][4]
        if prev1 <= 0 or prev6 <= 0 or c <= 0:
            return None
        vol24 = sum(b[5] for b in bars[i - 23:i + 1])
        return CandidateToken(
            mint="BACKTEST", symbol="BT", price_usd=c, liquidity_usd=liquidity_usd,
            volume_h24=vol24, pair_created_ms=created_ms,
            price_change_h1=(c / prev1 - 1.0) * 100.0,
            price_change_h6=(c / prev6 - 1.0) * 100.0,
            source=source,
        )

    @staticmethod
    def _summarize(trades: list, equity_curve: list, size_usd: float) -> dict:
        wins = [t for t in trades if t.pnl_usd > 0]
        losses = [t for t in trades if t.pnl_usd <= 0]
        gross_win = sum(t.pnl_usd for t in wins)
        gross_loss = abs(sum(t.pnl_usd for t in losses))
        win_rate = len(wins) / len(trades) if trades else None
        avg_win = gross_win / len(wins) if wins else 0.0
        avg_loss = -gross_loss / len(losses) if losses else 0.0
        peak, max_dd = 0.0, 0.0
        for eq in equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak)
        return {
            "trades": trades,
            "n_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "expectancy": (win_rate * avg_win + (1 - win_rate) * avg_loss) if win_rate is not None else 0.0,
            "total_pnl": sum(t.pnl_usd for t in trades),
            "total_return_pct": (sum(t.pnl_usd for t in trades) / size_usd) if size_usd > 0 else None,
            "max_drawdown": max_dd,
            "avg_bars_held": (sum(t.bars_held for t in trades) / len(trades)) if trades else 0.0,
        }
