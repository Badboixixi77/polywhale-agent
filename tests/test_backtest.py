"""Backtester + sleeve attribution tests on synthetic bars / ledgers."""
import pytest

from backtest import BacktestEngine
from metrics import MetricsEngine
from state import Ledger
from helpers import make_cfg

START = 1_700_000_000  # seconds


def bars_from(prices, step=3600, vol=5_000_000.0, start_ts=START):
    out = []
    for i, c in enumerate(prices):
        o = prices[i - 1] if i > 0 else c
        out.append((start_ts + i * step, o, max(o, c), min(o, c), c, vol))
    return out


def meme_cfg(**kw):
    # market_min_h6_pct=0 keeps synthetic scenarios gate-neutral; the
    # momentum-floor experiments use the engine's own min_h6_pct knob
    base = dict(min_market_liquidity_usd=1000.0, min_market_age_hours=1.0,
                market_min_h6_pct=0.0)
    base.update(kw)
    return make_cfg(**base)


OLD_MS = (START - 100 * 3600) * 1000  # pool created 100h before the window


# ---- meme-mode mechanics ----

def test_meme_mode_take_profit_then_trailing_stop():
    prices = [1.0] * 30 + [1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.2]
    engine = BacktestEngine(meme_cfg(), mode="meme", cost_pct=0.015)
    stats = engine.run(bars_from(prices), 50000.0, OLD_MS)
    assert stats["n_trades"] == 1
    assert stats["wins"] == 1 and stats["win_rate"] == 1.0
    t = stats["trades"][0]
    assert t.reason.startswith("trailing_stop")  # half TP sold, trail closed the rest
    assert t.pnl_usd > 0


def test_meme_mode_stop_loss_bounds_the_damage():
    prices = [1.0] * 30 + [0.8, 0.64, 0.55]
    engine = BacktestEngine(meme_cfg(), mode="meme", cost_pct=0.015)
    stats = engine.run(bars_from(prices), 50000.0, OLD_MS)
    assert stats["n_trades"] == 1
    t = stats["trades"][0]
    assert t.reason.startswith("stop_loss")
    assert t.pnl_usd < 0
    assert -0.55 < t.pnl_pct < -0.40  # loss bounded near the -40% stop + costs


def test_gate_rejects_too_young_pool():
    prices = [1.0] * 30 + [1.3, 1.7, 2.2]
    engine = BacktestEngine(meme_cfg(min_market_age_hours=48.0), mode="meme")
    created_ms = (START + 29 * 3600) * 1000  # only ~1h old at the ramp
    stats = engine.run(bars_from(prices), 50000.0, created_ms)
    assert stats["n_trades"] == 0
    assert stats["win_rate"] is None
    assert stats["total_pnl"] == 0.0


def test_low_liquidity_produces_no_trades():
    prices = [1.0] * 30 + [1.4, 1.8]
    engine = BacktestEngine(meme_cfg(), mode="meme")
    stats = engine.run(bars_from(prices), 10.0, OLD_MS)  # $10 liq < $1k floor
    assert stats["n_trades"] == 0
    assert stats["max_drawdown"] == 0.0


def test_momentum_floor_filters_dead_tape_entries():
    # flat tape: entries happen with no floor, vanish with a +2% h6 floor
    prices = [1.0] * 30 + [1.0, 1.01, 0.99, 1.0]  # h6 never reaches +2%
    bars = bars_from(prices)
    baseline = BacktestEngine(meme_cfg(), mode="meme").run(bars, 50000.0, OLD_MS)
    floored = BacktestEngine(meme_cfg(), mode="meme", min_h6_pct=2.0).run(
        bars, 50000.0, OLD_MS)
    assert baseline["n_trades"] >= 1
    assert floored["n_trades"] == 0
    # but a genuine ramp passes the floor and still trades
    ramp = [1.0] * 30 + [1.05, 1.12, 1.2, 1.3]
    ramped = BacktestEngine(meme_cfg(), mode="meme", min_h6_pct=2.0).run(
        bars_from(ramp), 50000.0, OLD_MS)
    assert ramped["n_trades"] >= 1


