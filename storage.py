"""SQLite history so you can track how each stock moves over time."""
import sqlite3
from pathlib import Path

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_returns (
    trade_date TEXT NOT NULL,
    ticker     TEXT NOT NULL,
    close      REAL,
    day_pct    REAL,
    week_pct   REAL,
    month_pct  REAL,
    market_cap REAL,
    PRIMARY KEY (trade_date, ticker)
);
"""


def connect(db_path):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(SCHEMA)
    return conn


def _clean(x):
    return None if pd.isna(x) else float(x)


def save_snapshot(conn, trade_date, returns: pd.DataFrame):
    """Store one day's numbers. Re-running the same day overwrites it."""
    rows = [
        (
            str(trade_date),
            ticker,
            _clean(r["close"]),
            _clean(r["day_pct"]),
            _clean(r["week_pct"]),
            _clean(r["month_pct"]),
            _clean(r.get("market_cap", float("nan"))),
        )
        for ticker, r in returns.iterrows()
    ]
    conn.executemany("INSERT OR REPLACE INTO daily_returns VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()


def ticker_history(conn, ticker, limit=30) -> pd.DataFrame:
    """Most recent saved rows for one ticker, newest first."""
    return pd.read_sql_query(
        "SELECT trade_date, close, day_pct, week_pct, month_pct "
        "FROM daily_returns WHERE ticker = ? ORDER BY trade_date DESC LIMIT ?",
        conn,
        params=(ticker.upper(), limit),
    )
