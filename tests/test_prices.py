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


# --- Index on prices(ticker, trade_date) --------------------------------------

def test_new_database_has_ticker_date_index(tmp_path):
    conn = connect(tmp_path / "t.db")
    names = [row[1] for row in conn.execute("PRAGMA index_list('prices')")]
    assert "idx_prices_ticker_date" in names


def test_old_database_gets_index_when_opened(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)  # a database made before the index existed
    old.execute("CREATE TABLE prices (trade_date TEXT NOT NULL, ticker TEXT NOT NULL, "
                "close REAL NOT NULL, PRIMARY KEY (trade_date, ticker))")
    old.execute("INSERT INTO prices VALUES ('2026-11-19', 'AAA', 12.0)")
    old.commit()
    old.close()

    conn = connect(path)
    names = [row[1] for row in conn.execute("PRAGMA index_list('prices')")]
    assert "idx_prices_ticker_date" in names
    assert list(price_series(conn, "AAA")["close"]) == [12.0]  # existing rows untouched


def test_price_series_query_uses_the_index(tmp_path):
    from storage import PRICE_SERIES_SQL

    conn = connect(tmp_path / "t.db")
    save_prices(conn, make_closes())
    plan = conn.execute("EXPLAIN QUERY PLAN " + PRICE_SERIES_SQL, ("AAA", 63)).fetchall()
    details = " ".join(row[3] for row in plan)
    # Check only the index name: the surrounding wording of the plan differs between
    # SQLite versions (e.g. "USING INDEX" vs "USING COVERING INDEX"), and CI's Linux
    # Python may bundle a different SQLite than the Mac.
    assert "idx_prices_ticker_date" in details


# --- Longer ranges and thinning (milestone 4f) --------------------------------

def test_range_definitions_are_trading_days_and_closes_are_one_more():
    from tools import MAX_PRICE_DAYS, RANGES

    assert RANGES == {"1M": 21, "3M": 63, "6M": 126, "1Y": 252, "5Y": 1260}
    assert MAX_PRICE_DAYS == 1261  # 5Y needs 1260 moves = 1261 closes


@pytest.mark.parametrize("label, closes", [("6M", 127), ("1Y", 253), ("5Y", 1261)])
def test_long_ranges_use_the_right_window(tmp_path, label, closes):
    from tools import Toolbox, range_change

    history = make_history(1300)
    conn = connect(tmp_path / "t.db")
    save_prices(conn, history)
    window = Toolbox(conn, {}).get_price_series("AAA", 63)["ranges"][label]
    assert window["closes"] == closes
    assert window["start_date"] == str(history.index[-closes].date())
    assert window["end_date"] == str(history.index[-1].date())
    expected = range_change(history["AAA"].tail(closes))
    assert window["change_pct"] == round(expected, 2)


def test_every_range_is_null_when_history_is_one_close_short(tmp_path):
    from tools import RANGES, Toolbox

    conn = connect(tmp_path / "t.db")
    save_prices(conn, make_history(1260))  # 5Y needs 1261
    ranges = Toolbox(conn, {}).get_price_series("AAA", 63)["ranges"]
    assert set(ranges) == set(RANGES)
    assert ranges["5Y"]["change_pct"] is None and ranges["5Y"]["start_date"] is None
    assert all(ranges[label]["change_pct"] is not None for label in ("1M", "3M", "6M", "1Y"))


def test_days_is_capped_at_1261(tmp_path):
    from tools import Toolbox

    conn = connect(tmp_path / "t.db")
    save_prices(conn, make_history(1300))
    result = Toolbox(conn, {}).get_price_series("AAA", 5000)
    assert len(result["prices"]) == 1261 and result["days_covered"] == 1261
    assert result["step"] == 1


@pytest.mark.parametrize("n, step, expected", [
    (11, 5, [0, 5, 10]),          # every 5th back from the last lands on the first too
    (10, 5, [0, 4, 9]),
    (5, 5, [0, 4]),
    (1, 5, [0]),
    (4, 1, [0, 1, 2, 3]),             # step 1 = unchanged
])
def test_thin_series_keeps_first_and_last(n, step, expected):
    from tools import thin_series

    df = pd.DataFrame({"close": range(n)})
    assert list(thin_series(df, step)["close"]) == expected


def test_thinned_prices_keep_ranges_from_daily_closes(tmp_path):
    from tools import Toolbox

    history = make_history(1300)
    conn = connect(tmp_path / "t.db")
    save_prices(conn, history)
    toolbox = Toolbox(conn, {})
    daily = toolbox.get_price_series("AAA", 1261)
    weekly = toolbox.get_price_series("AAA", 1261, step=5)
    assert weekly["ranges"] == daily["ranges"]          # thinning never touches the numbers
    assert weekly["step"] == 5 and weekly["days_covered"] == 1261
    assert len(weekly["prices"]) == 253                 # 1261 closes -> every 5th from the end
    assert weekly["prices"][0] == daily["prices"][0]    # starts on the 5Y start close
    assert weekly["prices"][-1] == daily["prices"][-1]  # ends on the latest close
    assert weekly["prices"][0]["date"] == daily["ranges"]["5Y"]["start_date"]
