# PolyWhale Agent

An autonomous crypto trading bot for Solana, built for a small learning budget. It splits a starter bankroll into two sleeves — a conservative **majors** sleeve that dollar-cost-averages into SOL, and a tightly capped **meme** sleeve — and runs both inside a hard **risk cage** that limits every trade, halts on daily losses, and carries a manual-reset kill switch.

> **Disclaimer:** This is experimental, educational software. Crypto trading can lose money fast, and memecoins especially so. Nothing here is financial advice, and no bot guarantees profit. Only ever trade money you can fully afford to lose.

---

## How it works

The bot runs a loop once per minute. Each cycle flows through four layers:

```
Perception  ->  Cognition  ->  Risk  ->  Execution
(sees market)   (decides)      (vetoes)   (trades)
```

1. **Perception** (`src/perception.py`) — reads market data: SOL prices from the Jupiter API, trending tokens and liquidity stats from DexScreener, and long-term SOL price history from CoinGecko.
2. **Cognition** (`src/cognition.py`) — two strategy engines decide *what* and *when*:
   - **MajorsEngine** — buys $2 of SOL once a week, but only while SOL trades above its 20-day average (a simple trend filter that pauses buying in downtrends).
   - **MemeEngine** — scores trending tokens by momentum, then runs a strict safety screen (RugCheck report, liquidity, token age, holder concentration) before any entry.
3. **Risk** (`src/risk.py`) — the cage. Every order must be approved here. It enforces per-trade caps, position limits, sleeve budgets, a daily-loss halt, and a portfolio drawdown kill switch.
4. **Execution** (`src/execution.py`) — routes swaps through the Jupiter aggregator. In **paper mode** (default) it simulates fills from live quotes without touching your wallet; in live mode it signs and sends real transactions.

Everything the bot knows — positions, fills, guardrail state — persists in a local SQLite ledger (`src/state.py`), so restarts are safe. Alerts, daily reports, performance metrics, and Telegram commands are handled by `src/notify.py`, `src/report.py`, `src/metrics.py`, and `src/commands.py`.

### The risk cage at a glance

| Guardrail | Default | Effect |
|---|---|---|
| Per-trade cap | $2 | No single meme bet exceeds 10% of the bankroll |
| Max meme positions | 5 | Exposure grows slowly |
| Sleeve budgets | $10 + $10 | Majors and meme money are fenced off from each other |
| Token safety screen | on | Rejects mintable/freezable tokens, thin LP, whale-heavy supply |
| Price-data guard | on | Rejects >3x single-cycle price jumps until a second source confirms |
| Daily loss limit | 5% | Halts all new entries for 24h |
| Drawdown kill switch | 20% | Stops trading entirely; requires manual reset |
| Take profit | +50% | Sells half the position |
| Stop loss | -40% | Full exit |
| Max hold | 72h | Time-based exit for meme positions |

All values are configurable via `.env` — scaling the bot up later means editing numbers, not code.

---

## Project structure

```
polywhale-agent/
├── src/
│   ├── agent.py        # Orchestrator: the main loop
│   ├── perception.py   # Market data (Jupiter, DexScreener, CoinGecko)
│   ├── cognition.py    # Strategy engines (majors DCA, meme momentum)
│   ├── risk.py         # The risk cage + portfolio watchdog
│   ├── execution.py    # Jupiter swaps (paper + live)
│   ├── state.py        # SQLite ledger (positions, fills, guardrails)
│   ├── config.py       # Typed configuration from .env
│   ├── notify.py       # Telegram alerts (log-only if unconfigured)
│   ├── report.py       # Daily summary reports (reports/<date>.md)
│   ├── metrics.py      # Win rate / profit factor / HTML dashboard
│   └── commands.py     # Telegram command channel (/status, /halt, ...)
├── tests/              # Pytest suite (risk cage + strategy logic)
├── ops/                # launchd service for auto-restart
├── legacy/             # Archived Polymarket bot (v1)
├── .env.example        # Template — copy to .env and fill in
├── .gitignore          # Keeps secrets and runtime data out of git
└── requirements.txt
```

