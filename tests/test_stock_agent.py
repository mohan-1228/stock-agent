import datetime

import numpy as np
import pandas as pd

from analysis import compute_returns, top_companies, top_movers
from report import build_report
from storage import connect, save_snapshot, ticker_history


def test_day_return_known_values():
    idx = pd.bdate_range("2026-10-26", periods=3)
    closes = pd.DataFrame({"A": [100.0, 110.0, 121.0]}, index=idx)
    r = compute_returns(closes)
    assert r.loc["A", "close"] == 121.0
    assert abs(r.loc["A", "day_pct"] - 10.0) < 1e-9
    # only 3 rows of history: week and month cannot be computed
    assert np.isnan(r.loc["A", "week_pct"])
    assert np.isnan(r.loc["A", "month_pct"])


def test_week_and_month_windows():
    idx = pd.bdate_range("2026-09-01", periods=30)
    closes = pd.DataFrame({"A": 100.0 + np.arange(30)}, index=idx)  # 100 ... 129
    r = compute_returns(closes)
    # week base = 5 rows back (124), month base = 21 rows back (108)
    assert abs(r.loc["A", "week_pct"] - (129 / 124 - 1) * 100) < 1e-9
    assert abs(r.loc["A", "month_pct"] - (129 / 108 - 1) * 100) < 1e-9


def test_top_movers_order_and_nan_ignored():
    returns = pd.DataFrame(
        {"close": [1, 1, 1, 1, 1], "day_pct": [5.0, -3.0, 12.0, np.nan, 0.5]},
        index=["A", "B", "C", "D", "E"],
    )
    gainers, losers = top_movers(returns, "day_pct", n=2)
    assert list(gainers.index) == ["C", "A"]
    assert list(losers.index) == ["B", "E"]


def test_top_companies_sorted_and_missing_cap_dropped():
    returns = pd.DataFrame(
        {"market_cap": [1e12, 3e12, np.nan, 2e12]}, index=["A", "B", "C", "D"]
    )
    assert list(top_companies(returns, n=3).index) == ["B", "D", "A"]


def _sample_returns():
    return pd.DataFrame(
        {
            "close": [100.0, 50.0, 20.0],
            "day_pct": [2.0, -1.5, 0.0],
            "week_pct": [4.0, -3.0, 1.0],
            "month_pct": [10.0, -8.0, 2.0],
            "market_cap": [3e12, 5e11, 4e10],
        },
        index=pd.Index(["AAA", "BBB", "CCC"], name="ticker"),
    )


def test_storage_roundtrip_and_overwrite(tmp_path):
    conn = connect(tmp_path / "t.db")
    day = datetime.date(2026, 10, 30)
    save_snapshot(conn, day, _sample_returns())
    save_snapshot(conn, day, _sample_returns())  # same day again must not duplicate
    hist = ticker_history(conn, "aaa")
    assert len(hist) == 1
    assert hist.loc[0, "day_pct"] == 2.0


def test_report_contains_expected_sections():
    returns = _sample_returns()
    top = top_companies(returns, n=3)
    names = {"AAA": "Alpha", "BBB": "Beta", "CCC": "Gamma"}
    text = build_report(datetime.date(2026, 10, 30), returns, top, names, n_movers=2)
    assert "Friday, October 30, 2026" in text
    assert "Top 2 gainers" in text and "Top 2 losers" in text
    assert "This week" in text and "This month" in text
    assert "Top 3 companies by market cap" in text
    assert "$3.00T" in text and "+2.00%" in text
