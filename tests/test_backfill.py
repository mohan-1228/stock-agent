"""Tests for the 5-year history backfill. No network: fetching and sleeping are faked."""
import pandas as pd
import pytest

from data import fetch_closes_in_batches, split_into_batches

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
