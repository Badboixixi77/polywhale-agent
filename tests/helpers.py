"""Shared test helpers."""
from config import Config


def make_cfg(**overrides) -> Config:
    base = dict(
        wallet_private_key="",
        rpc_url="http://localhost",
        dry_run=True,
        bankroll=20.0,
        majors_budget=10.0,
        meme_budget=10.0,
        dca_amount_usd=2.0,
        dca_interval_hours=168.0,
        sma_window=20,
        meme_trade_cap_usd=2.0,
        max_meme_positions=5,
        min_token_liquidity_usd=5000.0,
        min_token_age_hours=24.0,
        take_profit_pct=0.50,
        stop_loss_pct=-0.40,
        trailing_stop_pct=0.20,
        max_hold_hours=72.0,
        daily_loss_limit_pct=0.05,
        drawdown_kill_pct=0.20,
        slippage_bps=100,
        max_priority_fee_lamports=100000,
    )
    base.update(overrides)
    return Config(**base)
