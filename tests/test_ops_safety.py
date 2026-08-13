"""Tests for the dead-man's-switch watchdog and on-chain reconciliation logic."""
from watchdog import Watchdog, evaluate, read_heartbeat
from execution import find_drift
from state import Ledger

NOW = 1_755_000_000.0


# ---- heartbeat evaluation ----
def test_evaluate_ok_stale_never():
    assert evaluate(NOW, NOW - 10) == "ok"
    assert evaluate(NOW, NOW - 180) == "ok"      # exactly at threshold = ok
    assert evaluate(NOW, NOW - 181) == "stale"
    assert evaluate(NOW, None) == "never"


def test_read_heartbeat_from_ledger(tmp_path):
    db = str(tmp_path / "hb.db")
    ledger = Ledger(db)
    assert read_heartbeat(db) is None
    ledger.set_meta("last_heartbeat", NOW - 5)
    ledger.close()
    assert read_heartbeat(db) == NOW - 5
    # missing file degrades to None, never raises
    assert read_heartbeat(str(tmp_path / "nope.db")) is None


# ---- watchdog state machine ----
def make_watchdog(tmp_path):
    sent = []
    wd = Watchdog("token", "123", db_path=str(tmp_path / "hb.db"), sender=sent.append)
    wd.started_at = NOW - 3600  # past the never-grace period
    return wd, sent


def stamp(tmp_path, ts):
    ledger = Ledger(str(tmp_path / "hb.db"))
    ledger.set_meta("last_heartbeat", ts)
    ledger.close()


def test_healthy_heartbeat_no_alert(tmp_path):
    wd, sent = make_watchdog(tmp_path)
    stamp(tmp_path, NOW - 30)
    assert wd.tick(now=NOW) == "ok"
    assert sent == []


def test_stale_heartbeat_alerts(tmp_path):
    wd, sent = make_watchdog(tmp_path)
    stamp(tmp_path, NOW - 300)
    assert wd.tick(now=NOW) == "alerted"
    assert "HEARTBEAT LOST" in sent[0]
    assert "300s" in sent[0]


def test_cooldown_suppresses_repeat_alerts(tmp_path):
    wd, sent = make_watchdog(tmp_path)
    stamp(tmp_path, NOW - 300)
    assert wd.tick(now=NOW) == "alerted"
    assert wd.tick(now=NOW + 60) == "ok"     # still stale, but inside cooldown
    assert wd.tick(now=NOW + 601) == "alerted"  # cooldown over -> re-alert
    assert len(sent) == 2


def test_recovery_message(tmp_path):
    wd, sent = make_watchdog(tmp_path)
    stamp(tmp_path, NOW - 300)
    wd.tick(now=NOW)                          # alert
    stamp(tmp_path, NOW + 60 - 10)
    assert wd.tick(now=NOW + 60) == "recovered"
    assert "restored" in sent[-1]
    assert wd.tick(now=NOW + 120) == "ok"     # no duplicate recovery


def test_never_stamped_grace_period(tmp_path):
    wd, sent = make_watchdog(tmp_path)
    wd.started_at = NOW - 60                  # watchdog just started
    assert wd.tick(now=NOW) == "ok"           # grace: no alarm yet
    assert sent == []
    wd.started_at = NOW - 700                 # grace over, still never stamped
    assert wd.tick(now=NOW) == "alerted"
    assert "never stamped" in sent[0]


# ---- reconciliation drift detection ----
def test_find_drift_detects_shortfall():
    expected = [("SOL", "SOL", 0.15), ("DOGE42", "MINTX", 1000.0)]
    actual = {"SOL": 0.16, "MINTX": 500.0}
    drift = find_drift(expected, actual)
    assert len(drift) == 1
    assert "DOGE42" in drift[0]


def test_find_drift_tolerates_fee_dust():
    # 0.5% shortfall is inside the 1% tolerance
    drift = find_drift([("SOL", "SOL", 1.0)], {"SOL": 0.995})
    assert drift == []


def test_find_drift_missing_account_is_total_shortfall():
    drift = find_drift([("MYSTERY", "MINTZ", 10.0)], {})
    assert len(drift) == 1 and "MYSTERY" in drift[0]


def test_find_drift_ignores_excess():
    # more on-chain than the ledger expects is informational, not a hazard
    drift = find_drift([("SOL", "SOL", 1.0)], {"SOL": 5.0})
    assert drift == []
