"""Reporter and notifier tests."""
import asyncio
import time

import httpx
import pytest

from notify import Notifier
from report import Reporter
from state import Ledger
from helpers import make_cfg

STATUS = {
    "equity": 20.0,
    "daily_pnl": -0.5,
    "peak_equity": 20.0,
    "drawdown": 0.0,
    "halted": False,
    "killed": False,
}


@pytest.fixture
def env(tmp_path):
    cfg = make_cfg()
    ledger = Ledger(str(tmp_path / "test.db"))
    yield cfg, ledger
    ledger.close()


def test_summary_contains_key_figures(env):
    cfg, ledger = env
    ledger.open_or_add_position("majors", "SOLMINT", "SOL", 2.0, 0.026, 76.0)
    summary = Reporter(cfg, ledger).build_summary(STATUS)
    assert "Equity: $20.00" in summary
    assert "Day PnL: $-0.50" in summary
    assert "[majors] SOL" in summary
    assert "meme $0.00/$10.00" in summary
    assert "Mode: PAPER" in summary


def test_summary_flags_guardrail_state(env):
    cfg, ledger = env
    status = dict(STATUS, halted=True, killed=True)
    summary = Reporter(cfg, ledger).build_summary(status)
    assert "HALTED" in summary and "KILLED" in summary


def test_summary_empty_positions(env):
    cfg, ledger = env
    summary = Reporter(cfg, ledger).build_summary(STATUS)
    assert "Open positions (0)" in summary
    assert "- none" in summary


def test_write_report_creates_file(env, tmp_path):
    cfg, ledger = env
    reporter = Reporter(cfg, ledger, reports_dir=str(tmp_path / "reports"))
    path = reporter.write_report("test summary", day="2026-08-12")
    assert path.endswith("2026-08-12.md")
    with open(path) as f:
        assert "test summary" in f.read()


def test_notifier_disabled_without_credentials():
    async def run():
        async with httpx.AsyncClient() as http:
            notifier = Notifier(http)  # no token/chat id -> log-only
            assert not notifier.enabled
            await notifier.send("hello")  # must not raise or touch the network
    asyncio.run(run())


def test_fills_since(env):
    _, ledger = env
    ledger.record_fill("meme", "M", "buy", 2.0, 1000.0, 0.002, "paper")
    assert len(ledger.fills_since(time.time() - 60)) == 1
    assert len(ledger.fills_since(time.time() + 60)) == 0
