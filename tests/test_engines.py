"""Strategy engine tests: trend filter gating and meme exit math."""
import time

from cognition import MajorsEngine, MemeEngine
from perception import CandidateToken
from helpers import make_cfg

NOW = 1_755_000_000.0


def _candidate(**overrides) -> CandidateToken:
    base = dict(
        mint="MINT",
        symbol="DOGE42",
        price_usd=0.001,
        liquidity_usd=20_000.0,
        volume_h24=40_000.0,
        pair_created_ms=int((NOW - 48 * 3600) * 1000),
        price_change_h1=5.0,
        price_change_h6=20.0,
        price_change_h24=60.0,
    )
    base.update(overrides)
    return CandidateToken(**base)


def _position(entry=0.001, current=0.001, peak=None, opened=None, tp_half_done=False) -> dict:
    return {
        "entry_price": entry,
        "current_price": current,
        "peak_price": peak if peak is not None else current,
        "opened_at": opened if opened is not None else NOW - 3600,
        "tp_half_done": tp_half_done,
    }


# ---- MajorsEngine ----
def test_dca_due_when_never_bought():
    assert MajorsEngine(make_cfg()).dca_due(None)


def test_dca_not_due_immediately_after_buy():
    assert not MajorsEngine(make_cfg()).dca_due(time.time())


def test_dca_due_after_interval():
    past = time.time() - 169 * 3600
    assert MajorsEngine(make_cfg()).dca_due(past)


def test_trend_ok_above_sma():
    prices = [100.0] * 20 + [120.0]
    assert MajorsEngine(make_cfg()).trend_ok(prices)


def test_trend_blocked_below_sma():
    prices = [100.0] * 20 + [80.0]
    assert not MajorsEngine(make_cfg()).trend_ok(prices)


def test_decide_emits_dca_when_due_and_trending():
    decision = MajorsEngine(make_cfg()).decide(None, [100.0] * 20 + [120.0])
    assert decision is not None
    assert decision.sleeve == "majors" and decision.usd == 2.0


def test_decide_none_when_trend_down():
    assert MajorsEngine(make_cfg()).decide(None, [100.0] * 20 + [80.0]) is None


# ---- MemeEngine entry gate ----
def test_gate_rejects_thin_liquidity():
    ok, reason = MemeEngine(make_cfg()).passes_gate(_candidate(liquidity_usd=1_000.0))
    assert not ok and "liquidity" in reason


def test_gate_rejects_fresh_token():
    ok, reason = MemeEngine(make_cfg()).passes_gate(
        _candidate(pair_created_ms=int((NOW - 3 * 3600) * 1000)), now=NOW
    )
    assert not ok and "age" in reason


def test_gate_rejects_blow_off_top():
    ok, reason = MemeEngine(make_cfg()).passes_gate(_candidate(price_change_h6=450.0))
    assert not ok and "blow-off" in reason


def test_gate_rejects_falling_knife():
    ok, reason = MemeEngine(make_cfg()).passes_gate(_candidate(price_change_h1=-45.0))
    assert not ok and "falling knife" in reason


def test_gate_accepts_healthy_candidate():
    ok, _ = MemeEngine(make_cfg()).passes_gate(_candidate())
    assert ok


def test_score_rewards_turnover_and_momentum():
    engine = MemeEngine(make_cfg())
    hot = _candidate(volume_h24=80_000.0, price_change_h6=40.0)
    cold = _candidate(volume_h24=5_000.0, price_change_h6=-10.0)
    assert engine.score(hot) > engine.score(cold)


# ---- MemeEngine exits ----
def test_stop_loss_full_exit():
    pos = _position(entry=0.001, current=0.00055)  # -45%
    fraction, reason = MemeEngine(make_cfg()).exit_decision(pos, now=NOW)
    assert fraction == 1.0 and "stop_loss" in reason


def test_take_profit_sells_half_once():
    engine = MemeEngine(make_cfg())
    pos = _position(entry=0.001, current=0.0016)  # +60%
    fraction, reason = engine.exit_decision(pos, now=NOW)
    assert fraction == 0.5 and "take_profit_half" in reason
    pos["tp_half_done"] = True
    assert engine.exit_decision(pos, now=NOW) is None


def test_trailing_stop_after_peak():
    # peaked at +80%, now 25% below the peak -> trailing exit
    pos = _position(entry=0.001, current=0.00135, peak=0.0018)
    fraction, reason = MemeEngine(make_cfg()).exit_decision(pos, now=NOW)
    assert fraction == 1.0 and "trailing_stop" in reason


def test_max_hold_exit():
    pos = _position(entry=0.001, current=0.0011, opened=NOW - 80 * 3600)
    fraction, reason = MemeEngine(make_cfg()).exit_decision(pos, now=NOW)
    assert fraction == 1.0 and "max_hold" in reason


def test_hold_when_nothing_triggered():
    pos = _position(entry=0.001, current=0.0011)  # +10%, young position
    assert MemeEngine(make_cfg()).exit_decision(pos, now=NOW) is None
