"""Tests for whole-market discovery (GeckoTerminal) and source-aware gates."""
import asyncio

from perception import PerceptionLayer, CandidateToken
from cognition import MemeEngine
from helpers import make_cfg

NOW = 1_755_000_000.0
OLD_MS = int((NOW - 200 * 3600) * 1000)  # 200h old -> passes age gates


def pool_payload(mint="MINTABC", name="TOKEN / SOL", reserve=100000.0, volume=500000.0,
                 created_iso="2025-07-01T00:00:00Z", h6=5.0, h1=1.0):
    return {
        "id": f"solana_PAIR_{mint}",
        "attributes": {
            "name": name,
            "base_token_price_usd": "0.125",
            "reserve_in_usd": str(reserve),
            "volume_usd": {"h24": str(volume)},
            "price_change_percentage": {"h1": str(h1), "h6": str(h6), "h24": "12"},
            "pool_created_at": created_iso,
        },
        "relationships": {"base_token": {"data": {"id": f"solana_{mint}"}}},
    }


def make_candidate(source="market", liquidity=100000.0, age_ms=OLD_MS, **kw):
    return CandidateToken(
        mint=kw.get("mint", "MINTABC"), symbol="TOKEN", price_usd=0.125,
        liquidity_usd=liquidity, volume_h24=500000.0, pair_created_ms=age_ms,
        price_change_h1=1.0, price_change_h6=5.0, source=source,
    )


# ---- parsing ----
def test_pool_to_candidate_parses_fields():
    p = PerceptionLayer(make_cfg())
    c = p._pool_to_candidate(pool_payload())
    assert c is not None
    assert c.mint == "MINTABC"
    assert c.symbol == "TOKEN"
    assert c.source == "market"
    assert c.liquidity_usd == 100000.0
    assert c.volume_h24 == 500000.0
    assert abs(c.price_usd - 0.125) < 1e-9
    assert c.age_hours(NOW) > 100  # created well before NOW


def test_pool_to_candidate_skips_sol_and_stables():
    p = PerceptionLayer(make_cfg())
    from config import SOL_MINT, USDC_MINT
    assert p._pool_to_candidate(pool_payload(mint=SOL_MINT)) is None
    assert p._pool_to_candidate(pool_payload(mint=USDC_MINT)) is None
    assert p._pool_to_candidate(pool_payload(name="EURC / SOL")) is None  # stable by symbol
    assert p._pool_to_candidate(pool_payload(name="PYUSD / SOL")) is None
    assert p._pool_to_candidate({}) is None


# ---- source-aware gates ----
def test_market_gate_uses_market_thresholds():
    eng = MemeEngine(make_cfg())  # market: $25k liq / 48h ; meme: $5k / 24h
    ok, _ = eng.passes_gate(make_candidate("market", liquidity=30000.0), NOW)
    assert ok
    ok, reason = eng.passes_gate(make_candidate("market", liquidity=20000.0), NOW)
    assert not ok and "liquidity" in reason
    ok, reason = eng.passes_gate(make_candidate("market", age_ms=int((NOW - 30 * 3600) * 1000)), NOW)
    assert not ok and "age" in reason  # 30h old fails the 48h market gate


def test_meme_gate_unchanged():
    eng = MemeEngine(make_cfg())
    # 30h old + $6k liquidity: fine for memes (24h/$5k), would fail market gates
    c = make_candidate("meme", liquidity=6000.0, age_ms=int((NOW - 30 * 3600) * 1000))
    ok, _ = eng.passes_gate(c, NOW)
    assert ok


def test_candidate_defaults_to_meme_source():
    c = CandidateToken(mint="X", symbol="X", price_usd=1.0, liquidity_usd=1.0,
                       volume_h24=1.0, pair_created_ms=0)
    assert c.source == "meme"


def test_momentum_filters_apply_to_market_candidates_too():
    eng = MemeEngine(make_cfg())
    blowoff = CandidateToken(
        mint="M", symbol="T", price_usd=1.0, liquidity_usd=100000.0,
        volume_h24=1.0, pair_created_ms=OLD_MS, price_change_h6=400.0, source="market",
    )
    ok, reason = eng.passes_gate(blowoff, NOW)
    assert not ok and "blow-off" in reason


# ---- source-aware safety check ----
class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self, payload):
        self._payload = payload

    async def get(self, url, **kw):
        return FakeResp(self._payload)


