"""Tests for the 5-year history backfill. No network: fetching and sleeping are faked."""
import pandas as pd
import pytest

from backfill import parse_tickers, plural, print_summary, run_backfill, unknown_tickers
from data import fetch_closes_in_batches, split_into_batches
from storage import connect, save_prices

DAYS = pd.to_datetime(["2026-11-17", "2026-11-18", "2026-11-19"])


def fake_closes(tickers, period="5y"):
    """Three days of made-up closes for every ticker asked for."""
    return pd.DataFrame({t: [10.0, 11.0, 12.0] for t in tickers}, index=DAYS)


class FakeFetch:
    """Records each call; raises on the call numbers in `fail_calls`, returns empty on `empty_calls` (1-based)."""

    def __init__(self, fail_calls=(), empty_calls=()):
        self.calls = []
        self.fail_calls = set(fail_calls)
        self.empty_calls = set(empty_calls)

    def __call__(self, tickers, period):
        self.calls.append(list(tickers))
        if len(self.calls) in self.fail_calls:
            raise ConnectionError("Yahoo said no")
        if len(self.calls) in self.empty_calls:
            return pd.DataFrame()
        return fake_closes(tickers, period)


class FakeSleep:
    def __init__(self):
        self.pauses = []

    def __call__(self, seconds):
        self.pauses.append(seconds)


def tickers(n):
    return [f"T{i:03d}" for i in range(n)]


# --- data.py: batching -------------------------------------------------------

def test_split_into_batches_keeps_order_and_sizes():
    assert split_into_batches(tickers(5), 2) == [["T000", "T001"], ["T002", "T003"], ["T004"]]
    assert split_into_batches([], 100) == []
    with pytest.raises(ValueError):
        split_into_batches(tickers(3), 0)


def test_batches_have_right_sizes_and_pause_only_between_batches():
    fetch, sleep = FakeFetch(), FakeSleep()
    results = list(fetch_closes_in_batches(tickers(250), "5y", batch_size=100,
                                           pause_seconds=2.0, fetch=fetch, sleep=sleep))
    assert [len(c) for c in fetch.calls] == [100, 100, 50]
    assert sleep.pauses == [2.0, 2.0]  # between 1-2 and 2-3, never after the last batch
    assert [(r.number, r.total) for r in results] == [(1, 3), (2, 3), (3, 3)]
    assert all(r.error is None and r.closes is not None for r in results)


def test_failed_batch_is_retried_once_and_then_succeeds():
    fetch, sleep = FakeFetch(fail_calls={1}), FakeSleep()
    results = list(fetch_closes_in_batches(tickers(3), "5y", batch_size=100,
                                           retry_pause_seconds=5.0, fetch=fetch, sleep=sleep))
    assert len(fetch.calls) == 2           # first try failed, retry worked
    assert sleep.pauses == [5.0]           # waited before the retry
    assert results[0].error is None
    assert list(results[0].closes.columns) == tickers(3)


def test_batch_failing_twice_is_reported_and_next_batch_still_runs():
    fetch, sleep = FakeFetch(fail_calls={1, 2}), FakeSleep()  # batch 1: try + retry both fail
    results = list(fetch_closes_in_batches(tickers(4), "5y", batch_size=2, fetch=fetch, sleep=sleep))
    assert len(fetch.calls) == 3
    assert results[0].closes is None
    assert "ConnectionError" in results[0].error
    assert results[0].tickers == ["T000", "T001"]
    assert results[1].error is None
    assert list(results[1].closes.columns) == ["T002", "T003"]


def test_empty_result_for_whole_batch_counts_as_failure():
    fetch, sleep = FakeFetch(empty_calls={1, 2}), FakeSleep()
    [result] = list(fetch_closes_in_batches(tickers(2), "5y", fetch=fetch, sleep=sleep))
    assert result.closes is None
    assert result.error == "no data returned for the whole batch"


# --- backfill.py: saving, reporting, re-running ------------------------------

def count_prices(conn):
    return conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]


def quiet(*_):
    """A log function that prints nothing, so test output stays clean."""


def test_backfill_saves_all_batches_and_summary_counts(tmp_path):
    conn = connect(tmp_path / "t.db")
    summary = run_backfill(conn, tickers(5), "5y", batch_size=2, pause_seconds=2.0,
                           fetch=FakeFetch(), sleep=FakeSleep(), log=quiet)
    assert summary.rows_saved == 15 == count_prices(conn)   # 5 tickers x 3 days
    assert len(summary.first_dates) == 5
    assert summary.last_date.isoformat() == "2026-11-19"
    assert summary.no_data == [] and summary.failed == []


