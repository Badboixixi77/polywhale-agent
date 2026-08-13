"""Risk cage tests: caps, budgets, daily halt, kill switch."""
import pytest

from risk import RiskEngine
from state import Ledger
from helpers import make_cfg


@pytest.fixture
def env(tmp_path):
    cfg = make_cfg()
    ledger = Ledger(str(tmp_path / "test.db"))
    yield cfg, ledger, RiskEngine(cfg, ledger)
    ledger.close()


def _open_meme(ledger, mint, usd=2.0):
    ledger.open_or_add_position("meme", mint, "TOK", usd, 1000.0, 0.002)


# ---- per-trade cap ----
def test_per_trade_cap_blocks_oversized(env):
    _, _, risk = env
    ok, reason = risk.approve_entry("meme", 3.0)
    assert not ok and "cap" in reason


def test_per_trade_cap_allows_capped_size(env):
    _, _, risk = env
    ok, _ = risk.approve_entry("meme", 2.0)
    assert ok


# ---- concurrent position cap ----
def test_max_concurrent_positions(env):
    _, ledger, risk = env
    for i in range(5):
        _open_meme(ledger, f"mint{i}")
    ok, reason = risk.approve_entry("meme", 2.0)
    assert not ok and "max meme positions" in reason


# ---- sleeve budgets ----
def test_meme_budget_exhaustion(env):
    _, ledger, risk = env
    _open_meme(ledger, "mintA", usd=9.0)
    ok, reason = risk.approve_entry("meme", 2.0)
    assert not ok and "budget" in reason


def test_majors_budget_exhaustion(env):
    _, ledger, risk = env
    ledger.open_or_add_position("majors", "SOLMINT", "SOL", 9.0, 0.05, 180.0)
    ok, reason = risk.approve_entry("majors", 2.0)
    assert not ok and "budget" in reason


def test_majors_budget_allows_within_limit(env):
    _, ledger, risk = env
    ledger.open_or_add_position("majors", "SOLMINT", "SOL", 6.0, 0.033, 180.0)
    ok, _ = risk.approve_entry("majors", 2.0)
    assert ok


def test_unknown_sleeve_rejected(env):
    _, _, risk = env
    ok, _ = risk.approve_entry("forex", 1.0)
    assert not ok


# ---- kill switch ----
def test_kill_switch_blocks_everything(env):
    _, ledger, risk = env
    ledger.set_meta("kill_switch", "1")
    ok, reason = risk.approve_entry("meme", 1.0)
    assert not ok and "kill" in reason
    risk.reset_kill_switch()
    ok, _ = risk.approve_entry("meme", 1.0)
    assert ok


# ---- watchdog: daily halt ----
def test_daily_loss_triggers_halt(env):
    _, ledger, risk = env
    risk.watchdog_tick(20.0)                 # anchors day start at $20
    status = risk.watchdog_tick(18.5)        # -$1.50 > 5% of $20 ($1.00)
    assert status["halted"]
    ok, reason = risk.approve_entry("meme", 1.0)
    assert not ok and "halt" in reason


def test_small_daily_loss_does_not_halt(env):
    _, _, risk = env
    risk.watchdog_tick(20.0)
    status = risk.watchdog_tick(19.6)        # -$0.40, under the $1.00 limit
    assert not status["halted"]


# ---- watchdog: drawdown kill switch ----
def test_drawdown_triggers_kill_switch(env):
    _, ledger, risk = env
    risk.watchdog_tick(20.0)                 # peak $20
    status = risk.watchdog_tick(15.9)        # -20.5% drawdown
    assert status["killed"]
    assert ledger.get_meta("kill_switch") == "1"


def test_drawdown_below_threshold_survives(env):
    _, _, risk = env
    risk.watchdog_tick(20.0)
    status = risk.watchdog_tick(17.0)        # -15%, above the 20% kill level
    assert not status["killed"]


# ---- watchdog: weekly profit lock ----
def test_weekly_profit_lock_engages_at_target(env):
    _, ledger, risk = env                    # default lock = $100
    risk.watchdog_tick(50.0)                 # anchors week start at $50
    status = risk.watchdog_tick(150.0)       # +$100 for the week
    assert status["weekly_halted"]
    assert status["weekly_pnl"] >= 100.0
    ok, reason = risk.approve_entry("meme", 2.0)
    assert not ok and "weekly profit lock" in reason
    ok, reason = risk.approve_entry("satellite", 5.0)
    assert not ok and "weekly profit lock" in reason


def test_weekly_profit_below_target_keeps_trading(env):
    _, _, risk = env
    risk.watchdog_tick(50.0)
    status = risk.watchdog_tick(120.0)       # +$70: under the $100 lock
    assert not status["weekly_halted"]


def test_weekly_lock_disabled_at_zero(env):
    _, ledger, risk = env
    cfg2 = make_cfg(weekly_profit_lock_usd=0.0)
    risk2 = RiskEngine(cfg2, ledger)
    risk2.watchdog_tick(50.0)
    status = risk2.watchdog_tick(500.0)      # huge gain, but lock disabled
    assert not status["weekly_halted"]


def test_week_anchor_rolls_over(env):
    _, ledger, risk = env
    risk.watchdog_tick(100.0)
    ledger.set_meta("week_key", "1999-W01")  # simulate week rollover
    ledger.set_meta("week_start_equity", 999.0)
    status = risk.watchdog_tick(100.0)
    assert status["weekly_pnl"] == 0.0       # re-anchored at current equity
    assert ledger.get_meta("week_key") != "1999-W01"
