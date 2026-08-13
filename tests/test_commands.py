"""Tests for the Telegram command channel (auth, backlog, dispatch)."""
import asyncio

from commands import CommandHandler, HELP_TEXT
from state import Ledger
from helpers import make_cfg

CHAT = 424242  # fake chat id for tests
OTHER = 999


class FakeOps:
    def __init__(self):
        self.calls = []

    def ops_status(self):
        self.calls.append("status")
        return "STATUS"

    def ops_halt(self):
        self.calls.append("halt")
        return "HALTED"

    def ops_resume(self):
        self.calls.append("resume")
        return "RESUMED"

    def ops_reset_kill(self):
        self.calls.append("reset_kill")
        return "RESET"

    def ops_report(self):
        self.calls.append("report")
        return "REPORT"

    def ops_metrics(self):
        self.calls.append("metrics")
        return "METRICS"


class FakeNotifier:
    enabled = True

    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self, updates):
        self.updates = updates
        self.requests = []

    async def get(self, url, params=None, timeout=None):
        self.requests.append((url, dict(params or {})))
        return FakeResponse({"ok": True, "result": self.updates})


def update(uid, chat_id, text):
    return {"update_id": uid, "message": {"chat": {"id": chat_id}, "text": text}}


def make_handler(tmp_path, updates, chat_id=CHAT):
    cfg = make_cfg(telegram_bot_token="token", telegram_chat_id=str(chat_id))
    ledger = Ledger(str(tmp_path / "cmd.db"))
    ops = FakeOps()
    notifier = FakeNotifier()
    http = FakeHttp(updates)
    return CommandHandler(http, cfg, ops, notifier, ledger), ops, notifier, ledger


def test_first_run_skips_backlog(tmp_path):
    handler, ops, notifier, _ = make_handler(tmp_path, [update(1, CHAT, "/start")])
    handled = asyncio.run(handler.poll_once())
    assert handled == 0
    assert ops.calls == []
    assert notifier.sent == []


def test_command_dispatch_and_reply(tmp_path):
    handler, ops, notifier, ledger = make_handler(tmp_path, [])
    ledger.set_meta("tg_offset", 1)  # past first-run
    handler.http.updates = [update(2, CHAT, "/status")]
    handled = asyncio.run(handler.poll_once())
    assert handled == 1
    assert ops.calls == ["status"]
    assert notifier.sent == ["STATUS"]
    assert ledger.get_meta("tg_offset") == "3"


def test_unauthorized_chat_ignored(tmp_path):
    handler, ops, notifier, ledger = make_handler(tmp_path, [])
    ledger.set_meta("tg_offset", 1)
    handler.http.updates = [update(2, OTHER, "/halt")]
    handled = asyncio.run(handler.poll_once())
    assert handled == 0
    assert ops.calls == []
    assert notifier.sent == []
    # offset still advances so the update is never re-seen
    assert ledger.get_meta("tg_offset") == "3"


def test_offset_persists_across_restart(tmp_path):
    handler, _, _, ledger = make_handler(tmp_path, [update(5, CHAT, "/start")])
    asyncio.run(handler.poll_once())
    assert ledger.get_meta("tg_offset") == "6"
    # second handler instance (simulated restart) resumes from stored offset
    handler2, _, _, _ = make_handler(tmp_path, [])
    asyncio.run(handler2.poll_once())
    assert handler2.http.requests[0][1].get("offset") == 6


def test_all_commands_route(tmp_path):
    handler, ops, _, ledger = make_handler(tmp_path, [])
    ledger.set_meta("tg_offset", 1)
    cmds = ["/status", "/halt", "/resume", "/reset_kill", "/report", "/metrics"]
    for i, c in enumerate(cmds):
        handler.http.updates = [update(10 + i, CHAT, c)]
        asyncio.run(handler.poll_once())
    assert ops.calls == ["status", "halt", "resume", "reset_kill", "report", "metrics"]


def test_help_and_unknown(tmp_path):
    handler, _, notifier, ledger = make_handler(tmp_path, [])
    ledger.set_meta("tg_offset", 1)
    assert asyncio.run(handler.handle("/help")) == HELP_TEXT
    assert asyncio.run(handler.handle("/bogus")).startswith("Unknown command.")
    # bot-mention suffix is stripped before routing
    handler.http.updates = [update(2, CHAT, "/help@PolyWhaleAgent_bot")]
    asyncio.run(handler.poll_once())
    assert notifier.sent[-1] == HELP_TEXT
