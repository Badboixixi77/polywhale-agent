"""Perception layer: market data only. No decisions happen here.

Sources:
- Jupiter Price API  -> live USD prices (SOL + any SPL mint)
- CoinGecko          -> daily SOL history bootstrap for the trend filter
- DexScreener        -> meme candidate discovery + pair stats
- GeckoTerminal      -> whole-market discovery (top pools by volume)
"""
import logging
import time
import asyncio
from dataclasses import dataclass
from datetime import datetime

import httpx

from config import COINGECKO_BASE, DEXSCREENER_BASE, GECKOTERMINAL_BASE, JUPITER_BASE, SOL_MINT, USDC_MINT

logger = logging.getLogger("PolyWhale.perception")

USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
SKIP_MINTS = {SOL_MINT, USDC_MINT, USDT_MINT}  # never "hunt" SOL or stables as candidates
# Stablecoins (any chain flavor) by symbol — momentum strategy can't work on pegs
SKIP_SYMBOLS = {
    "USDC", "USDT", "PYUSD", "USDE", "DAI", "USDS", "EURC", "USDG", "FDUSD",
    "TUSD", "USDX", "USD1", "GHO", "CRVUSD", "FRAX", "LUSD", "MIM", "DOLA",
}


@dataclass
class CandidateToken:
    mint: str
    symbol: str
    price_usd: float
    liquidity_usd: float
    volume_h24: float
    pair_created_ms: int
    price_change_h1: float = 0.0
    price_change_h6: float = 0.0
    price_change_h24: float = 0.0
    pair_address: str = ""
    source: str = "meme"  # "meme" (DexScreener) or "market" (GeckoTerminal)

    def age_hours(self, now: float = None) -> float:
        if not self.pair_created_ms:
            return 0.0
        now = now if now is not None else time.time()
        return max(0.0, (now * 1000 - self.pair_created_ms) / 3_600_000)


