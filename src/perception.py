"""Perception layer: market data only. No decisions happen here.

Sources:
- Jupiter Price API  -> live USD prices (SOL + any SPL mint)
- CoinGecko          -> daily SOL history bootstrap for the trend filter
- DexScreener        -> meme candidate discovery + pair stats
"""
import logging
import time
from dataclasses import dataclass

import httpx

from config import COINGECKO_BASE, DEXSCREENER_BASE, JUPITER_BASE, SOL_MINT

logger = logging.getLogger("PolyWhale.perception")


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

    async def close(self):
        await self.http.aclose()
