"""One-time history backfill: download ~5 years of daily closes into the `prices` table.

    python backfill.py                      # every ticker in tickers.csv, 5 years
    python backfill.py --tickers AAPL,NVDA  # try a few first
    python backfill.py --period 10y

It only writes the `prices` table (never `daily_returns`). Rows are saved with INSERT OR REPLACE,
so re-running is safe: it refreshes old rows with today's split/dividend adjustment and never
duplicates them. Empty results are never saved, so good rows are never overwritten with nothing.
"""
import argparse
import sys
import time
from dataclasses import dataclass, field
from datetime import timedelta

import config
from data import YAHOO_PERIODS, fetch_closes, fetch_closes_in_batches
from storage import connect, save_prices
from universe import load_universe

# A ticker whose first saved day is more than this many calendar days after the earliest
# first day of the run is a "shorter history" (recent listing). The slack covers holidays
# and a missing bar or two on the very first days.
SHORT_HISTORY_SLACK_DAYS = 7

# A ticker with fewer saved rows than this (in the whole prices table) has so little history
# that it may have stopped trading, e.g. KVUE after its acquisition. Warned about, not "recent listing".
MIN_HISTORY_ROWS = 30


@dataclass
class BackfillSummary:
    """Everything the end-of-run summary prints."""
    rows_saved: int = 0
    first_dates: dict = field(default_factory=dict)  # ticker -> first date with data
    saved_rows: dict = field(default_factory=dict)   # ticker -> rows in the prices table after saving
    last_date: object = None                           # newest date seen in any batch
    no_data: list = field(default_factory=list)      # batch worked, but this ticker had nothing
    failed: list = field(default_factory=list)       # in a batch that failed even after the retry
    failed_reasons: list = field(default_factory=list)

    def very_little_history(self):
        """Tickers with fewer than MIN_HISTORY_ROWS saved rows in total: may have stopped trading."""
        return sorted(t for t, n in self.saved_rows.items() if n < MIN_HISTORY_ROWS)

    def short_history(self):
        """Tickers with less than the full period (normal for recent listings; info only).

        Tickers with very little history are left out: they get a warning instead.
        """
        if not self.first_dates:
            return []
        earliest = min(self.first_dates.values())
        cutoff = earliest + timedelta(days=SHORT_HISTORY_SLACK_DAYS)
        too_few = set(self.very_little_history())
        return sorted(t for t, d in self.first_dates.items() if d > cutoff and t not in too_few)


def plural(n, word):
    """plural(1, "row") -> "1 row", plural(5, "row") -> "5 rows"."""
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def parse_tickers(text):
    """'aapl, NVDA,,aapl' -> ['AAPL', 'NVDA'] (upper-case, no blanks, no repeats, order kept)."""
    seen = []
    for part in text.split(","):
        ticker = part.strip().upper()
        if ticker and ticker not in seen:
            seen.append(ticker)
    return seen


def unknown_tickers(requested, known):
    """Requested tickers that are not in tickers.csv (we warn but still fetch them)."""
    known = set(known)
    return [t for t in requested if t not in known]


def count_saved_rows(conn, ticker):
    """How many closes the prices table holds for one ticker (all dates, not just this run)."""
    return conn.execute("SELECT COUNT(*) FROM prices WHERE ticker = ?", (ticker,)).fetchone()[0]