class PerceptionLayer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.http = httpx.AsyncClient(
            timeout=20.0, headers={"User-Agent": "polywhale/2.0 (learning bot)"}
        )

    # ---- prices ----
    async def token_price(self, mint: str) -> float:
        """USD price for any SPL mint via Jupiter Price API. 0.0 on failure."""
        try:
            resp = await self.http.get(f"{JUPITER_BASE}/price/v3", params={"ids": mint})
            resp.raise_for_status()
            return float(resp.json()[mint]["usdPrice"])
        except Exception as e:
            logger.warning(f"price fetch failed for {mint[:8]}: {e}")
            return 0.0

    async def sol_price(self) -> float:
        return await self.token_price(SOL_MINT)

    async def token_price_dexscreener(self, mint: str) -> float:
        """Independent price source (best-liquidity pair) for cross-checking."""
        try:
            resp = await self.http.get(f"{DEXSCREENER_BASE}/latest/dex/tokens/{mint}")
            resp.raise_for_status()
            pairs = [p for p in resp.json().get("pairs", []) or [] if p.get("chainId") == "solana"]
            if not pairs:
                return 0.0
            best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
            return float(best.get("priceUsd") or 0)
        except Exception as e:
            logger.warning(f"dexscreener price failed for {mint[:8]}: {e}")
            return 0.0

    async def bootstrap_sol_history(self, days: int = 25) -> list:
        """Daily SOL/USD closes from CoinGecko, used once to seed the SMA."""
        try:
            resp = await self.http.get(
                f"{COINGECKO_BASE}/coins/solana/market_chart",
                params={"vs_currency": "usd", "days": days, "interval": "daily"},
            )
            resp.raise_for_status()
            prices = resp.json().get("prices", [])
            return [(ms / 1000.0, float(p)) for ms, p in prices if p and p > 0]
        except Exception as e:
            logger.warning(f"SOL history bootstrap failed: {e}")
            return []

    # ---- meme discovery ----
    async def discover_candidates(self, limit: int = 15) -> list:
        """Pull boosted/trending Solana tokens from DexScreener and enrich with pair stats."""
        mints = await self._trending_mints()
        candidates = []
        for mint in mints[:limit]:
            candidate = await self._pair_stats(mint)
            if candidate:
                candidates.append(candidate)
        return candidates

    async def _trending_mints(self) -> list:
        seen, mints = set(), []
        for endpoint in ("token-boosts/latest/v1", "token-profiles/latest/v1"):
            try:
                resp = await self.http.get(f"{DEXSCREENER_BASE}/{endpoint}")
                resp.raise_for_status()
                for item in resp.json():
                    if item.get("chainId") != "solana":
                        continue
                    mint = item.get("tokenAddress", "")
                    if mint and mint not in seen:
                        seen.add(mint)
                        mints.append(mint)
            except Exception as e:
                logger.warning(f"discovery endpoint {endpoint} failed: {e}")
        return mints

    async def _pair_stats(self, mint: str):
        try:
            resp = await self.http.get(f"{DEXSCREENER_BASE}/latest/dex/tokens/{mint}")
            resp.raise_for_status()
            pairs = [p for p in resp.json().get("pairs", []) or [] if p.get("chainId") == "solana"]
            if not pairs:
                return None
            best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
            change = best.get("priceChange") or {}
            return CandidateToken(
                mint=mint,
                symbol=(best.get("baseToken") or {}).get("symbol", "???"),
                price_usd=float(best.get("priceUsd") or 0),
                liquidity_usd=float((best.get("liquidity") or {}).get("usd") or 0),
                volume_h24=float((best.get("volume") or {}).get("h24") or 0),
                pair_created_ms=int(best.get("pairCreatedAt") or 0),
                price_change_h1=float(change.get("h1") or 0),
                price_change_h6=float(change.get("h6") or 0),
                price_change_h24=float(change.get("h24") or 0),
                pair_address=best.get("pairAddress", ""),
            )
        except Exception as e:
            logger.warning(f"pair stats failed for {mint[:8]}: {e}")
            return None

    # ---- whole-market discovery ----
    async def discover_market_candidates(self, limit: int = 30) -> list:
        """Hunt the entire Solana market: top pools by 24h volume (GeckoTerminal).
        Zero/low-liquidity pools (synthetic markets, fresh stock-token spam)
        are skipped so the candidate budget is spent on established, tradable
        pools — the entry gate demands $25k liquidity anyway."""
        candidates, seen = [], set()
        for page in range(1, 9):
            pools = None
            for attempt in (1, 2):  # one retry after backoff if rate-limited
                try:
                    resp = await self.http.get(
                        f"{GECKOTERMINAL_BASE}/networks/solana/pools",
                        params={"page": page, "sort": "h24_volume_usd_desc"},
                        headers={"Accept": "application/json"},
                    )
                    resp.raise_for_status()
                    pools = resp.json().get("data", [])
                    break
                except Exception as e:
                    logger.warning(f"market discovery page {page} attempt {attempt} failed: {e}")
                    if attempt == 1:
                        await asyncio.sleep(5.0)  # back off, then retry once
            if pools is None:
                break  # rate-limited twice: deeper pages have worse volume anyway
            for pool in pools:
                c = self._pool_to_candidate(pool)
                if c and c.mint not in seen and c.liquidity_usd >= 10000.0:
                    seen.add(c.mint)
                    candidates.append(c)
            if len(candidates) >= limit:
                break
            await asyncio.sleep(2.0)  # pace GeckoTerminal's rate limit
        return candidates[:limit]

    async def discover_trending_candidates(self, limit: int = 15) -> list:
        """Hunt actively-traded tape: pools ranked by 24h transaction count.
        This is where real meme momentum lives (the volume leaderboard is
        often flooded with synthetic stock pools). Lower liquidity floor —
        the satellite sleeve's judgment engine decides what's worth risking."""
        candidates, seen = [], set()
        for page in range(1, 4):
            pools = None
            for attempt in (1, 2):
                try:
                    resp = await self.http.get(
                        f"{GECKOTERMINAL_BASE}/networks/solana/pools",
                        params={"page": page, "sort": "h24_tx_count_desc"},
                        headers={"Accept": "application/json"},
                    )
                    resp.raise_for_status()
                    pools = resp.json().get("data", [])
                    break
                except Exception as e:
                    logger.warning(f"trending discovery page {page} attempt {attempt} failed: {e}")
                    if attempt == 1:
                        await asyncio.sleep(5.0)
            if pools is None:
                break
            for pool in pools:
                c = self._pool_to_candidate(pool)
                if c is not None:
                    c.source = "trending"
                if c and c.mint not in seen and c.liquidity_usd >= 5000.0:
                    seen.add(c.mint)
                    candidates.append(c)
            if len(candidates) >= limit:
                break
            await asyncio.sleep(2.0)  # pace GeckoTerminal's rate limit
        return candidates[:limit]

    def _pool_to_candidate(self, pool: dict):
        """Parse one GeckoTerminal pool into a CandidateToken. None if unusable."""
        try:
            base_id = (pool.get("relationships", {}).get("base_token", {})
                       .get("data", {}).get("id", ""))
            mint = base_id.split("_", 1)[1] if "_" in base_id else ""
            if not mint or mint in SKIP_MINTS:
                return None
            attrs = pool.get("attributes", {})
            change = attrs.get("price_change_percentage", {}) or {}
            volume = attrs.get("volume_usd", {}) or {}
            created_ms = 0
            if attrs.get("pool_created_at"):
                created_ms = int(datetime.fromisoformat(
                    attrs["pool_created_at"].replace("Z", "+00:00")).timestamp() * 1000)
            name = attrs.get("name", "???")
            symbol = name.split("/")[0].strip() if "/" in name else name
            if symbol.upper() in SKIP_SYMBOLS:
                return None
            return CandidateToken(
                mint=mint,
                symbol=symbol,
                price_usd=float(attrs.get("base_token_price_usd") or 0),
                liquidity_usd=float(attrs.get("reserve_in_usd") or 0),
                volume_h24=float(volume.get("h24") or 0),
                pair_created_ms=created_ms,  # pool age stands in for token age
                price_change_h1=float(change.get("h1") or 0),
                price_change_h6=float(change.get("h6") or 0),
                price_change_h24=float(change.get("h24") or 0),
                pair_address=pool.get("id", ""),
                source="market",
            )
        except Exception as e:
            logger.warning(f"pool parse failed: {e}")
            return None

    async def close(self):
        await self.http.aclose()
