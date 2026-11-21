import pandas as pd
import pytest

from storage import connect, price_series, save_prices


def make_closes():
    days = pd.to_datetime(["2026-11-17", "2026-11-18", "2026-11-19"])
    return pd.DataFrame({"AAA": [10.0, 11.0, 12.0], "BBB": [5.0, None, 4.0]}, index=days)


def test_save_prices_and_read_series_oldest_first(tmp_path):
    conn = connect(tmp_path / "t.db")
    assert save_prices(conn, make_closes()) == 5  # BBB's missing day is skipped
    series = price_series(conn, "aaa", 30)
    assert list(series["trade_date"]) == ["2026-11-17", "2026-11-18", "2026-11-19"]
    assert list(series["close"]) == [10.0, 11.0, 12.0]


def test_price_series_limits_to_most_recent_days(tmp_path):
    conn = connect(tmp_path / "t.db")
    save_prices(conn, make_closes())
    assert list(price_series(conn, "AAA", 2)["close"]) == [11.0, 12.0]


def test_saving_prices_twice_does_not_duplicate(tmp_path):
    conn = connect(tmp_path / "t.db")
    save_prices(conn, make_closes())
    save_prices(conn, make_closes())
    assert len(price_series(conn, "AAA", 30)) == 3
    assert price_series(conn, "ZZZ", 30).empty

def test_toolbox_price_series_shape_and_unknown_ticker(tmp_path):
    from tools import Toolbox

    conn = connect(tmp_path / "t.db")
    save_prices(conn, make_closes())
    toolbox = Toolbox(conn, {"AAA": "Alpha Inc"})
    result = toolbox.get_price_series(" aaa ", 2)
    assert result["ticker"] == "AAA"
    assert result["name"] == "Alpha Inc"
    assert result["prices"] == [{"date": "2026-11-18", "close": 11.0}, {"date": "2026-11-19", "close": 12.0}]
    with pytest.raises(ValueError, match="ZZZ"):
        toolbox.get_price_series("ZZZ")


# --- Range change (milestone 4b) ---------------------------------------------

def make_history(n_days, start=100.0):
    """n_days of made-up closes on business days, wiggling up and down (no network)."""
    days = pd.bdate_range("2026-06-01", periods=n_days)
    values = [start + (i % 7) * 1.5 - (i % 3) * 0.8 + i * 0.1 for i in range(n_days)]
    return pd.DataFrame({"AAA": values}, index=days)


def test_one_month_change_equals_month_pct_from_same_data(tmp_path):
    from analysis import compute_returns
    from storage import save_snapshot
    from tools import Toolbox, range_change

    closes = make_history(70)
    returns = compute_returns(closes)
    conn = connect(tmp_path / "t.db")
    save_snapshot(conn, str(closes.index[-1].date()), returns)
    save_prices(conn, closes)
    toolbox = Toolbox(conn, {"AAA": "Alpha Inc"})

    result = toolbox.get_price_series("AAA", 63)
    one_month = result["ranges"]["1M"]
    assert one_month["closes"] == 22 and one_month["trading_days"] == 21
    # Same window as month_pct: from the close 21 trading days ago to the latest close.
    assert one_month["start_date"] == str(closes.index[-22].date())
    assert one_month["end_date"] == str(closes.index[-1].date())
    assert one_month["change_pct"] == toolbox.get_stock("AAA")["stock"]["month_pct"]
    # And at full precision, before any rounding.
    assert range_change(closes["AAA"].tail(22)) == pytest.approx(returns.loc["AAA", "month_pct"])


def test_less_history_than_range_gives_null_not_a_number(tmp_path):
    from tools import Toolbox

    conn = connect(tmp_path / "t.db")
    save_prices(conn, make_history(30))
    ranges = Toolbox(conn, {}).get_price_series("AAA", 63)["ranges"]
    assert ranges["1M"]["change_pct"] is not None
    assert ranges["3M"] == {
        "trading_days": 63, "closes": 64, "start_date": None, "end_date": None, "change_pct": None,
    }


def test_ranges_do_not_depend_on_days_and_old_fields_stay(tmp_path):
    from tools import Toolbox

    conn = connect(tmp_path / "t.db")
    save_prices(conn, make_history(70))
    toolbox = Toolbox(conn, {})
    small, large = toolbox.get_price_series("AAA", 5), toolbox.get_price_series("AAA", 63)
    assert len(small["prices"]) == 5 and len(large["prices"]) == 63
    assert small["ranges"] == large["ranges"]
    assert large["ranges"]["3M"]["change_pct"] is not None
    assert {"as_of", "ticker", "name", "prices"} <= set(small)


@pytest.mark.parametrize(
    "closes, expected",
    [
        ([10.0, 10.0, 10.0], 0.0),               # flat
        ([10.0, 11.0], pytest.approx(10.0)),     # +10%
        ([10.0], None),                          # one close: no change to measure
        ([], None),                              # nothing saved
        ([0.0, 5.0], None),                      # would divide by zero
        ([-1.0, 5.0], None),                     # nonsense price
        ([float("nan"), 5.0], None),             # missing first close
        ([5.0, None], None),                     # missing last close
    ],
)
def test_range_change_edge_cases(closes, expected):
    from tools import range_change

    assert range_change(closes) == expected


def test_toolbox_tiny_series_and_zero_close_do_not_crash(tmp_path):
    from tools import Toolbox

    conn = connect(tmp_path / "t.db")
    days = pd.bdate_range("2026-06-01", periods=22)
    save_prices(conn, pd.DataFrame({"ZER": [0.0] + [5.0] * 21, "ONE": [7.0] + [None] * 21}, index=days))
    toolbox = Toolbox(conn, {})
    assert toolbox.get_price_series("ZER")["ranges"]["1M"]["change_pct"] is None
    one = toolbox.get_price_series("ONE")
    assert len(one["prices"]) == 1
    assert one["ranges"]["1M"]["change_pct"] is None
