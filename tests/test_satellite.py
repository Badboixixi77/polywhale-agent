"""Satellite sleeve tests: loose gate, ride-winner exits, compounded slice."""
import pytest

from cognition import MemeEngine
from risk import RiskEngine
from state import Ledger
from perception import CandidateToken
from helpers import make_cfg

NOW = 1_755_000_000.0


def make_candidate(**kw):
    base = dict(
        mint="SATMINT", symbol="RIDE", price_usd=0.05, liquidity_usd=50000.0,
        volume_h24=200000.0, pair_created_ms=int((NOW - 5 * 3600) * 1000),
        price_change_h1=2.0, price_change_h6=10.0, source="market",
    )
    base.update(kw)
    return CandidateToken(**base)


def make_pos(entry=1.0, current=1.0, peak=None, opened_at=NOW - 3600):
    return {
        "id": 1, "entry_price": entry, "current_price": current,
        "peak_price": peak if peak is not None else max(entry, current),
        "opened_at": opened_at,
    }


# ---- gate: loose by design ----
def test_gate_allows_fresh_pools():
    eng = MemeEngine(make_cfg())
    ok, _ = eng.satellite_gate(make_candidate(), NOW)  # 5h old
    assert ok


def test_gate_rejects_barely_minted():
    eng = MemeEngine(make_cfg())
    ok, reason = eng.satellite_gate(
        make_candidate(pair_created_ms=int((NOW - 1800) * 1000)), NOW)
    assert not ok and "age" in reason


def test_gate_rejects_thin_liquidity():
    eng = MemeEngine(make_cfg())
    ok, reason = eng.satellite_gate(make_candidate(liquidity_usd=5000.0), NOW)
    assert not ok and "liquidity" in reason


def test_gate_still_blocks_blowoff_and_knives():
    eng = MemeEngine(make_cfg())
    ok, reason = eng.satellite_gate(make_candidate(price_change_h6=400.0), NOW)
    assert not ok and "blow-off" in reason
    ok, reason = eng.satellite_gate(make_candidate(price_change_h1=-50.0), NOW)
    assert not ok and "falling knife" in reason


# ---- exit: ride winners, wide leash ----
def test_no_take_profit_rides():
    eng = MemeEngine(make_cfg())
    assert eng.satellite_exit(make_pos(entry=1.0, current=3.0), NOW) is None  # +200%, still riding


def test_no_time_limit():
    eng = MemeEngine(make_cfg())
    pos = make_pos(entry=1.0, current=1.1, opened_at=NOW - 400 * 3600)  # 400h, small gain
    assert eng.satellite_exit(pos, NOW) is None


def test_wide_stop_only_at_minus_60():
    eng = MemeEngine(make_cfg())
    assert eng.satellite_exit(make_pos(current=0.50), NOW) is None  # -50%: hold
    decision = eng.satellite_exit(make_pos(current=0.35), NOW)       # -65%: exit
    assert decision == (1.0, "satellite_stop (-65%)")


def test_trailing_locks_profit_not_losses():
    eng = MemeEngine(make_cfg())
    # peaked at 2.0 (in profit), now 1.40 = 30% off peak -> trail fires
    decision = eng.satellite_exit(make_pos(entry=1.0, current=1.40, peak=2.0), NOW)
    assert decision is not None and "satellite_trail" in decision[1]
    # never got above entry: trail must not fire even on a 30% dip
    assert eng.satellite_exit(make_pos(entry=1.0, current=0.70, peak=0.95), NOW) is None


# ---- risk: one coin, slice is the risk unit ----
@pytest.fixture
def env(tmp_path):
    cfg = make_cfg()
    ledger = Ledger(str(tmp_path / "test.db"))
    yield cfg, ledger, RiskEngine(cfg, ledger)
    ledger.close()


def test_satellite_approves_up_to_capital(env):
    cfg, ledger, risk = env
    ledger.set_meta("satellite_capital", cfg.satellite_budget_usd)
    ok, _ = risk.approve_entry("satellite", 5.0)
    assert ok
    ok, reason = risk.approve_entry("satellite", 5.01)
    assert not ok and "satellite capital" in reason


def test_satellite_defaults_to_budget_when_meta_missing(env):
    _, _, risk = env
    ok, _ = risk.approve_entry("satellite", 5.0)
    assert ok


def test_satellite_compounded_capital_approves_bigger_slice(env):
    _, ledger, risk = env
    ledger.set_meta("satellite_capital", 12.5)  # slice grew through wins
    ok, _ = risk.approve_entry("satellite", 12.5)
    assert ok


def test_satellite_one_position_at_a_time(env):
    _, ledger, risk = env
    ledger.open_or_add_position("satellite", "SATMINT", "RIDE", 5.0, 100.0, 0.05)
    ok, reason = risk.approve_entry("satellite", 5.0)
    assert not ok and "already holds" in reason


def test_satellite_respects_global_cage(env):
    _, ledger, risk = env
    ledger.set_meta("kill_switch", "1")
    ok, reason = risk.approve_entry("satellite", 5.0)
    assert not ok and "kill switch" in reason