def run_backfill(conn, tickers, period, batch_size, pause_seconds,
                 fetch=fetch_closes, sleep=time.sleep, log=print):
    """Fetch closes batch by batch and save each batch right away. Returns a BackfillSummary.

    Saving after every batch means a failure (or Ctrl+C) later keeps all earlier batches.
    """
    summary = BackfillSummary()
    batches = fetch_closes_in_batches(tickers, period, batch_size=batch_size,
                                      pause_seconds=pause_seconds, fetch=fetch, sleep=sleep)
    for batch in batches:
        label = f"Batch {batch.number} of {batch.total} ({len(batch.tickers)} tickers)"
        if batch.closes is None:
            summary.failed.extend(batch.tickers)
            summary.failed_reasons.append(f"batch {batch.number}: {batch.error}")
            log(f"{label}: FAILED after retry ({batch.error}). Nothing saved for this batch.")
            continue

        closes = batch.closes.reindex(columns=batch.tickers)  # missing tickers become empty columns
        saved = save_prices(conn, closes)                       # skips empty values by itself
        summary.rows_saved += saved
        for ticker in batch.tickers:
            values = closes[ticker].dropna()
            if values.empty:
                summary.no_data.append(ticker)
            else:
                summary.first_dates[ticker] = values.index[0].date()
                summary.saved_rows[ticker] = count_saved_rows(conn, ticker)
                last = values.index[-1].date()
                if summary.last_date is None or last > summary.last_date:
                    summary.last_date = last
        log(f"{label}: saved {saved:,} rows (total so far: {summary.rows_saved:,})")
    return summary


def print_summary(summary, n_requested, period, log=print):
    """Plain-language end-of-run report, including what to retry."""
    log("")
    log("=== Backfill summary ===")
    log(f"Rows saved: {summary.rows_saved:,}")
    if summary.first_dates:
        log(f"Date range: {min(summary.first_dates.values())} to {summary.last_date}")
    log(f"Tickers with data: {len(summary.first_dates)} of {n_requested}")
    few = summary.very_little_history()
    if few:
        details = ", ".join(f"{t} ({plural(summary.saved_rows[t], 'row')})" for t in few)
        log(f"Warning: very little history, may have stopped trading: {details}")
    short = summary.short_history()
    if short:
        has = "ticker has" if len(short) == 1 else "tickers have"
        log(f"Info: {len(short)} {has} less than the full {period} of history "
            f"(normal for recent listings), e.g. {', '.join(short[:10])}")
    if summary.no_data:
        log(f"No data returned ({len(summary.no_data)}): {', '.join(summary.no_data)}")
    if summary.failed:
        log(f"FAILED ({len(summary.failed)} tickers in failed batches):")
        for reason in summary.failed_reasons:
            log(f"  {reason}")
        log("Everything else was saved. Retry the failed ones with:")
        log(f"  python backfill.py --period {period} --tickers {','.join(summary.failed)}")


def main():
    parser = argparse.ArgumentParser(description="Download years of daily closes into the prices table.")
    parser.add_argument("--period", default=config.BACKFILL_PERIOD,
                        help=f"how far back (default {config.BACKFILL_PERIOD}); one of {', '.join(YAHOO_PERIODS)}")
    parser.add_argument("--tickers", help="comma-separated list, e.g. AAPL,NVDA (default: all of tickers.csv)")
    args = parser.parse_args()

    if args.period not in YAHOO_PERIODS:
        sys.exit(f"Unknown period '{args.period}'. Use one of: {', '.join(YAHOO_PERIODS)}")

    known = load_universe(config.TICKERS_CSV)["ticker"].tolist()
    if args.tickers:
        tickers = parse_tickers(args.tickers)
        if not tickers:
            sys.exit("--tickers was given but contains no tickers.")
        unknown = unknown_tickers(tickers, known)
        if unknown:
            print(f"Warning: not in tickers.csv, fetching anyway: {', '.join(unknown)}")
    else:
        tickers = known

    print(f"Backfilling {len(tickers)} tickers, period {args.period}, "
          f"batches of {config.BACKFILL_BATCH_SIZE}, {config.BACKFILL_PAUSE_SECONDS:g}s pause")
    started = time.monotonic()
    conn = connect(config.DB_PATH)
    try:
        summary = run_backfill(conn, tickers, args.period,
                               config.BACKFILL_BATCH_SIZE, config.BACKFILL_PAUSE_SECONDS)
        total_rows = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    finally:
        conn.close()

    print_summary(summary, len(tickers), args.period)
    print(f"The prices table now holds {total_rows:,} rows. Took {time.monotonic() - started:.0f}s.")
    if summary.failed:
        sys.exit(1)  # non-zero exit so a failure can't be missed


if __name__ == "__main__":
    main()
