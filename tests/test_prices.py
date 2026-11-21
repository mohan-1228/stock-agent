import pandas as pd

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