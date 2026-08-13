"""Execution layer: Jupiter swaps.

Paper mode (DRY_RUN=true, the default) simulates fills from live Jupiter
quotes without signing anything. Live mode builds, signs and sends the
transaction. A failed swap never books a position.
"""
import base64
import logging
import struct

import httpx

from config import JUPITER_BASE, SOL_MINT, USDC_MINT

logger = logging.getLogger("PolyWhale.execution")

KNOWN_DECIMALS = {SOL_MINT: 9, USDC_MINT: 6}
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
RECONCILE_TOLERANCE = 0.01  # 1% slack for fee dust


def find_drift(expected: list, actual: dict, tolerance: float = RECONCILE_TOLERANCE) -> list:
    """Compare ledger expectations [(symbol, key, amount)] against on-chain
    balances. Shortfalls are hazards and get reported; excess on-chain is
    informational only (dust, tips) and ignored."""
    drift = []
    for symbol, key, amount in expected:
        have = actual.get(key, 0.0)
        if have < amount * (1 - tolerance):
            drift.append(f"{symbol}: ledger {amount:.6g}, on-chain {have:.6g}")
    return drift


class ExecutionLayer:
    def __init__(self, cfg, ledger, http: httpx.AsyncClient):
        self.cfg = cfg
        self.ledger = ledger
        self.http = http
        self._decimals_cache = dict(KNOWN_DECIMALS)

    @property
    def mode(self) -> str:
        return "paper" if self.cfg.dry_run else "live"

    # ---- decimals ----
    async def get_decimals(self, mint: str) -> int:
        if mint in self._decimals_cache:
            return self._decimals_cache[mint]
        resp = await self.http.post(
            self.cfg.rpc_url,
            json={
                "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                "params": [mint, {"encoding": "base64"}],
            },
        )
        resp.raise_for_status()
        data_b64 = resp.json()["result"]["value"]["data"][0]
        data = base64.b64decode(data_b64)
        # SPL mint layout: decimals is the byte at offset 44
        decimals = struct.unpack_from("B", data, 44)[0]
        self._decimals_cache[mint] = decimals
        return decimals

    # ---- price ----
    async def _sol_usd_price(self) -> float:
        """Spot SOL price from Jupiter's price endpoint. 0.0 on any failure."""
        try:
            resp = await self.http.get(f"{JUPITER_BASE}/price/v3", params={"ids": SOL_MINT})
            resp.raise_for_status()
            return float(resp.json()[SOL_MINT]["usdPrice"])
        except Exception as e:
            logger.error(f"SOL price fetch failed: {e}")
            return 0.0

    # ---- quote ----
    async def quote(self, input_mint: str, output_mint: str, amount_raw: int) -> dict:
        resp = await self.http.get(
            f"{JUPITER_BASE}/swap/v1/quote",
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount_raw),
                "slippageBps": self.cfg.slippage_bps,
            },
        )
        resp.raise_for_status()
        quote = resp.json()
        if "outAmount" not in quote:
            raise RuntimeError(f"bad quote: {quote}")
        return quote

    # ---- swaps ----
    async def buy(self, sleeve: str, input_mint: str, output_mint: str, symbol: str, usd: float) -> dict:
        """Swap `usd` worth of input_mint for output_mint. Returns fill dict or None."""
        try:
            in_dec = await self.get_decimals(input_mint)
            if input_mint == output_mint:
                # no swap needed (e.g. majors DCA accumulating SOL from SOL):
                # book the purchase directly at the current market price
                price = await self._sol_usd_price() if output_mint == SOL_MINT else 0.0
                if price <= 0:
                    return None
                out_amount = usd / price
                mode = "paper" if self.cfg.dry_run else "live"
                fill = {"mode": mode, "usd": usd, "amount": out_amount, "price": price}
                self.ledger.record_fill(sleeve, output_mint, "buy", usd, out_amount, price, mode)
                logger.info(f"[{mode}] BUY ${usd:.2f} {symbol}: {out_amount:.8g} @ ${price:.8g} (same-asset)")
                return fill
            # Dollars -> input-token units: stablecoins are ~1:1, but SOL must
            # be sized by spot price ($6 of SOL is 0.079 SOL, not 6 SOL).
            if input_mint == SOL_MINT:
                sol_price = await self._sol_usd_price()
                if sol_price <= 0:
                    return None
                in_amount = usd / sol_price
            else:
                in_amount = usd
            amount_raw = int(in_amount * (10 ** in_dec))
            quote = await self.quote(input_mint, output_mint, amount_raw)
            out_dec = await self.get_decimals(output_mint)
            out_amount = int(quote["outAmount"]) / (10 ** out_dec)
            if out_amount <= 0:
                return None
            fill_price = usd / out_amount

            if self.cfg.dry_run:
                fill = {"mode": "paper", "usd": usd, "amount": out_amount, "price": fill_price}
            else:
                tx_ok = await self._send_swap(quote)
                if not tx_ok:
                    return None
                fill = {"mode": "live", "usd": usd, "amount": out_amount, "price": fill_price}

            self.ledger.record_fill(sleeve, output_mint, "buy", usd, out_amount, fill_price, fill["mode"])
            logger.info(f"[{fill['mode']}] BUY ${usd:.2f} {symbol}: {out_amount:.8g} @ ${fill_price:.8g}")
            return fill
        except Exception as e:
            logger.error(f"buy failed ({sleeve}/{symbol}): {e}")
            return None

    async def sell(self, sleeve: str, mint: str, symbol: str, amount: float, ref_sol_price: float) -> dict:
        """Sell token amount back to SOL. ref_sol_price converts proceeds to USD.
        Returns fill dict (with usd + price) or None."""
        try:
            dec = await self.get_decimals(mint)
            amount_raw = int(amount * (10 ** dec))
            quote = await self.quote(mint, SOL_MINT, amount_raw)
            sol_out = int(quote["outAmount"]) / 1e9
            if sol_out <= 0 or ref_sol_price <= 0:
                return None
            usd_out = sol_out * ref_sol_price
            fill_price = usd_out / amount

            if self.cfg.dry_run:
                fill = {"mode": "paper", "usd": usd_out, "amount": amount, "price": fill_price}
            else:
                tx_ok = await self._send_swap(quote)
                if not tx_ok:
                    return None
                fill = {"mode": "live", "usd": usd_out, "amount": amount, "price": fill_price}

            self.ledger.record_fill(sleeve, mint, "sell", usd_out, amount, fill_price, fill["mode"], f"sol_out={sol_out:.6f}")
            logger.info(f"[{fill['mode']}] SELL {amount:.8g} {symbol} -> {sol_out:.6f} SOL (~${usd_out:.3f})")
            return fill
        except Exception as e:
            logger.error(f"sell failed ({sleeve}/{symbol}): {e}")
            return None

    # ---- live signing/sending ----
    async def _send_swap(self, quote: dict) -> bool:
        """Build, sign and send the swap transaction. Returns True only when confirmed."""
        try:
            # Lazy imports so paper mode runs without on-chain deps installed.
            from solders.keypair import Keypair
            from solders.message import to_bytes_versioned
            from solders.transaction import VersionedTransaction
            from solana.rpc.async_api import AsyncClient
            from solana.rpc.commitment import Confirmed
            try:
                from solana.rpc.types import TxOpts
            except ImportError:  # solana.py >= 0.36 moved TxOpts to models
                from solana.rpc.models import TxOpts

            if not self.cfg.wallet_private_key:
                logger.error("live mode requires WALLET_PRIVATE_KEY in .env")
                return False

            keypair = Keypair.from_base58_string(self.cfg.wallet_private_key)
            resp = await self.http.post(
                f"{JUPITER_BASE}/swap/v1/swap",
                json={
                    "quoteResponse": quote,
                    "userPublicKey": str(keypair.pubkey()),
                    "wrapAndUnwrapSol": True,
                    "prioritizationFeeLamports": {
                        "priorityLevelWithMaxLamports": {
                            "maxLamports": self.cfg.max_priority_fee_lamports,
                            "priorityLevel": "high",
                        }
                    },
                },
            )
            resp.raise_for_status()
            tx_bytes = base64.b64decode(resp.json()["swapTransaction"])
            tx = VersionedTransaction.from_bytes(tx_bytes)

            async with AsyncClient(self.cfg.rpc_url) as client:
                # Jupiter's embedded blockhash often expires before preflight
                # (BlockhashNotFound); rebuild the message with a fresh one.
                from solders.message import MessageV0

                bh_resp = await client.get_latest_blockhash()
                fresh_hash = bh_resp.value.blockhash
                msg = MessageV0(
                    tx.message.header,
                    tx.message.account_keys,
                    fresh_hash,
                    tx.message.instructions,
                    tx.message.address_table_lookups,
                )
                signature = keypair.sign_message(to_bytes_versioned(msg))
                signed_tx = VersionedTransaction.populate(msg, [signature])
                send_resp = await client.send_raw_transaction(
                    bytes(signed_tx), opts=TxOpts(skip_preflight=False, max_retries=3)
                )
                sig = send_resp.value
                if sig is None:
                    logger.error(f"swap send failed: {send_resp}")
                    return False
                await client.confirm_transaction(sig, commitment=Confirmed)
            logger.info(f"swap confirmed: {sig}")
            return True
        except Exception as e:
            logger.error(f"live swap failed: {e}")
            return False

    # ---- on-chain reconciliation ----
    async def wallet_balances(self) -> dict:
        """Actual on-chain balances: {'SOL': x, <mint>: amount}. Live mode only."""
        from solders.keypair import Keypair
        from solders.pubkey import Pubkey
        from solana.rpc.async_api import AsyncClient
        from solana.rpc.types import TokenAccountOpts

        kp = Keypair.from_base58_string(self.cfg.wallet_private_key)
        balances = {}
        async with AsyncClient(self.cfg.rpc_url) as client:
            sol = await client.get_balance(kp.pubkey())
            balances["SOL"] = sol.value / 1e9
            opts = TokenAccountOpts(program_id=Pubkey.from_string(TOKEN_PROGRAM_ID))
            resp = await client.get_token_accounts_by_owner_json_parsed(kp.pubkey(), opts)
            for item in resp.value:
                info = item.account.data.parsed["info"]
                balances[info["mint"]] = float(info["tokenAmount"]["uiAmountString"])
        return balances

    async def reconcile(self) -> tuple:
        """Verify on-chain balances cover ledger expectations. Returns (ok, notes)."""
        if self.cfg.dry_run:
            logger.info("reconcile: paper mode, on-chain check skipped")
            return True, []
        actual = await self.wallet_balances()
        expected = [
            (p["symbol"], "SOL" if p["mint"] == SOL_MINT else p["mint"], p["amount"])
            for p in self.ledger.open_positions()
        ]
        notes = find_drift(expected, actual)
        if notes:
            logger.critical(f"RECONCILE DRIFT: {notes}")
            return False, notes
        logger.info("reconcile: on-chain balances match ledger")
        return True, []
