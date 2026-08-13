"""Tests for the metrics engine and HTML dashboard."""
import os

from metrics import MetricsEngine
from state import Ledger
from helpers import make_cfg


def make_ledger(tmp_path):
    return Ledger(str(tmp_path / "ledger.db"))


def seed_closed_trades(ledger):
    # win: cost 2, exit +0.50 pnl ; loss: cost 2, pnl -0.80
    ledger.open_or_add_position("meme", "MINTW", "WIN", 2.0, 1000.0, 0.002)
    ledger.reduce_position(1, 1.0, 0.0025)  # proceeds 2.5 -> pnl +0.5
    ledger.open_or_add_position("meme", "MINTL", "LOSS", 2.0, 1000.0, 0.002)
    ledger.reduce_position(2, 1.0, 0.0012)  # proceeds 1.2 -> pnl -0.8


def test_compute_stats(tmp_path):
    ledger = make_ledger(tmp_path)
    seed_closed_trades(ledger)
    m = MetricsEngine(make_cfg(), ledger)
    s = m.compute()
    assert s["closed_trades"] == 2
    assert s["wins"] == 1 and s["losses"] == 1
    assert s["win_rate"] == 0.5
    assert abs(s["profit_factor"] - 0.5 / 0.8) < 1e-9
    assert abs(s["avg_win"] - 0.5) < 1e-9
    assert abs(s["avg_loss"] + 0.8) < 1e-9
    assert abs(s["realized_pnl"] + 0.3) < 1e-9


def test_compute_empty_ledger(tmp_path):
    m = MetricsEngine(make_cfg(), make_ledger(tmp_path))
    s = m.compute()
    assert s["closed_trades"] == 0
    assert s["win_rate"] is None
    assert s["profit_factor"] is None
    assert s["max_drawdown"] == 0.0


def test_max_drawdown():
    assert MetricsEngine._max_drawdown([20, 22, 18, 21, 15]) == (22 - 15) / 22


def test_render_text_handles_no_trades(tmp_path):
    m = MetricsEngine(make_cfg(), make_ledger(tmp_path))
    text = m.render_text()
    assert "Win rate: n/a" in text
    assert "Profit factor: n/a" in text
    assert "PAPER" in text


def test_dashboard_html_and_file(tmp_path):
    ledger = make_ledger(tmp_path)
    seed_closed_trades(ledger)
    for i, eq in enumerate([20.0, 20.5, 19.8, 21.0]):
        ledger.record_equity(1_755_000_000 + i * 60, eq)
    reports_dir = str(tmp_path / "reports")
    m = MetricsEngine(make_cfg(), ledger, reports_dir=reports_dir)
    html = m.render_html()
    assert "PolyWhale Dashboard" in html
    assert "<svg" in html
    assert "high $21.00" in html
    path = m.write_dashboard()
    assert os.path.exists(path)
    with open(path) as f:
        assert "PolyWhale Dashboard" in f.read()


def test_dashboard_placeholder_with_few_samples(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.record_equity(1_755_000_000, 20.0)
    m = MetricsEngine(make_cfg(), ledger, reports_dir=str(tmp_path / "r"))
    assert "Not enough equity samples" in m.render_html()
