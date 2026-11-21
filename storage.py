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
CREATE TABLE IF NOT EXISTS prices (
    trade_date TEXT NOT NULL,
    ticker     TEXT NOT NULL,
    close      REAL NOT NULL,
    PRIMARY KEY (trade_date, ticker)
);
"""


def connect(db_path, check_same_thread=True):
    """Open the database and create the tables if needed.

    The API passes check_same_thread=False: FastAPI may open the connection on one worker
    thread and use it on another. That is safe because each request gets its own connection.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.executescript(SCHEMA)
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


def latest_snapshot(conn):
    """(trade_date, DataFrame indexed by ticker) for the newest saved day.

    Returns (None, empty DataFrame) when nothing has been saved yet.
    """
    row = conn.execute("SELECT MAX(trade_date) FROM daily_returns").fetchone()
    if row is None or row[0] is None:
        return None, pd.DataFrame()
    df = pd.read_sql_query(
        "SELECT ticker, close, day_pct, week_pct, month_pct, market_cap "
        "FROM daily_returns WHERE trade_date = ?",
        conn,
        params=(row[0],),
        index_col="ticker",
    )
    return row[0], df.astype("float64")


def save_prices(conn, closes: pd.DataFrame):
    """Store the daily close of every ticker for every day in `closes` (rows = days, columns = tickers).

    Safe to run every day: days already saved are simply overwritten with the same values.
    """
    rows = [
        (str(day.date()), ticker, float(value))
        for ticker in closes.columns
        for day, value in closes[ticker].dropna().items()
    ]
    conn.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?)", rows)
    conn.commit()
    return len(rows)


def price_series(conn, ticker, days=63) -> pd.DataFrame:
    """The last `days` saved closes for one ticker, oldest first (ready to draw as a line)."""
    df = pd.read_sql_query(
        "SELECT trade_date, close FROM prices WHERE ticker = ? ORDER BY trade_date DESC LIMIT ?",
        conn,
        params=(ticker.upper(), int(days)),
    )
    return df.iloc[::-1].reset_index(drop=True)