# ---- satellite-mode mechanics ----

def test_satellite_rides_past_take_profit_level():
    # +10%/bar ramp to 3x — meme rules would half-exit at +50%, satellite rides
    prices = [1.0] * 30 + [1.1 * 1.0] * 1
    p = 1.1
    for _ in range(11):
        p *= 1.10
        prices.append(p)          # peak ~3.14
    prices += [p * 0.78, p * 0.70]  # 25%-trail breach
    engine = BacktestEngine(meme_cfg(), mode="satellite", cost_pct=0.0, size_usd=5.0)
    stats = engine.run(bars_from(prices), 50000.0, OLD_MS)
    assert stats["n_trades"] == 1
    t = stats["trades"][0]
    assert t.reason.startswith("satellite_trail")
    assert t.pnl_pct > 1.0  # rode a double, never took half at +50%


def test_satellite_wide_stop_on_slow_bleed():
    prices = [1.0] * 30
    p = 1.0
    for _ in range(8):
        p *= 0.90
        prices.append(p)          # bleeds to ~0.43 (-57%)
    prices.append(p * 0.82)       # ret <= -60% vs fill -> stop
    engine = BacktestEngine(meme_cfg(), mode="satellite", cost_pct=0.0, size_usd=5.0)
    stats = engine.run(bars_from(prices), 50000.0, OLD_MS)
    assert stats["n_trades"] == 1
    t = stats["trades"][0]
    assert t.reason.startswith("satellite_stop")
    assert t.pnl_pct < -0.60


# ---- summary math ----

def test_summary_expectancy_and_drawdown():
    # one winning ramp then one losing dump in the same window
    prices = [1.0] * 30 + [1.1, 1.2, 1.4, 1.6, 1.2]  # trade 1 wins
    prices += [1.2] * 2 + [1.0, 0.8, 0.7]             # trade 2 stops out
    engine = BacktestEngine(meme_cfg(), mode="meme", cost_pct=0.0)
    stats = engine.run(bars_from(prices), 50000.0, OLD_MS)
    assert stats["n_trades"] == 2
    assert stats["wins"] == 1 and stats["losses"] == 1
    assert stats["win_rate"] == 0.5
    exp = 0.5 * stats["avg_win"] + 0.5 * stats["avg_loss"]
    assert stats["expectancy"] == pytest.approx(exp)
    assert stats["total_pnl"] == pytest.approx(
        sum(t.pnl_usd for t in stats["trades"]))
    assert stats["max_drawdown"] > 0.0


# ---- sleeve attribution ----

def test_sleeve_attribution_split_by_machine(tmp_path):
    cfg = make_cfg()
    ledger = Ledger(str(tmp_path / "attr.db"))
    try:
        ledger.open_or_add_position("meme", "MINTA", "AAA", 2.0, 2.0, 1.0)
        pid = [p for p in ledger.open_positions() if p["mint"] == "MINTA"][0]["id"]
        ledger.reduce_position(pid, 1.0, 1.5)  # +$1.00
        ledger.open_or_add_position("satellite", "MINTB", "BBB", 5.0, 5.0, 1.0)
        pid = [p for p in ledger.open_positions() if p["mint"] == "MINTB"][0]["id"]
        ledger.reduce_position(pid, 1.0, 0.5)  # -$2.50

        m = MetricsEngine(cfg, ledger)
        sleeves = m.sleeve_stats()
        assert set(sleeves) == {"meme", "satellite"}
        assert sleeves["meme"]["trades"] == 1
        assert sleeves["meme"]["win_rate"] == 1.0
        assert sleeves["meme"]["pnl"] == pytest.approx(1.0)
        assert sleeves["meme"]["expectancy"] == pytest.approx(1.0)
        assert sleeves["satellite"]["losses"] == 1
        assert sleeves["satellite"]["pnl"] == pytest.approx(-2.5)
        assert sleeves["satellite"]["expectancy"] == pytest.approx(-2.5)

        text = m.render_text()
        assert "[meme]" in text and "[satellite]" in text
        html = m.render_html()
        assert "Sleeve attribution" in html
    finally:
        ledger.close()
