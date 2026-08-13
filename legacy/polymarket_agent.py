"""
PolyWhale Autonomous Agent v1.0
Date: August 12, 2026
Single-file deployment version.
"""
import asyncio
import json
import time
import hashlib
import logging
import numpy as np
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional
from enum import Enum
import httpx
import feedparser
from collections import defaultdict

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
except ImportError:
    ClobClient = None

import openai
from dotenv import load_dotenv
import os

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler("polywhale.log"), logging.StreamHandler()]
)
logger = logging.getLogger("PolyWhale")


class SignalStrength(Enum):
    NOISE = 0
    WEAK = 1
    MODERATE = 2
    STRONG = 3
    EXTREME = 4


@dataclass
class Belief:
    market_id: str
    token_id: str
    question: str
    outcome: str
    believed_probability: float
    confidence: float
    market_price: float
    edge: float
    reasoning: str
    evidence: list = field(default_factory=list)
    last_updated: float = field(default_factory=time.time)
    decay_rate: float = 0.02

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.last_updated) > 3600

    def decay(self):
        hours_elapsed = (time.time() - self.last_updated) / 3600
        decay_factor = np.exp(-self.decay_rate * hours_elapsed)
        self.believed_probability = (
            self.believed_probability * decay_factor +
            self.market_price * (1 - decay_factor)
        )
        self.edge = self.believed_probability - self.market_price


@dataclass
class NewsEvent:
    headline: str
    summary: str
    source: str
    timestamp: float
    url: str
    content_hash: str
    relevance_score: float = 0.0
    affected_markets: list = field(default_factory=list)
    sentiment: float = 0.0
    processed: bool = False


@dataclass
class Position:
    market_id: str
    token_id: str
    outcome: str
    size: float
    entry_price: float
    current_price: float
    kelly_fraction: float
    opened_at: float = field(default_factory=time.time)

    @property
    def pnl(self) -> float:
        if self.outcome == "YES":
            return self.size * (self.current_price - self.entry_price) / self.entry_price
        return self.size * (self.entry_price - self.current_price) / self.entry_price

    @property
    def hold_duration_hours(self) -> float:
        return (time.time() - self.opened_at) / 3600


@dataclass
class ArbitrageOpportunity:
    market_ids: list
    token_ids: list
    outcomes: list
    prices: list
    total_cost: float
    guaranteed_profit: float
    strategy: str