---

## Getting started

### 1. Install

```bash
cd polywhale-agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Then edit `.env`:

- Leave `DRY_RUN=true` while learning (paper trading — no real funds at risk).
- Leave `WALLET_PRIVATE_KEY` empty in paper mode.
- Optionally add Telegram credentials for phone alerts (see below).

### 3. Run

```bash
python src/agent.py
```

You will see one status line per minute:

```
Cycle 12: SOL=$75.80 equity=$20.00 dayPnL=$+0.01 dd=0.0% open=2
```

### 4. Test

```bash
python -m pytest tests/ -q
```

---

## Telegram alerts

The bot can push notifications to your phone for: startup, every buy/sell, watchdog halts, kill-switch engagement, and the daily report.

1. Message **@BotFather** on Telegram, create a bot, and copy its token.
2. Message your new bot once (send `/start`).
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and find your `chat.id`.
4. Put both values in `.env`:

```
TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_CHAT_ID=<your chat id>
```

If these are empty, all alerts still appear in `polywhale.log` prefixed with `ALERT:`.

### Telegram commands

With Telegram configured, the channel is two-way. Send these to your bot (only your chat ID is authorized):

| Command | Effect |
|---|---|
| `/status` | Equity, day PnL, guardrail flags, open positions |
| `/halt` | Pause all new entries (manual halt) |
| `/resume` | Clear the manual halt |
| `/reset_kill` | Reset the drawdown kill switch |
| `/report` | Write today's report and return its path |
| `/metrics` | Win rate, profit factor, max drawdown |
| `/help` | Command list |

---

## Monitoring

- **Daily reports** — written to `reports/<date>.md` at each UTC midnight rollover (or on demand via `/report`).
- **Dashboard** — `reports/dashboard.html` is refreshed automatically every ~15 minutes and via `/metrics`. Open it in any browser: equity curve, win rate, profit factor, max drawdown. No server, no JS dependencies.
- **Heartbeat dead-man's switch** — the agent stamps a heartbeat into the ledger every cycle. A separate watchdog process (`src/watchdog.py`) checks it every minute and alerts via Telegram if it goes stale >3 min (frozen/hung bot) — including a recovery message when the bot comes back. Being a separate process is the point: a frozen bot cannot silence its own executioner.
- **On-chain reconciliation** — in live mode the bot compares real wallet balances (native SOL + SPL token accounts) against the ledger on startup and hourly. Any shortfall beyond 1% tolerance triggers an alert and halts new entries until reviewed.
- **Auto-restart (macOS)** — install the bot **and its watchdog** as launchd services so crashes and reboots never silently kill either:

```bash
bash ops/install_service.sh
```

Stop them again with `launchctl bootout gui/$(id -u)/com.polywhale.agent` (same for `com.polywhale.watchdog`).

---

## Going live (eventually)

Paper-trade first — ideally for several days — and study `polywhale.log` and `reports/`. When you choose to go live:

1. Create a **dedicated wallet** used only for this bot. Fund it with the bankroll: ~$10 USDC (majors sleeve buys SOL with USDC) + ~$10 SOL (meme sleeve) + a little extra SOL for fees.
2. Put that wallet's private key in `WALLET_PRIVATE_KEY` in `.env`. Never reuse a wallet that holds savings.
3. Set `DRY_RUN=false` and restart.

A free [Helius](https://helius.dev) RPC key in `RPC_URL` is recommended if the public endpoint feels slow.

### Safety rules

- **Never commit `.env`.** It is gitignored; `.env.example` is the shareable template.
- If a wallet key or bot token ever leaks, rotate it immediately (BotFather `/revoke` for tokens).
- The kill switch stops trading but does not auto-sell positions — decide exits manually if it fires.

---

## Limitations / roadmap

- Solana only (Base chain support deferred).
- Paper-mode fills assume quote prices; real swaps may get slightly worse fills.
- Strategy is deliberately simple; this project values discipline over cleverness.
