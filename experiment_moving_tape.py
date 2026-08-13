"""Find pools that ACTUALLY moved in the last 30 days, then sweep the
momentum floor on each — proves whether the floor keeps real momentum trades."""
import asyncio
import sys
import time
from datetime import datetime

sys.path.insert(0, "src")

import httpx

from backtest import BacktestEngine, WARMUP_BARS
from config import Config, GECKOTERMINAL_BASE


async def candidate_pools(http):
    out = []
    for page in range(1, 9):
        try:
            r = await http.get(f"{GECKOTERMINAL_BASE}/networks/solana/pools",
                               params={"page": page, "sort": "h24_volume_usd_desc"},
                               headers={"Accept": "application/json"})
            r.raise_for_status()
        except Exception as e:
            print("page err:", e)
            break
        for p in r.json().get("data", []):
            a = p["attributes"]
            symbol = a["name"].split("/")[0].strip().upper()
            if symbol in {"SOL", "WSOL", "USDC", "USDT", "BTC", "CBBTC", "WBTC", "ETH", "JITOSOL", "MSOL", "BSOL", "JUPSOL", "BNSOL", "USD1", "PYUSD", "EURC"}:
                continue  # majors/stables are dead tape by nature
            created = a.get("pool_created_at", "")
            age_d = (time.time() - datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()) / 86400 if created else -1
            liq = float(a.get("reserve_in_usd") or 0)
            if age_d < 15 or liq < 50_000:
                continue  # need history + tradable liquidity
            out.append({"name": a["name"], "address": a["address"], "liq": liq,
                        "created_ms": int(datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() * 1000)})
            if len(out) >= 8:
                return out
        await asyncio.sleep(2.5)
    return out


def max_h6(bars):
    best = 0.0
    for i in range(WARMUP_BARS, len(bars)):
        if bars[i - 6][4] > 0:
            best = max(best, (bars[i][4] / bars[i - 6][4] - 1) * 100)
    return best


async def main():
    cfg = Config.from_env()
    async with httpx.AsyncClient(timeout=30.0) as http:
        pools = await candidate_pools(http)
        print(f"candidates: {[p['name'] for p in pools]}\n")
        for p in pools:
            await asyncio.sleep(12)
            bars = await BacktestEngine.fetch_ohlcv(http, p["address"], "hour", 1, 1000)
            bars = bars[-720:]
            if len(bars) <= 60:
                print(f"skip {p['name']}: {len(bars)} bars")
                continue
            mh6 = max_h6(bars)
            print(f"=== {p['name']} liq=${p['liq']:,.0f} bars={len(bars)} maxH6={mh6:+.0f}% ===")
            if mh6 < 10:
                print("  (dead tape — floor comparison pointless)\n")
                continue
            print(f"{'floor':>6} | {'trades':>6} | {'W/L':>5} | {'win%':>5} | "
                  f"{'PnL $':>7} | {'expect':>8} | {'maxDD':>6}")
            for floor in (None, 2.0, 5.0, 10.0):
                eng = BacktestEngine(cfg, mode="meme", cost_pct=0.015, min_h6_pct=floor)
                s = eng.run(bars, p["liq"], p["created_ms"], source="market")
                wr = f"{s['win_rate']:.0%}" if s["win_rate"] is not None else "  n/a"
                label = " off " if floor is None else f"{floor:>+5.1f}"
                print(f"{label} | {s['n_trades']:>6} | {s['wins']:>2}/{s['losses']:<2} | "
                      f"{wr:>5} | {s['total_pnl']:>+7.2f} | {s['expectancy']:>+8.3f} | "
                      f"{s['max_drawdown']:>6.1%}")
            print()


if __name__ == "__main__":
    asyncio.run(main())