class PerceptionLayer:
    RSS_FEEDS = [
        "https://feeds.reuters.com/reuters/topNews",
        "https://feeds.reuters.com/reuters/politicsNews",
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
        "https://feeds.bloomberg.com/markets/news.rss",
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
        "https://thehill.com/feed/",
        "https://fivethirtyeight.com/all/feed",
    ]
    POLYMARKET_API = "https://gamma-api.polymarket.com"
    CLOB_API = "https://clob.polymarket.com"

    def __init__(self):
        self.seen_hashes = set()
        self.news_queue = asyncio.Queue(maxsize=1000)
        self.market_cache = {}
        self.price_cache = {}
        self.http_client = httpx.AsyncClient(timeout=30.0)

    async def start(self):
        logger.info("👁️  Perception layer starting...")
        await asyncio.gather(
            self._poll_news_loop(),
            self._poll_markets_loop(),
            self._poll_prices_loop(),
        )

    async def _poll_news_loop(self):
        while True:
            try:
                for feed_url in self.RSS_FEEDS:
                    try:
                        response = await self.http_client.get(feed_url)
                        feed = feedparser.parse(response.text)
                        for entry in feed.entries[:15]:
                            content_hash = hashlib.md5(entry.get("title", "").encode()).hexdigest()
                            if content_hash in self.seen_hashes:
                                continue
                            self.seen_hashes.add(content_hash)
                            news = NewsEvent(
                                headline=entry.get("title", ""),
                                summary=entry.get("summary", "")[:500],
                                source=feed_url,
                                timestamp=time.time(),
                                url=entry.get("link", ""),
                                content_hash=content_hash,
                            )
                            await self.news_queue.put(news)
                            logger.info(f"📰 New: {news.headline[:80]}")
                    except Exception as e:
                        logger.warning(f"Feed error {feed_url}: {e}")
            except Exception as e:
                logger.error(f"News loop error: {e}")
            await asyncio.sleep(45)

    async def _poll_markets_loop(self):
        while True:
            try:
                response = await self.http_client.get(
                    f"{self.POLYMARKET_API}/markets",
                    params={"active": True, "closed": False, "limit": 200, "order": "volume24hr", "ascending": False}
                )
                if response.status_code == 200:
                    markets = response.json()
                    for market in markets:
                        mid = market.get("id") or market.get("condition_id")
                        if mid:
                            self.market_cache[mid] = market
                    logger.info(f"📊 Tracking {len(self.market_cache)} active markets")
            except Exception as e:
                logger.error(f"Market fetch error: {e}")
            await asyncio.sleep(120)

    async def _poll_prices_loop(self):
        while True:
            try:
                for market_id, market in list(self.market_cache.items()):
                    try:
                        tokens = market.get("tokens", [])
                        for token in tokens:
                            token_id = token.get("token_id")
                            if token_id:
                                response = await self.http_client.get(
                                    f"{self.CLOB_API}/price",
                                    params={"token_id": token_id, "side": "buy"}
                                )
                                if response.status_code == 200:
                                    price_data = response.json()
                                    self.price_cache[token_id] = float(price_data.get("price", 0))
                    except Exception:
                        pass
                    await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"Price loop error: {e}")
            await asyncio.sleep(30)

    def get_active_markets(self) -> dict:
        return self.market_cache

    def get_price(self, token_id: str) -> float:
        return self.price_cache.get(token_id, 0.0)


