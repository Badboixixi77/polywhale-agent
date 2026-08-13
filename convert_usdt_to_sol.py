"""One-time funding normalizer: swap the wallet's entire USDT balance to SOL.
The bot trades from SOL; exchange withdrawals that arrive as USDT must be
converted first. Safe to re-run; refuses to swap without fee fuel.

Usage: venv/bin/python convert_usdt_to_sol.py --dry-run   # quote only
       venv/bin/python convert_usdt_to_sol.py             # execute swap
"""
import asyncio
import dataclasses
import sys

sys.path.insert(0, "src")

import httpx  # noqa: E402

from config import Config, SOL_MINT  # noqa: E402
from execution import ExecutionLayer  # noqa: E402
from state import Ledger  # noqa: E402

USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


async def main():
    dry = "--dry-run" in sys.argv
    cfg = dataclasses.replace(Config.from_env(), dry_run=dry)

    async with httpx.AsyncClient(timeout=30.0) as http:
        from solders.keypair import Keypair

        kp = Keypair.from_base58_string(cfg.wallet_private_key)
        addr = str(kp.pubkey())
        print(f"wallet: {addr}")

        r = await http.post(cfg.rpc_url, json={
            "jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [addr]})
        lamports = r.json()["result"]["value"]
        print(f"SOL balance: {lamports / 1e9:.6f} SOL")
        if lamports < 10_000 and not dry:
            print("ABORT: no SOL for transaction fees — send ~0.02 SOL to the wallet first")
            return

        r = await http.post(cfg.rpc_url, json={
            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
            "params": [addr, {"programId": TOKEN_PROGRAM}, {"encoding": "jsonParsed"}]})
        raw = 0
        for acct in r.json()["result"]["value"]:
            info = acct["account"]["data"]["parsed"]["info"]
            if info["mint"] == USDT_MINT:
                raw = int(info["tokenAmount"]["amount"])
        if raw <= 0:
            print("no USDT in wallet — nothing to convert")
            return
        usdt = raw / 1e6
        print(f"USDT balance: {usdt:.2f} — quoting USDT -> SOL...")

        led = Ledger("polywhale_ledger.db")
        ex = ExecutionLayer(cfg, led, http)
        quote = await ex.quote(USDT_MINT, SOL_MINT, raw)
        out_sol = int(quote["outAmount"]) / 1e9
        print(f"quote: {usdt:.2f} USDT -> {out_sol:.6f} SOL (dry_run={dry})")
        if dry:
            print("dry-run: nothing sent")
            led.close()
            return

        ok = await ex._send_swap(quote)
        led.close()
        if ok:
            print(f"swap confirmed: wallet now holds ~{out_sol:.4f} SOL (+ fee dust)")
        else:
            print("swap FAILED — check logs above; retry when ready")


asyncio.run(main())
