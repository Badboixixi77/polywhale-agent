"""Performance metrics and a self-contained HTML dashboard.

Stats are computed from the ledger alone (closed positions + equity
history), so they stay honest in both paper and live mode. The dashboard
is a single HTML file with an inline SVG equity curve — open it in any
browser, no server or JS libraries involved.
"""
import os
from datetime import datetime, timezone


class MetricsEngine:
    def __init__(self, cfg, ledger, reports_dir: str = "reports"):
        self.cfg = cfg
        self.ledger = ledger
        self.reports_dir = reports_dir

    # ---- stats ----
    def compute(self) -> dict:
        closed = self.ledger.closed_positions()
        wins = [p for p in closed if p["realized_pnl"] > 0]
        losses = [p for p in closed if p["realized_pnl"] <= 0]
        gross_win = sum(p["realized_pnl"] for p in wins)
        gross_loss = abs(sum(p["realized_pnl"] for p in losses))
        history = self.ledger.equity_history(limit=10000)
        equities = [e for _, e in history]
        return {
            "closed_trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(closed)) if closed else None,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
            "avg_win": (gross_win / len(wins)) if wins else 0.0,
            "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
            "realized_pnl": self.ledger.realized_pnl_total(),
            "open_positions": len(self.ledger.open_positions()),
            "max_drawdown": self._max_drawdown(equities),
            "equity_points": len(history),
        }

    @staticmethod
    def _max_drawdown(equities: list) -> float:
        peak, max_dd = 0.0, 0.0
        for eq in equities:
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak)
        return max_dd

    def sleeve_stats(self) -> dict:
        """Per-sleeve attribution: which machine earns, which one bleeds."""
        by_sleeve = {}
        for p in self.ledger.closed_positions():
            by_sleeve.setdefault(p["sleeve"], []).append(p["realized_pnl"])
        out = {}
        for sleeve, pnls in sorted(by_sleeve.items()):
            wins = [x for x in pnls if x > 0]
            losses = [x for x in pnls if x <= 0]
            wr = len(wins) / len(pnls)
            avg_win = sum(wins) / len(wins) if wins else 0.0
            avg_loss = sum(losses) / len(losses) if losses else 0.0
            out[sleeve] = {
                "trades": len(pnls),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": wr,
                "pnl": sum(pnls),
                "avg_win": avg_win,
                "avg_loss": avg_loss,
                "expectancy": wr * avg_win + (1 - wr) * avg_loss,
            }
        return out

    # ---- rendering ----
    def render_text(self, stats: dict = None) -> str:
        s = stats or self.compute()
        wr = f"{s['win_rate']:.0%}" if s["win_rate"] is not None else "n/a"
        pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] is not None else "n/a"
        base = (
            f"PolyWhale metrics | mode={'PAPER' if self.cfg.dry_run else 'LIVE'}\n"
            f"Closed trades: {s['closed_trades']} (W {s['wins']} / L {s['losses']})\n"
            f"Win rate: {wr} | Profit factor: {pf}\n"
            f"Avg win: ${s['avg_win']:+.2f} | Avg loss: ${s['avg_loss']:+.2f}\n"
            f"Realized PnL: ${s['realized_pnl']:+.2f} | Open positions: {s['open_positions']}\n"
            f"Max drawdown: {s['max_drawdown']:.1%}"
        )
        sleeves = self.sleeve_stats()
        for name, st in sleeves.items():
            base += (
                f"\n[{name}] {st['trades']} trades (W {st['wins']}/L {st['losses']}) "
                f"wr {st['win_rate']:.0%} | PnL ${st['pnl']:+.2f} | "
                f"expectancy ${st['expectancy']:+.3f}/trade"
            )
        return base

    def render_html(self, stats: dict = None, max_points: int = 600) -> str:
        s = stats or self.compute()
        history = self.ledger.equity_history(limit=10000)
        if len(history) > max_points:
            step = len(history) / max_points
            history = [history[int(i * step)] for i in range(max_points)] + [history[-1]]
        svg = self._equity_svg(history)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        wr = f"{s['win_rate']:.0%}" if s["win_rate"] is not None else "n/a"
        pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] is not None else "n/a"
        mode = "PAPER" if self.cfg.dry_run else "LIVE"
        cards = [
            ("Mode", mode),
            ("Realized PnL", f"${s['realized_pnl']:+.2f}"),
            ("Closed trades", str(s["closed_trades"])),
            ("Win rate", wr),
            ("Profit factor", pf),
            ("Avg win", f"${s['avg_win']:+.2f}"),
            ("Avg loss", f"${s['avg_loss']:+.2f}"),
            ("Max drawdown", f"{s['max_drawdown']:.1%}"),
            ("Open positions", str(s["open_positions"])),
        ]
        cards_html = "".join(
            f'<div class="card"><div class="label">{k}</div><div class="value">{v}</div></div>'
            for k, v in cards
        )
        rows_html = "".join(
            f"<tr><td>{name}</td><td>{st['trades']}</td>"
            f"<td>{st['wins']}/{st['losses']}</td><td>{st['win_rate']:.0%}</td>"
            f"<td>${st['pnl']:+.2f}</td><td>${st['expectancy']:+.3f}</td></tr>"
            for name, st in self.sleeve_stats().items()
        )
        sleeves_panel = (
            f"<div class='panel' style='margin-top:16px'>"
            f"<div class='label' style='margin-bottom:8px'>Sleeve attribution</div>"
            f"<table style='color:#e6edf3;font-size:13px;border-collapse:collapse;width:100%'>"
            f"<tr style='color:#8b949e'><th align='left'>Sleeve</th><th>Trades</th><th>W/L</th>"
            f"<th>Win rate</th><th>PnL</th><th>Expectancy</th></tr>{rows_html}</table></div>"
            if rows_html else ""
        )
        return (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<title>PolyWhale Dashboard</title><style>"
            "body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:24px}"
            "h1{font-size:20px;margin:0 0 4px} .sub{color:#8b949e;font-size:12px;margin-bottom:20px}"
            ".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:24px}"
            ".card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 14px}"
            ".label{color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:.05em}"
            ".value{font-size:20px;font-weight:600;margin-top:4px}"
            ".panel{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px}"
            "</style></head><body>"
            "<h1>PolyWhale Dashboard</h1>"
            f"<div class='sub'>Generated {now} | equity samples: {s['equity_points']}</div>"
            f"<div class='grid'>{cards_html}</div>"
            f"<div class='panel'><div class='label' style='margin-bottom:8px'>Equity curve</div>{svg}</div>"
            f"{sleeves_panel}"
            "</body></html>"
        )

    @staticmethod
    def _equity_svg(history: list, width: int = 860, height: int = 220) -> str:
        if len(history) < 2:
            return "<div class='sub'>Not enough equity samples yet — check back after a few cycles.</div>"
        equities = [e for _, e in history]
        lo, hi = min(equities), max(equities)
        span = (hi - lo) or 1.0
        pad = 8.0
        points = []
        for i, eq in enumerate(equities):
            x = pad + i * (width - 2 * pad) / (len(equities) - 1)
            y = height - pad - (eq - lo) / span * (height - 2 * pad)
            points.append(f"{x:.1f},{y:.1f}")
        return (
            f"<svg width='100%' viewBox='0 0 {width} {height}' preserveAspectRatio='none'>"
            f"<polyline fill='none' stroke='#3fb950' stroke-width='2' points='{' '.join(points)}'/>"
            f"<text x='{pad}' y='14' fill='#8b949e' font-size='11'>high ${hi:.2f}</text>"
            f"<text x='{pad}' y='{height - 2}' fill='#8b949e' font-size='11'>low ${lo:.2f}</text>"
            "</svg>"
        )

    def write_dashboard(self, stats: dict = None) -> str:
        os.makedirs(self.reports_dir, exist_ok=True)
        path = os.path.join(self.reports_dir, "dashboard.html")
        with open(path, "w") as f:
            f.write(self.render_html(stats))
        return path