class CognitionLayer:
    def __init__(self, perception: PerceptionLayer):
        self.perception = perception
        self.beliefs: dict[str, Belief] = {}
        self.positions: dict[str, Position] = {}
        self.trade_history: list = []
        self.bankroll: float = float(os.getenv("BANKROLL", "50000"))
        self.max_portfolio_risk: float = 0.25
        self.llm_client = openai.AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.model = os.getenv("LLM_MODEL", "gpt-4o")

    async def _reason(self, system_prompt: str, user_prompt: str) -> str:
        try:
            response = await self.llm_client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                temperature=0.2, max_tokens=2000,
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"LLM reasoning error: {e}")
            return "{}"

    async def map_news_to_markets(self, news: NewsEvent) -> list[dict]:
        markets_summary = self._get_markets_summary()
        system_prompt = """You are a prediction market analyst on August 12, 2026. Map news to markets and estimate probability shifts. Output valid JSON only."""
        user_prompt = f"""NEWS: {news.headline} | {news.summary}
MARKETS:
{markets_summary}
Output JSON array of affected markets with new_estimated_probability and confidence. Empty array if none."""
        response = await self._reason(system_prompt, user_prompt)
        try:
            clean = response.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[1].rsplit("```", 1)[0]
            return json.loads(clean)
        except json.JSONDecodeError:
            return []

    async def estimate_true_probability(self, market: dict, recent_news: list) -> dict:
        market_id = market.get("id") or market.get("condition_id")
        existing = self.beliefs.get(market_id)
        news_context = "\n".join([f"- {n.headline}" for n in recent_news[-20:]])
        system_prompt = """You are a superforecaster on Aug 12, 2026. Estimate true probabilities. Output valid JSON only."""
        tokens = market.get("tokens", [])
        token_prices = {t.get("outcome", "YES"): self.perception.get_price(t.get("token_id", "")) for t in tokens}
        user_prompt = f"""MARKET: {market.get('question','')} PRICES: {token_prices} NEWS: {news_context}
Output JSON: estimated_true_probability_yes, confidence, reasoning"""
        response = await self._reason(system_prompt, user_prompt)
        try:
            clean = response.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[1].rsplit("```", 1)[0]
            return json.loads(clean)
        except json.JSONDecodeError:
            return {}

    def detect_arbitrage(self) -> list:
        opportunities = []
        markets = self.perception.get_active_markets()
        for mid, market in markets.items():
            tokens = market.get("tokens", [])
            prices = [self.perception.get_price(t.get("token_id", "")) for t in tokens if self.perception.get_price(t.get("token_id", "")) > 0]
            if len(prices) >= 2:
                total = sum(prices)
                if 0.005 < (1.0 - total) and total < 0.98:
                    opportunities.append(ArbitrageOpportunity([mid], [t.get("token_id") for t in tokens], [t.get("outcome") for t in tokens], prices, total, 1.0-total, f"BUY_ALL:{total:.3f}"))
        return opportunities

    def kelly_size(self, believed_prob, market_price, confidence, max_fraction=0.05):
        if market_price <= 0.01 or market_price >= 0.99:
            return {"fraction": 0, "size_usd": 0, "reason": "Boundary"}
        believed_prob = np.clip(believed_prob, 0.02, 0.98)
        if believed_prob > market_price:
            direction, p, b = "YES", believed_prob, (1/market_price)-1
        else:
            direction, p, b = "NO", 1-believed_prob, (1/(1-market_price))-1
        q = 1 - p
        if b <= 0: return {"fraction": 0, "size_usd": 0}
        kelly_raw = (p*b - q)/b
        if kelly_raw <= 0: return {"fraction": 0, "size_usd": 0, "direction": direction, "reason": "No edge"}
        kelly_fraction = min(kelly_raw * 0.5 * confidence, max_fraction)
        exposure = sum(pos.size for pos in self.positions.values())
        available = self.bankroll * self.max_portfolio_risk - exposure
        size = min(kelly_fraction * self.bankroll, available, self.bankroll * max_fraction)
        if size < 10: return {"fraction": 0, "size_usd": 0}
        return {"direction": direction, "fraction": kelly_fraction, "size_usd": round(size,2), "edge": abs(believed_prob-market_price), "believed_prob": believed_prob, "market_price": market_price, "reason": "TRADE"}

    def update_belief(self, market_id, token_id, question, outcome, new_prob, confidence, market_price, reasoning, evidence=None):
        existing = self.beliefs.get(market_id)
        if existing and not existing.is_stale:
            new_prob = existing.believed_probability*(1-confidence*0.3) + new_prob*confidence*0.3
            confidence = min(0.95, (existing.confidence+confidence)/2 + 0.05)
        self.beliefs[market_id] = Belief(market_id, token_id, question, outcome, new_prob, confidence, market_price, new_prob-market_price, reasoning, evidence or [])
        logger.info(f"🧠 Belief: {question[:50]} P={new_prob:.3f} M={market_price:.3f}")

    def decay_all_beliefs(self):
        for b in self.beliefs.values(): b.decay()

    def get_actionable_beliefs(self, min_edge=0.03, min_conf=0.4):
        return [b for b in self.beliefs.values() if abs(b.edge)>=min_edge and b.confidence>=min_conf and not b.is_stale]

    def _get_markets_summary(self):
        lines = []
        for mid, m in list(self.perception.get_active_markets().items())[:50]:
            prices = " ".join(f"{t.get('outcome')}={self.perception.get_price(t.get('token_id','')):.2f}" for t in m.get("tokens",[]))
            lines.append(f"[{mid[:8]}] {m.get('question','')[:80]} | {prices}")
        return "\n".join(lines)