def _report(mint_auth=None, freeze_auth=None, score=10, risks=(), top10_pct=5.0, lp_locked=99.0):
    return {
        "mintAuthority": mint_auth,
        "freezeAuthority": freeze_auth,
        "score_normalised": score,
        "risks": list(risks),
        "topHolders": [{"pct": top10_pct} for _ in range(10)],
        "markets": [{"lp": {"lpLockedPct": lp_locked}}],
    }


RENOUNCED = {"owner": "11111111111111111111111111111111"}  # System Program
LIVE = {"owner": "SomeWalletOwner111111111111111111111111111"}


def test_renounced_authorities_pass_both_sources():
    # RugCheck returns account objects; owner=System Program means renounced
    eng = MemeEngine(make_cfg())
    for src in ("meme", "market"):
        ok, _ = asyncio.run(eng.safety_check(
            FakeHttp(_report(mint_auth=RENOUNCED, freeze_auth=RENOUNCED, top10_pct=2.0)), "MINT", src))
        assert ok


def test_safety_meme_rejects_mint_authority():
    eng = MemeEngine(make_cfg())
    ok, reason = asyncio.run(eng.safety_check(FakeHttp(_report(mint_auth=LIVE)), "MINT", "meme"))
    assert not ok and "mint authority" in reason


def test_safety_market_tolerates_mint_authority():
    # wrapped tokens (cbBTC/WBTC style) keep mint authority legitimately
    eng = MemeEngine(make_cfg())
    ok, reason = asyncio.run(eng.safety_check(FakeHttp(_report(mint_auth=LIVE)), "MINT", "market"))
    assert ok


def test_safety_freeze_authority_blocks_both_sources():
    eng = MemeEngine(make_cfg())
    for src in ("meme", "market"):
        ok, reason = asyncio.run(eng.safety_check(FakeHttp(_report(freeze_auth=LIVE)), "MINT", src))
        assert not ok and "freeze" in reason


def test_safety_holder_cap_source_aware():
    eng = MemeEngine(make_cfg())
    # 45% top-10 concentration: fine for market (cap 80%), rejected for meme (cap 30%)
    ok, _ = asyncio.run(eng.safety_check(FakeHttp(_report(top10_pct=4.5)), "MINT", "market"))
    assert ok
    ok, reason = asyncio.run(eng.safety_check(FakeHttp(_report(top10_pct=4.5)), "MINT", "meme"))
    assert not ok and "top-10" in reason


def test_safety_lp_lock_only_enforced_for_memes():
    eng = MemeEngine(make_cfg())
    ok, _ = asyncio.run(eng.safety_check(FakeHttp(_report(lp_locked=0.0)), "MINT", "market"))
    assert ok
    ok, reason = asyncio.run(eng.safety_check(FakeHttp(_report(lp_locked=50.0, top10_pct=2.0)), "MINT", "meme"))
    assert not ok and "LP locked" in reason


def test_safety_rug_score_blocks_memes_only():
    eng = MemeEngine(make_cfg())
    ok, _ = asyncio.run(eng.safety_check(FakeHttp(_report(score=80)), "MINT", "market"))
    assert ok  # market candidates rely on liquidity/age gates + parsed authorities
    ok, reason = asyncio.run(eng.safety_check(FakeHttp(_report(score=80, top10_pct=2.0)), "MINT", "meme"))
    assert not ok and "rugcheck score" in reason


def test_safety_market_ignores_authority_risks_but_not_others():
    eng = MemeEngine(make_cfg())
    # RugCheck's heuristic flags can contradict parsed (renounced) authorities
    ok, _ = asyncio.run(eng.safety_check(FakeHttp(_report(
        risks=[{"name": "Mint Authority still enabled", "level": "danger"}])), "MINT", "market"))
    assert ok
    ok, reason = asyncio.run(eng.safety_check(FakeHttp(_report(
        risks=[{"name": "Creator holds large supply", "level": "danger"}])), "MINT", "market"))
    assert not ok and "danger risk" in reason
    # memes still blocked by any danger risk
    ok, reason = asyncio.run(eng.safety_check(FakeHttp(_report(
        risks=[{"name": "Mint Authority still enabled", "level": "danger"}], top10_pct=2.0)), "MINT", "meme"))
    assert not ok
