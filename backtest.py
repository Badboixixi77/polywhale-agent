"""Backtest runner: replay real Solana pool history through the live strategy.

Usage:
  venv/bin/python backtest.py --pool solana_<POOL_ID>          # one pool
  venv/bin/python backtest.py --leaderboard 5                  # top volume pools
  options: --mode meme|satellite --timeframe hour --aggregate 1
           --bars 720 --cost 0.015

Honesty note: liquidity/age come from the pool's CURRENT snapshot and the
universe is today's leaderboard, so treat results as "how the mechanics
behave on real price paths", not as an unbiased edge estimate.
"""
import argparse
import asyncio
import sys
import time

sys.path.insert(0, "src")

import httpx  # noqa: E402

from backtest import BacktestEngine  # noqa: E402
from config import Config, GECKOTERMINAL_BASE  # noqa: E402
from perception import SKIP_MINTS, SKIP_SYMBOLS  # noqa: E402


async def leaderboard_pools(http, n: int, min_age_days: int = 3) -> list:
    """Top-volume tradable pools with real history (mirrors discovery filters).
    Brand-new pools are skipped — they have no bars to replay."""
    out = []
    now_s = time.time()
    for page in (1, 2, 3, 4, 5, 6):
        try:
            resp = await http.get(
                f"{GECKOTERMINAL_BASE}/networks/solana/pools",
                params={"page": page, "sort": "h24_volume_usd_desc"},
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
        except Exception as e:
            print(f"leaderboard page {page} failed: {e}")
            break
        for pool in resp.json().get("data", []):
            attrs = pool.get("attributes", {})
            name = attrs.get("name", "???")
            symbol = name.split("/")[0].strip() if "/" in name else name
            base_id = (pool.get("relationships", {}).get("base_token", {})
                       .get("data", {}).get("id", ""))
            mint = base_id.split("_", 1)[1] if "_" in base_id else ""
            liq = float(attrs.get("reserve_in_usd") or 0)
            if not mint or mint in SKIP_MINTS or symbol.upper() in SKIP_SYMBOLS or liq < 10000:
                continue
            created_ms = 0
            if attrs.get("pool_created_at"):
                from datetime import datetime
                created_ms = int(datetime.fromisoformat(
                    attrs["pool_created_at"].replace("Z", "+00:00")).timestamp() * 1000)
            age_days = (now_s - created_ms / 1000) / 86400 if created_ms else 0
            if age_days < min_age_days:
                continue  # too young to have replayable history
            out.append({"id": pool["id"], "address": attrs.get("address", ""),
                        "symbol": symbol, "liquidity": liq,
                        "created_ms": created_ms})
            if len(out) >= n:
                return out
        await asyncio.sleep(1.5)
    return out[:n]


def print_summary(label: str, stats: dict, timeframe_note: str):
    wr = f"{stats['win_rate']:.0%}" if stats["win_rate"] is not None else "n/a"
    pf = f"{stats['profit_factor']:.2f}" if stats["profit_factor"] is not None else "n/a"
    ret = f"{stats['total_return_pct']:+.0%}" if stats["total_return_pct"] is not None else "n/a"
    print(f"\n=== {label} ({timeframe_note}) ===")
    print(f"Trades: {stats['n_trades']} (W {stats['wins']} / L {stats['losses']}) | win rate {wr}")
    print(f"Avg win ${stats['avg_win']:+.2f} | avg loss ${stats['avg_loss']:+.2f} | "
          f"expectancy ${stats['expectancy']:+.3f}/trade")
    print(f"Profit factor {pf} | total PnL ${stats['total_pnl']:+.2f} ({ret} on the slice)")
    print(f"Max drawdown {stats['max_drawdown']:.1%} | avg hold {stats['avg_bars_held']:.1f} bars")
    for t in stats["trades"][-6:]:
        print(f"  {t.reason:22s} {t.pnl_pct:+7.1%}  held {t.bars_held} bars")


async def main():
    ap = argparse.ArgumentParser(description="PolyWhale strategy backtester")
    ap.add_argument("--pool", help="GeckoTerminal pool id, e.g. solana_XXXX")
    ap.add_argument("--leaderboard", type=int, default=0, help="backtest top-N volume pools")
    ap.add_argument("--mode", choices=["meme", "satellite"], default="meme")
    ap.add_argument("--timeframe", default="hour", choices=["minute", "hour", "day"])
    ap.add_argument("--aggregate", type=int, default=1)
    ap.add_argument("--bars", type=int, default=720)
    ap.add_argument("--cost", type=float, default=0.015, help="round-trip cost haircut")
    ap.add_argument("--min-h6", type=float, default=None,
                    help="experiment: require h6 momentum >= this %% before entry")
    args = ap.parse_args()

    cfg = Config.from_env()
    async with httpx.AsyncClient(timeout=30.0) as http:
        engine = BacktestEngine(cfg, mode=args.mode, cost_pct=args.cost,
                                min_h6_pct=args.min_h6)
        pools = []
        if args.pool:
            pools = [{"id": args.pool, "address": args.pool.split("_", 1)[-1],
                      "symbol": args.pool[-6:], "liquidity": 100000.0,
                      "created_ms": 0}]
        elif args.leaderboard:
            print(f"Fetching top-{args.leaderboard} tradable pools by 24h volume...")
            pools = await leaderboard_pools(http, args.leaderboard)
        else:
            ap.error("pass --pool <id> or --leaderboard N")

        for p in pools:
            bars = await BacktestEngine.fetch_ohlcv(
                http, p["address"] or p["id"], timeframe=args.timeframe,
                aggregate=args.aggregate, limit=min(args.bars, 1000))
            if len(bars) <= 30:
                print(f"skip {p['symbol']}: only {len(bars)} bars")
                await asyncio.sleep(10)
                continue
            bars = bars[-args.bars:]
            created_ms = p["created_ms"] or (bars[0][0] * 1000)
            stats = engine.run(bars, p["liquidity"], created_ms, source="market")
            span_days = (bars[-1][0] - bars[0][0]) / 86400
            print_summary(f"{p['symbol']} liq=${p['liquidity']:,.0f}",
                          stats, f"{len(bars)} {args.timeframe} bars ~{span_days:.0f}d")
            await asyncio.sleep(12.0)  # GeckoTerminal free tier pacing


if __name__ == "__main__":
    asyncio.run(main())
