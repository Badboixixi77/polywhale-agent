"""SQLite ledger: positions, fills, price history, and guardrail meta-state.

Everything the bot knows about its own portfolio survives restarts here.
"""
import sqlite3
import threading
import time


class Ledger:
    def __init__(self, path: str = "polywhale_ledger.db"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._create_tables()

    def _create_tables(self):
        with self._lock, self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sleeve TEXT NOT NULL,
                    mint TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    size_usd REAL NOT NULL,
                    amount REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    current_price REAL NOT NULL,
                    peak_price REAL NOT NULL,
                    opened_at REAL NOT NULL,
                    closed_at REAL,
                    status TEXT NOT NULL DEFAULT 'open',
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    close_reason TEXT,
                    tp_half_done INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    sleeve TEXT NOT NULL,
                    mint TEXT NOT NULL,
                    side TEXT NOT NULL,
                    usd REAL NOT NULL,
                    amount REAL NOT NULL,
                    price REAL NOT NULL,
                    mode TEXT NOT NULL,
                    note TEXT
                );
                CREATE TABLE IF NOT EXISTS price_history (
                    ts REAL NOT NULL,
                    sol_price REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS equity_history (
                    ts REAL NOT NULL,
                    equity REAL NOT NULL
                );
            """)

    # ---- meta ----
    def get_meta(self, key: str, default=None) -> str:
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    # ---- fills ----
    def record_fill(self, sleeve, mint, side, usd, amount, price, mode, note=""):
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO fills (ts, sleeve, mint, side, usd, amount, price, mode, note) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (time.time(), sleeve, mint, side, usd, amount, price, mode, note),
            )

    def fills_since(self, ts: float):
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM fills WHERE ts>=? ORDER BY ts", (ts,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- positions ----
    def open_or_add_position(self, sleeve, mint, symbol, usd, amount, price):
        """Open a position, or average into an existing open one (majors DCA)."""
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM positions WHERE sleeve=? AND mint=? AND status='open'",
                (sleeve, mint),
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO positions (sleeve, mint, symbol, size_usd, amount, entry_price, "
                    "current_price, peak_price, opened_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (sleeve, mint, symbol, usd, amount, price, price, price, time.time()),
                )
            else:
                new_amount = row["amount"] + amount
                new_size = row["size_usd"] + usd
                self.conn.execute(
                    "UPDATE positions SET size_usd=?, amount=?, entry_price=?, current_price=?, peak_price=? WHERE id=?",
                    (new_size, new_amount, new_size / new_amount, price, max(row["peak_price"], price), row["id"]),
                )

    def open_positions(self):
        with self._lock:
            rows = self.conn.execute("SELECT * FROM positions WHERE status='open'").fetchall()
        return [dict(r) for r in rows]

    def closed_positions(self):
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM positions WHERE status='closed' ORDER BY closed_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def position_count(self, sleeve) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM positions WHERE sleeve=? AND status='open'", (sleeve,)
            ).fetchone()
        return row["n"]

    def invested_usd(self, sleeve=None) -> float:
        if sleeve:
            sql = "SELECT COALESCE(SUM(size_usd),0) AS s FROM positions WHERE sleeve=? AND status='open'"
            args = (sleeve,)
        else:
            sql = "SELECT COALESCE(SUM(size_usd),0) AS s FROM positions WHERE status='open'"
            args = ()
        with self._lock:
            row = self.conn.execute(sql, args).fetchone()
        return row["s"]

    def update_price(self, mint: str, price: float):
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE positions SET current_price=?, peak_price=MAX(peak_price, ?) "
                "WHERE mint=? AND status='open'",
                (price, price, mint),
            )

    def reduce_position(self, position_id: int, fraction: float, exit_price: float) -> float:
        """Sell a fraction of a position. Returns realized PnL for that slice."""
        fraction = min(max(fraction, 0.0), 1.0)
        with self._lock, self.conn:
            row = self.conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
            if row is None or row["status"] != "open":
                return 0.0
            cost_basis = row["size_usd"] * fraction
            proceeds = row["amount"] * fraction * exit_price
            pnl = proceeds - cost_basis
            if fraction >= 1.0:
                self.conn.execute(
                    "UPDATE positions SET status='closed', closed_at=?, realized_pnl=realized_pnl+?, "
                    "size_usd=0, amount=0 WHERE id=?",
                    (time.time(), pnl, position_id),
                )
            else:
                self.conn.execute(
                    "UPDATE positions SET size_usd=size_usd-?, amount=amount*(1-?), realized_pnl=realized_pnl+? WHERE id=?",
                    (cost_basis, fraction, pnl, position_id),
                )
            return pnl

    def mark_tp_half_done(self, position_id: int):
        with self._lock, self.conn:
            self.conn.execute("UPDATE positions SET tp_half_done=1 WHERE id=?", (position_id,))

    def realized_pnl_total(self) -> float:
        with self._lock:
            row = self.conn.execute("SELECT COALESCE(SUM(realized_pnl),0) AS s FROM positions").fetchone()
        return row["s"]

    # ---- SOL price history (trend filter) ----
    def append_price(self, ts: float, price: float):
        with self._lock, self.conn:
            self.conn.execute("INSERT INTO price_history (ts, sol_price) VALUES (?,?)", (ts, price))

    def price_history(self, limit: int = 100):
        with self._lock:
            rows = self.conn.execute(
                "SELECT ts, sol_price FROM price_history ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [(r["ts"], r["sol_price"]) for r in reversed(rows)]

    def close(self):
        self.conn.close()

    # ---- equity history (metrics / dashboard) ----
    def record_equity(self, ts: float, equity: float):
        with self._lock, self.conn:
            self.conn.execute("INSERT INTO equity_history (ts, equity) VALUES (?,?)", (ts, equity))

    def equity_history(self, limit: int = 5000):
        with self._lock:
            rows = self.conn.execute(
                "SELECT ts, equity FROM equity_history ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [(r["ts"], r["equity"]) for r in reversed(rows)]
