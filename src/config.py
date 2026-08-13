"""PolyWhale v2 configuration.

All risk limits and budgets live here, loaded from .env. Scaling the bot
to a bigger bankroll later means editing numbers, not code.
"""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

JUPITER_BASE = "https://lite-api.jup.ag"
DEXSCREENER_BASE = "https://api.dexscreener.com"
RUGCHECK_BASE = "https://api.rugcheck.xyz"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
GECKOTERMINAL_BASE = "https://api.geckoterminal.com/api/v2"


@dataclass(frozen=True)
class Config:
    # connectivity
    wallet_private_key: str
    rpc_url: str
    dry_run: bool
    # bankroll (USD)
    bankroll: float
    majors_budget: float
    meme_budget: float
    # majors sleeve: DCA + trend filter
    dca_amount_usd: float
    dca_interval_hours: float
    sma_window: int
    # meme sleeve
    meme_trade_cap_usd: float
    max_meme_positions: int
    min_token_liquidity_usd: float
    min_token_age_hours: float
    take_profit_pct: float      # sell half at this gain
    stop_loss_pct: float        # full exit at this loss (negative)
    trailing_stop_pct: float    # trail from peak once take-profit has triggered
    max_hold_hours: float
    # portfolio guardrails
    daily_loss_limit_pct: float
    drawdown_kill_pct: float
    slippage_bps: int
    max_priority_fee_lamports: int
    # alerts (optional — log-only when empty)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # discovery (meme | market | both) + whole-market gate tuning
    discovery: str = "market"
    min_market_liquidity_usd: float = 25000.0
    min_market_age_hours: float = 48.0
    meme_scan_every_cycles: int = 3

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            wallet_private_key=os.getenv("WALLET_PRIVATE_KEY", ""),
            rpc_url=os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com"),
            dry_run=os.getenv("DRY_RUN", "true").lower() == "true",
            bankroll=float(os.getenv("BANKROLL", "20")),
            majors_budget=float(os.getenv("MAJORS_BUDGET", "10")),
            meme_budget=float(os.getenv("MEME_BUDGET", "10")),
            dca_amount_usd=float(os.getenv("DCA_AMOUNT_USD", "2")),
            dca_interval_hours=float(os.getenv("DCA_INTERVAL_HOURS", str(24 * 7))),
            sma_window=int(os.getenv("SMA_WINDOW", "20")),
            meme_trade_cap_usd=float(os.getenv("MEME_TRADE_CAP_USD", "2")),
            max_meme_positions=int(os.getenv("MAX_MEME_POSITIONS", "5")),
            min_token_liquidity_usd=float(os.getenv("MIN_TOKEN_LIQUIDITY_USD", "5000")),
            min_token_age_hours=float(os.getenv("MIN_TOKEN_AGE_HOURS", "24")),
            take_profit_pct=float(os.getenv("TAKE_PROFIT_PCT", "0.50")),
            stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "-0.40")),
            trailing_stop_pct=float(os.getenv("TRAILING_STOP_PCT", "0.20")),
            max_hold_hours=float(os.getenv("MAX_HOLD_HOURS", "72")),
            daily_loss_limit_pct=float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.05")),
            drawdown_kill_pct=float(os.getenv("DRAWDOWN_KILL_PCT", "0.20")),
            slippage_bps=int(os.getenv("SLIPPAGE_BPS", "100")),
            max_priority_fee_lamports=int(os.getenv("MAX_PRIORITY_FEE_LAMPORTS", "100000")),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            discovery=os.getenv("DISCOVERY", "market").lower(),
            min_market_liquidity_usd=float(os.getenv("MIN_MARKET_LIQUIDITY_USD", "25000")),
            min_market_age_hours=float(os.getenv("MIN_MARKET_AGE_HOURS", "48")),
            meme_scan_every_cycles=int(os.getenv("MEME_SCAN_EVERY_CYCLES", "3")),
        )
