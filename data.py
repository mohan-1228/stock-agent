"""Price and market-cap fetching via yfinance (an unofficial Yahoo Finance wrapper).

Imports are inside the functions so the rest of the project (and the tests)
work without yfinance installed.
"""
import pandas as pd


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


def fetch_market_caps(tickers):
    """Latest market cap per ticker (NaN when unavailable)."""
    import yfinance as yf

    caps = {}
    for t in tickers:
        try:
            caps[t] = float(yf.Ticker(t).fast_info["market_cap"])
        except Exception:
            caps[t] = float("nan")
    return pd.Series(caps, name="market_cap", dtype="float64")