def test_failing_batch_is_reported_and_other_batches_are_saved(tmp_path):
    conn = connect(tmp_path / "t.db")
    lines = []
    summary = run_backfill(conn, tickers(4), "5y", batch_size=2, pause_seconds=0,
                           fetch=FakeFetch(fail_calls={1, 2}), sleep=FakeSleep(), log=lines.append)
    assert summary.failed == ["T000", "T001"]
    assert count_prices(conn) == 6                           # batch 2 still saved (2 x 3 days)
    assert any("FAILED" in line for line in lines)

    out = []
    print_summary(summary, 4, "5y", log=out.append)
    assert "  python backfill.py --period 5y --tickers T000,T001" in out


def test_saving_twice_does_not_duplicate_rows(tmp_path):
    conn = connect(tmp_path / "t.db")
    for _ in range(2):
        run_backfill(conn, tickers(3), "5y", batch_size=100, pause_seconds=0,
                     fetch=FakeFetch(), sleep=FakeSleep(), log=quiet)
    assert count_prices(conn) == 9


def test_failed_batch_never_deletes_existing_rows(tmp_path):
    conn = connect(tmp_path / "t.db")
    run_backfill(conn, tickers(2), "5y", 100, 0, fetch=FakeFetch(), sleep=FakeSleep(), log=quiet)
    run_backfill(conn, tickers(2), "5y", 100, 0, fetch=FakeFetch(fail_calls={1, 2}),
                 sleep=FakeSleep(), log=quiet)
    assert count_prices(conn) == 6


def test_ticker_without_data_is_reported_and_short_history_is_not(tmp_path):
    def fetch(batch, period):
        days = pd.bdate_range("2021-11-19", "2026-11-19")
        df = pd.DataFrame({"OLD": 1.0, "NEW": 2.0, "GONE": float("nan")}, index=days)
        df.loc[df.index < "2025-06-02", "NEW"] = float("nan")   # listed mid-2025
        return df[[t for t in batch if t != "MISSING"]]         # MISSING: column left out

    conn = connect(tmp_path / "t.db")
    summary = run_backfill(conn, ["OLD", "NEW", "GONE", "MISSING"], "5y", 100, 0,
                           fetch=fetch, sleep=FakeSleep(), log=quiet)
    assert summary.no_data == ["GONE", "MISSING"]
    assert summary.failed == []
    assert summary.short_history() == ["NEW"]

    out = []
    print_summary(summary, 4, "5y", log=out.append)
    assert "Tickers with data: 2 of 4" in out
    assert any(line.startswith("Info: 1 ticker has less than the full 5y") for line in out)


def test_backfill_never_touches_daily_returns(tmp_path):
    conn = connect(tmp_path / "t.db")
    run_backfill(conn, tickers(3), "5y", 100, 0, fetch=FakeFetch(), sleep=FakeSleep(), log=quiet)
    assert conn.execute("SELECT COUNT(*) FROM daily_returns").fetchone()[0] == 0


def test_plural():
    assert plural(1, "row") == "1 row"
    assert plural(5, "row") == "5 rows"


def test_parse_and_unknown_tickers():
    assert parse_tickers(" aapl, NVDA,,aapl ") == ["AAPL", "NVDA"]
    assert unknown_tickers(["AAPL", "ZZZZ"], ["AAPL", "NVDA"]) == ["ZZZZ"]


def test_ticker_with_very_little_history_gets_warning_not_recent_listing_info(tmp_path):
    def fetch(batch, period):
        days = pd.bdate_range("2021-11-19", "2026-11-19")
        df = pd.DataFrame({"OLD": 1.0, "NEW": 2.0, "KVUE": float("nan")}, index=days)
        df.loc[df.index < "2025-06-02", "NEW"] = float("nan")   # recent listing: plenty of rows
        df.loc[df.index[:5], "KVUE"] = 3.0                      # only 5 rows: stopped trading?
        return df[batch]

    conn = connect(tmp_path / "t.db")
    summary = run_backfill(conn, ["OLD", "NEW", "KVUE"], "5y", 100, 0,
                           fetch=fetch, sleep=FakeSleep(), log=quiet)
    assert summary.very_little_history() == ["KVUE"]
    assert summary.short_history() == ["NEW"]                    # KVUE is not called a recent listing

    out = []
    print_summary(summary, 3, "5y", log=out.append)
    assert "Warning: very little history, may have stopped trading: KVUE (5 rows)" in out
    assert any(line.startswith("Info: 1 ticker has less than the full 5y") for line in out)


def test_very_little_history_counts_all_saved_rows_not_just_this_run(tmp_path):
    conn = connect(tmp_path / "t.db")
    old = pd.DataFrame({"AAA": [1.0] * 40}, index=pd.bdate_range("2026-01-01", periods=40))
    save_prices(conn, old)                                       # 40 rows saved earlier
    summary = run_backfill(conn, ["AAA"], "1mo", 100, 0,
                           fetch=FakeFetch(), sleep=FakeSleep(), log=quiet)  # only 3 new days
    assert summary.saved_rows["AAA"] == 43
    assert summary.very_little_history() == []
