"""Price and market-cap fetching via yfinance (an unofficial Yahoo Finance wrapper).

Imports are inside the functions so the rest of the project (and the tests)
work without yfinance installed.
"""
import pandas as pd
import time
from concurrent.futures import ThreadPoolExecutor


def fetch_closes(tickers, period="3mo"):
    """Daily adjusted closes: rows = trading days, columns = tickers."""
    import yfinance as yf

    raw = yf.download(
        tickers,
        period=period,
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=True,
    )
    if raw.empty:
        return pd.DataFrame()
    closes = raw["Close"]
    if isinstance(closes, pd.Series):  # single-ticker edge case
        closes = closes.to_frame(name=tickers[0])
    return closes.dropna(how="all").sort_index()


def _fetch_one_cap(ticker, retries=1):
    """Market cap for one ticker; NaN if Yahoo has none after a retry."""
    import yfinance as yf

    for attempt in range(retries + 1):
        try:
            return float(yf.Ticker(ticker).fast_info["market_cap"])
        except Exception:
            if attempt < retries:
                time.sleep(0.5)  # brief pause before the retry
    return float("nan")


def fetch_market_caps(tickers, workers=8):
    """Latest market cap per ticker (NaN when unavailable), fetched in parallel.

    Each lookup spends most of its time waiting on the network, so running several
    at once cuts the total time roughly by the number of workers.
    """
    with ThreadPoolExecutor(max_workers=workers) as pool:
        values = list(pool.map(_fetch_one_cap, tickers))  # map keeps the original order
    return pd.Series(values, index=tickers, name="market_cap", dtype="float64")
