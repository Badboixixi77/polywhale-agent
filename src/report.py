"""Daily summary report built from the ledger, written to reports/<date>.md."""
import os
import time
from datetime import datetime, timezone


class Reporter:
    def __init__(self, cfg, ledger, reports_dir: str = "reports"):
        self.cfg = cfg
        self.ledger = ledger
        self.reports_dir = reports_dir

    def build_summary(self, status: dict) -> str:
        now = datetime.now(timezone.utc)
        open_positions = self.ledger.open_positions()
        fills_24h = self.ledger.fills_since(time.time() - 24 * 3600)
        buys = sum(1 for f in fills_24h if f["side"] == "buy")
        sells = sum(1 for f in fills_24h if f["side"] == "sell")
        lines = [
            f"# PolyWhale daily report — {now.strftime('%Y-%m-%d %H:%M UTC')}",
            f"Mode: {'PAPER' if self.cfg.dry_run else 'LIVE'}",
            "",
            f"Equity: ${status['equity']:.2f} | Day PnL: ${status['daily_pnl']:+.2f} | "
            f"Drawdown: {status['drawdown']:.1%} | Peak: ${status['peak_equity']:.2f}",
            f"Guardrails: {'HALTED' if status['halted'] else 'ok'} / "
            f"{'KILLED' if status['killed'] else 'ok'}",
            "",
            f"Budgets: majors ${self.ledger.invested_usd('majors'):.2f}/${self.cfg.majors_budget:.2f} | "
            f"meme ${self.ledger.invested_usd('meme'):.2f}/${self.cfg.meme_budget:.2f}",
            f"Realized PnL (all-time): ${self.ledger.realized_pnl_total():+.2f}",
            f"Fills (24h): {buys} buys / {sells} sells",
            "",
            f"Open positions ({len(open_positions)}):",
        ]
        if not open_positions:
            lines.append("- none")
        for p in open_positions:
            value = p["amount"] * p["current_price"]
            ret = (p["current_price"] / p["entry_price"] - 1) if p["entry_price"] > 0 else 0.0
            lines.append(
                f"- [{p['sleeve']}] {p['symbol']}: cost ${p['size_usd']:.2f}, value ${value:.2f}, "
                f"entry ${p['entry_price']:.8g}, now ${p['current_price']:.8g} ({ret:+.1%})"
            )
        return "\n".join(lines)

    def write_report(self, summary: str, day: str = None) -> str:
        day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        os.makedirs(self.reports_dir, exist_ok=True)
        path = os.path.join(self.reports_dir, f"{day}.md")
        with open(path, "w") as f:
            f.write(summary + "\n")
        return path
