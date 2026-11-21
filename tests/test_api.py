"""API tests on a small made-up database. No network, no real model, no data/stocks.db."""
import logging
import sqlite3

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import api
import config
from storage import connect, save_prices, save_snapshot
from tools import Toolbox

NAMES = {"AAA": "Alpha Inc", "BBB": "Beta Bank", "CCC": "Gamma Corp", "DDD": "Delta Ltd"}
GROUPS = {"tech": ["AAA", "CCC", "ZZZ"], "banks": ["BBB"]}  # ZZZ is listed but has no data


def _snapshot():
    return pd.DataFrame(
        {
            "close": [100.0, 50.0, 20.0, 10.0],
            "day_pct": [2.0, -1.5, 25.0, -4.0],  # CCC moved 25%: an outlier
            "week_pct": [4.0, -3.0, 1.0, np.nan],
            "month_pct": [10.0, -8.0, 2.0, -1.0],
            "market_cap": [3e12, 5e11, 4e10, np.nan],  # DDD has no market cap
        },
        index=pd.Index(["AAA", "BBB", "CCC", "DDD"], name="ticker"),
    )


def _closes():
    days = pd.to_datetime(["2026-11-18", "2026-11-19", "2026-11-20"])
    return pd.DataFrame({"AAA": [98.0, 99.0, 100.0]}, index=days)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Point the API at an empty temporary database with our fake names and groups."""
    path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(api, "NAMES", NAMES)
    monkeypatch.setattr(api, "GROUPS", GROUPS)
    return path


@pytest.fixture
def client(db_path):
    """A test client whose database holds one saved day and a few prices."""
    conn = connect(db_path)
    save_snapshot(conn, "2026-11-20", _snapshot())
    save_prices(conn, _closes())
    conn.close()
    return TestClient(api.app)


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_summary_counts_stocks_and_outliers(client):
    assert client.get("/api/summary").json() == {"as_of": "2026-11-20", "stocks": 4, "outliers": 1}


def test_movers_losers_sorted_and_limited(client):
    body = client.get("/api/movers?period=day&direction=losers&n=2").json()
    assert body["as_of"] == "2026-11-20"
    assert [s["ticker"] for s in body["stocks"]] == ["DDD", "BBB"]
    assert body["stocks"][0]["name"] == "Delta Ltd"


@pytest.mark.parametrize(
    "query",
    [
        "period=year&direction=gainers",  # unknown period
        "period=day&direction=up",  # unknown direction
        "direction=gainers",  # period missing
        "period=day&direction=gainers&n=0",  # n too small
        "period=day&direction=gainers&n=51",  # n too big
        "period=day&direction=gainers&n=abc",  # n not a number
    ],
)
def test_movers_rejects_bad_input(client, query):
    assert client.get(f"/api/movers?{query}").status_code == 422


def test_companies_sorted_by_market_cap(client):
    body = client.get("/api/companies?n=10").json()
    assert [c["ticker"] for c in body["companies"]] == ["AAA", "BBB", "CCC"]  # DDD: no cap
    assert body["companies"][0]["market_cap_billions"] == 3000.0


def test_companies_rejects_n_over_100(client):
    assert client.get("/api/companies?n=101").status_code == 422


def test_stock_has_groups_and_outlier_flag(client):
    stock = client.get("/api/stock/ccc").json()["stock"]  # lowercase works
    assert stock["ticker"] == "CCC"
    assert stock["day_pct"] == 25.0
    assert stock["groups"] == ["tech"]
    assert stock["is_outlier"] is True


def test_stock_not_in_any_group_and_not_outlier(client):
    stock = client.get("/api/stock/DDD").json()["stock"]
    assert stock["groups"] == []
    assert stock["is_outlier"] is False
    assert stock["week_pct"] is None  # missing value becomes null, not NaN


def test_unknown_stock_is_404(client):
    response = client.get("/api/stock/ZZZ")
    assert response.status_code == 404
    assert "ZZZ" in response.json()["detail"]


def test_ticker_with_odd_characters_is_422(client):
    assert client.get("/api/stock/A$B").status_code == 422
    assert client.get("/api/stock/TOOLONGTICKER").status_code == 422


def test_prices_oldest_first_and_limited(client):
    body = client.get("/api/stock/AAA/prices?days=2").json()
    assert body["ticker"] == "AAA"
    assert body["prices"] == [
        {"date": "2026-11-19", "close": 99.0},
        {"date": "2026-11-20", "close": 100.0},
    ]


def test_prices_unknown_ticker_404_and_bad_days_422(client):
    assert client.get("/api/stock/BBB/prices").status_code == 404  # no prices saved for BBB
    assert client.get("/api/stock/AAA/prices?days=0").status_code == 422
    assert client.get("/api/stock/AAA/prices?days=253").status_code == 422


def test_groups_all_and_one(client):
    all_groups = client.get("/api/groups").json()["groups"]
    assert set(all_groups) == {"tech", "banks"}
    assert all_groups["tech"]["stocks_with_data"] == 2  # ZZZ has no data
    one = client.get("/api/groups/TECH").json()  # case does not matter
    assert one["group"] == "tech"
    assert one["day"]["average_pct"] == 13.5  # (2 + 25) / 2


def test_unknown_group_is_404(client):
    assert client.get("/api/groups/space").status_code == 404


def test_outliers(client):
    body = client.get("/api/outliers").json()
    assert body["threshold_pct"] == config.OUTLIER_DAY_PCT
    assert [s["ticker"] for s in body["stocks"]] == ["CCC"]


def test_empty_database_is_503(db_path):
    client = TestClient(api.app)
    response = client.get("/api/summary")
    assert response.status_code == 503
    assert "main.py" in response.json()["detail"]
    assert client.get("/api/movers?period=day&direction=gainers").status_code == 503
    assert client.get("/api/health").status_code == 200  # health never needs data


def test_unexpected_error_is_logged_but_hidden(client, monkeypatch, caplog):
    def boom(self, threshold=None):
        raise KeyError("secret /Users/someone/path")

    monkeypatch.setattr(Toolbox, "get_outliers", boom)
    with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
        response = client.get("/api/outliers")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal error."}  # nothing else leaks
    assert "secret" in caplog.text  # but the real error is on the server console


def test_connection_is_closed_after_each_request(client, monkeypatch):
    opened = []

    def recording_connect(path, check_same_thread=True):
        conn = connect(path, check_same_thread)
        opened.append(conn)
        return conn

    monkeypatch.setattr(api, "connect", recording_connect)
    client.get("/api/summary")
    client.get("/api/stock/ZZZ")  # an error response must close it too
    assert len(opened) == 2
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):  # "Cannot operate on a closed database"
            conn.execute("SELECT 1")
