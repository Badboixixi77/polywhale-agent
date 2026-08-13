"""One-off experiment: does a minimum-h6-momentum floor fix the bleed on
low-volatility market entries? Sweeps the knob offline on one bar fetch."""
import asyncio, sys

sys.path.insert(0, "src")

import httpx

from backtest import BacktestEngine
from config import Config, GECKOTERMINAL_BASE


async def find_pool(http, query: str):
    try:
        r = await http.get(f"{GECKOTERMINAL_BASE}/search/pools",
                           params={"query": query, "network": "solana"},
                           headers={"Accept": "application/json"})
        r.raise_for_status()
        best = None
        for p in r.json().get("data", []):
            attrs = p.get("attributes", {})
            name = attrs.get("name", "")
            if query.upper() not in name.split("/")[0].upper():
                continue
            liq = float(attrs.get("reserve_in_usd") or 0)
            if liq < 10000:
                continue
            if best is None or liq > best["liq"]:
                from datetime import datetime
                created = attrs.get("pool_created_at", "")
                cms = int(datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() * 1000) if created else 0
                best = {"name": name, "address": attrs.get("address", ""),
                        "liq": liq, "created_ms": cms}
        return best
    except Exception as e:
        print("search err:", e)
        return None


async def main():
    query = sys.argv[1] if len(sys.argv) > 1 else "cbBTC"
    cfg = Config.from_env()
    async with httpx.AsyncClient(timeout=30.0) as http:
        pool = await find_pool(http, query)
        if pool is None:
            print(f"no tradable pool found for {query}")
            return
        print(f"pool: {pool['name']} liq=${pool['liq']:,.0f}")
        await asyncio.sleep(5)
        bars = await BacktestEngine.fetch_ohlcv(http, pool["address"], "hour", 1, 1000)
        bars = bars[-720:]
        print(f"bars: {len(bars)} (~{(bars[-1][0]-bars[0][0])/86400:.0f} days)")
        print(f"{'floor':>6} | {'trades':>6} | {'W/L':>5} | {'win%':>5} | "
              f"{'PnL $':>7} | {'expect':>8} | {'maxDD':>6}")
        print("-" * 60)
        for floor in (None, 1.0, 2.0, 5.0, 10.0, 20.0):
            eng = BacktestEngine(cfg, mode="meme", cost_pct=0.015, min_h6_pct=floor)
            s = eng.run(bars, pool["liq"], pool["created_ms"], source="market")
            wr = f"{s['win_rate']:.0%}" if s["win_rate"] is not None else "  n/a"
            label = " off " if floor is None else f"{floor:>+5.1f}"
            print(f"{label} | {s['n_trades']:>6} | {s['wins']:>2}/{s['losses']:<2} | "
                  f"{wr:>5} | {s['total_pnl']:>+7.2f} | {s['expectancy']:>+8.3f} | "
                  f"{s['max_drawdown']:>6.1%}")


if __name__ == "__main__":
    asyncio.run(main())
