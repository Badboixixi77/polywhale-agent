"""Execution sizing tests — dollars must convert to input-token units correctly.

2026-08-13 live incident: buy() treated SOL like a stablecoin, so a $6
satellite slice became a 6 SOL (~$456) transfer and failed preflight with
'insufficient lamports'. These tests lock the SOL-by-price sizing.
"""
import base64

import pytest

from execution import ExecutionLayer
from state import Ledger
from helpers import make_cfg

SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TOKEN = "FakeMint1111111111111111111111111111111111"


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeHttp:
    """Price v3 + swap quote; records the quoted input amount."""

    def __init__(self, sol_price=76.0, out_amount="10000000"):
        self.sol_price = sol_price
        self.out_amount = out_amount
        self.last_quote_amount = None

    async def get(self, url, params=None, **kw):
        if "price/v3" in url:
            return FakeResp({SOL: {"usdPrice": self.sol_price}})
        if "quote" in url:
            self.last_quote_amount = int(params["amount"])
            return FakeResp({"outAmount": self.out_amount})
        return FakeResp({})

    async def post(self, url, json=None, **kw):
        # SPL mint layout: decimals at byte offset 44 (here: 6)
        data = b"\x00" * 44 + bytes([6]) + b"\x00" * 37
        return FakeResp({"result": {"value": {
            "data": [base64.b64encode(data).decode(), "base64"]}}})


@pytest.fixture()
def ledger(tmp_path):
    led = Ledger(str(tmp_path / "test.db"))
    yield led
    led.close()


def test_sol_input_sized_by_price(ledger):
    """$6 at SOL=$76 must quote ~0.079 SOL (78.9M lamports), not 6 SOL."""
    cfg = make_cfg(dry_run=True)
    http = FakeHttp(sol_price=76.0)
    ex = ExecutionLayer(cfg, ledger, http)
    import asyncio

    fill = asyncio.run(ex.buy("satellite", SOL, TOKEN, "TEST", 6.0))
    assert fill is not None
    expected = int(6.0 / 76.0 * 1e9)
    assert abs(http.last_quote_amount - expected) / expected < 0.01
    assert fill["usd"] == pytest.approx(6.0)


def test_stablecoin_input_stays_one_to_one(ledger):
    """USDC input keeps the legacy $->unit mapping (6 decimals)."""
    cfg = make_cfg(dry_run=True)
    http = FakeHttp()
    ex = ExecutionLayer(cfg, ledger, http)
    import asyncio

    fill = asyncio.run(ex.buy("meme", USDC, TOKEN, "TEST", 2.0))
    assert fill is not None
    assert http.last_quote_amount == 2_000_000


def test_sol_input_fails_closed_without_price(ledger):
    class DeadHttp(FakeHttp):
        async def get(self, url, params=None, **kw):
            raise RuntimeError("no price")

    cfg = make_cfg(dry_run=True)
    ex = ExecutionLayer(cfg, ledger, DeadHttp())
    import asyncio

    fill = asyncio.run(ex.buy("satellite", SOL, TOKEN, "TEST", 6.0))
    assert fill is None