class ExecutionLayer:
    def __init__(self, cognition: CognitionLayer):
        self.cognition = cognition
        self.clob = None
        self.dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
        if ClobClient and not self.dry_run:
            try:
                self.clob = ClobClient("https://clob.polymarket.com", key=os.getenv("POLY_API_KEY"), chain_id=137, signature_type=2, funder=os.getenv("POLY_FUNDER_ADDRESS"))
                logger.info("🔗 LIVE MODE")
            except Exception as e:
                logger.error(e); self.dry_run = True
        else:
            logger.info("🏜️  DRY RUN")

    async def execute_trade(self, belief, sizing):
        if sizing["size_usd"] <= 0: return None
        logger.info(f"💰 {'[DRY] ' if self.dry_run else ''}EXEC {sizing['direction']} ${sizing['size_usd']:.2f} on {belief.question[:50]}")
        self.cognition.positions[belief.market_id] = Position(belief.market_id, belief.token_id, sizing["direction"], sizing["size_usd"], belief.market_price, belief.market_price, sizing["fraction"])
        self.cognition.trade_history.append({"market": belief.market_id, "size": sizing["size_usd"], "ts": time.time()})
        return True

    async def execute_arbitrage(self, arb):
        logger.info(f"⚡ {'[DRY] ' if self.dry_run else ''}ARB {arb.strategy[:80]}")
        return True

    async def manage_positions(self):
        for mid, pos in list(self.cognition.positions.items()):
            pos.current_price = self.cognition.perception.get_price(pos.token_id)
            if pos.hold_duration_hours > 72:
                del self.cognition.positions[mid]


class PolyWhaleAgent:
    def __init__(self):
        self.perception = PerceptionLayer()
        self.cognition = CognitionLayer(self.perception)
        self.execution = ExecutionLayer(self.cognition)
        self.cycle = 0
        self.news_buffer = []

    async def run(self):
        logger.info("🐋 AGENT ACTIVATED")
        asyncio.create_task(self.perception.start())
        await asyncio.sleep(10)
        while True:
            self.cycle += 1
            try:
                while not self.perception.news_queue.empty():
                    self.news_buffer.append(self.perception.news_queue.get_nowait())
                self.news_buffer = self.news_buffer[-100:]

                for news in [n for n in self.news_buffer if not n.processed][:5]:
                    mappings = await self.cognition.map_news_to_markets(news)
                    for m in mappings:
                        mid = m.get("market_id","")
                        market = self.perception.market_cache.get(mid,{})
                        tid = next((t.get("token_id") for t in market.get("tokens",[]) if t.get("outcome")=="YES"),"")
                        self.cognition.update_belief(mid, tid, market.get("question",""), "YES", m.get("new_estimated_probability",0.5), m.get("confidence",0.5), self.perception.get_price(tid), m.get("reasoning",""))
                    news.processed = True

                if self.cycle % 5 == 0:
                    for mid, market in sorted(self.perception.market_cache.items(), key=lambda x: float(x[1].get("volume24hr",0)), reverse=True)[:10]:
                        est = await self.cognition.estimate_true_probability(market, self.news_buffer)
                        if est:
                            tid = next((t.get("token_id") for t in market.get("tokens",[]) if t.get("outcome")=="YES"),"")
                            self.cognition.update_belief(mid, tid, market.get("question",""), "YES", est.get("estimated_true_probability_yes",0.5), est.get("confidence",0.5), self.perception.get_price(tid), est.get("reasoning",""))

                self.cognition.decay_all_beliefs()
                for arb in self.cognition.detect_arbitrage():
                    await self.execution.execute_arbitrage(arb)
                for belief in self.cognition.get_actionable_beliefs()[:3]:
                    if belief.market_id in self.cognition.positions: continue
                    sizing = self.cognition.kelly_size(belief.believed_probability, belief.market_price, belief.confidence)
                    if sizing["size_usd"]>0:
                        await self.execution.execute_trade(belief, sizing)
                await self.execution.manage_positions()

                logger.info(f"📋 Cycle {self.cycle}: Beliefs={len(self.cognition.beliefs)} Pos={len(self.cognition.positions)} PNL=${sum(p.pnl for p in self.cognition.positions.values()):.2f}")
            except Exception as e:
                logger.error(f"Loop error: {e}")
            await asyncio.sleep(60)


async def main():
    agent = PolyWhaleAgent()
    await agent.run()

if __name__ == "__main__":
    asyncio.run(main())