"""Price and market-cap fetching via yfinance (an unofficial Yahoo Finance wrapper).

Imports are inside the functions so the rest of the project (and the tests)
work without yfinance installed.
"""
import pandas as pd
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass


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


# Periods that yf.download documents as valid in the installed yfinance (1.8.0, see the
# `period` docstring in yfinance/multi.py). Checking against this list turns a typo into a
# clear error instead of a confusing empty download.
YAHOO_PERIODS = ("1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max")


def split_into_batches(items, size):
    """Cut a list into chunks of at most `size` items, keeping the order.

    Asking Yahoo for ~100 tickers at a time instead of all 520 at once makes each request
    smaller and less likely to be blocked or time out.
    """
    if size < 1:
        raise ValueError("batch size must be at least 1")
    return [items[i:i + size] for i in range(0, len(items), size)]


@dataclass
class BatchResult:
    """What happened to one batch: its closes, or the error if it failed even after the retry."""
    number: int                    # 1-based position, for "batch 3 of 6"
    total: int                     # how many batches there are
    tickers: list                  # the tickers asked for in this batch
    closes: pd.DataFrame | None    # rows = days, columns = tickers; None when the batch failed
    error: str | None              # short reason when the batch failed, else None


def _fetch_with_retry(batch, period, fetch, sleep, retries, retry_pause_seconds):
    """Return (closes, None) on success or (None, reason) after all attempts fail.

    An empty result for the WHOLE batch is treated as a failure too: yf.download often prints
    network errors and returns an empty table instead of raising.
    """
    reason = None
    for attempt in range(retries + 1):
        if attempt > 0:
            sleep(retry_pause_seconds)  # give Yahoo a moment before trying again
        try:
            closes = fetch(batch, period)
        except Exception as exc:  # any error: network, Yahoo blocking, parsing
            reason = f"{type(exc).__name__}: {exc}"
            continue
        if closes is None or closes.empty:
            reason = "no data returned for the whole batch"
            continue
        return closes, None
    return None, reason


def fetch_closes_in_batches(
    tickers,
    period,
    batch_size=100,
    pause_seconds=2.0,
    retries=1,
    retry_pause_seconds=5.0,
    fetch=fetch_closes,
    sleep=time.sleep,
):
    """Download closes batch by batch, yielding one BatchResult as soon as each batch is done.

    It is a generator (a conveyor belt): the caller can save each batch right away, so a crash
    later on keeps everything already saved. A failed batch never stops the batches after it.
    `fetch` and `sleep` are parameters so tests can pass fakes (no network, no waiting).
    """
    batches = split_into_batches(list(tickers), batch_size)
    for number, batch in enumerate(batches, start=1):
        if number > 1:
            sleep(pause_seconds)  # short pause between batches so Yahoo doesn't block us
        closes, error = _fetch_with_retry(batch, period, fetch, sleep, retries, retry_pause_seconds)
        yield BatchResult(number, len(batches), batch, closes, error)
