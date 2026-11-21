"""Tools the agent can call.

Each tool reads the saved data (SQLite) and returns plain dicts that can be sent as JSON.
The model never calculates anything: every number it states should come from one of these.

Try them yourself, no AI needed:
    python tools.py get_movers period=week direction=losers n=5
    python tools.py get_ticker_history ticker=INTC days=10
"""
import json
import sys

import pandas as pd

import config
from analysis import find_outliers, top_companies, top_movers
from storage import connect, latest_snapshot, price_series, ticker_history
from universe import load_universe, load_groups

PERIOD_COLUMN = {"day": "day_pct", "week": "week_pct", "month": "month_pct"}


def _num(x, digits=2):
    """Round a number for the model; missing values become None (JSON null)."""
    return None if pd.isna(x) else round(float(x), digits)


def _rows(df, names):
    return [
        {
            "ticker": ticker,
            "name": names.get(ticker, ""),
            "close": _num(r["close"]),
            "day_pct": _num(r["day_pct"]),
            "week_pct": _num(r["week_pct"]),
            "month_pct": _num(r["month_pct"]),
            "market_cap_billions": _num(r["market_cap"] / 1e9, 1),
        }
        for ticker, r in df.iterrows()
    ]


class Toolbox:
    """Holds the data the tools read. One Toolbox = one snapshot of the latest saved day."""

    def __init__(self, conn, names, same_company_groups=(), outlier_threshold=20.0, theme_groups=None):
        self.conn = conn
        self.names = names
        self.groups = same_company_groups
        self.outlier_threshold = outlier_threshold
        self.theme_groups = theme_groups or {}  # {"banks": ["JPM", ...], ...}
        self._theme_lookup = {name.casefold(): name for name in self.theme_groups}
        self.as_of, self.snapshot = latest_snapshot(conn)

    def _need_data(self):
        if self.snapshot.empty:
            raise RuntimeError("No saved data yet. Run main.py first.")

    def get_movers(self, period, direction, n=10):
        self._need_data()
        if period not in PERIOD_COLUMN:
            raise ValueError(f"period must be one of {list(PERIOD_COLUMN)}")
        if direction not in ("gainers", "losers"):
            raise ValueError("direction must be 'gainers' or 'losers'")
        n = max(1, min(int(n), 50))
        gainers, losers = top_movers(self.snapshot, PERIOD_COLUMN[period], n)
        chosen = gainers if direction == "gainers" else losers
        return {
            "as_of": self.as_of,
            "period": period,
            "direction": direction,
            "stocks": _rows(chosen, self.names),
        }

    def get_top_companies(self, n=20):
        self._need_data()
        n = max(1, min(int(n), 100))
        top = top_companies(self.snapshot, n, self.groups)
        return {"as_of": self.as_of, "companies": _rows(top, self.names)}

    def get_ticker_history(self, ticker, days=30):
        days = max(1, min(int(days), 120))
        hist = ticker_history(self.conn, ticker, days)
        if hist.empty:
            raise ValueError(
                f"No saved history for {ticker}. It may not be tracked, or the report has not run yet."
            )
        symbol = ticker.upper()
        rows = [
            {
                "trade_date": r["trade_date"],
                "close": _num(r["close"]),
                "day_pct": _num(r["day_pct"]),
                "week_pct": _num(r["week_pct"]),
                "month_pct": _num(r["month_pct"]),
            }
            for _, r in hist.iterrows()
        ]
        return {
            "ticker": symbol,
            "name": self.names.get(symbol, ""),
            "note": "Only days the report has run are saved. Newest first.",
            "rows": rows,
        }

    def get_price_series(self, ticker, days=63):
        """Daily closes for one ticker, oldest first, for drawing a price line chart.

        Reads the `prices` table (full daily history), not `daily_returns`
        (which only has days the report ran). Not an agent tool yet: the dashboard uses it.
        """
        days = max(1, min(int(days), 252))
        symbol = ticker.strip().upper()
        series = price_series(self.conn, symbol, days)
        if series.empty:
            raise ValueError(f"No saved prices for {symbol}.")
        return {
            "as_of": self.as_of,
            "ticker": symbol,
            "name": self.names.get(symbol, ""),
            "prices": [
                {"date": r["trade_date"], "close": _num(r["close"])} for _, r in series.iterrows()
            ],
        }

    def get_stock(self, ticker):
        self._need_data()
        symbol = ticker.strip().upper()
        if symbol not in self.snapshot.index:
            raise ValueError(f"{symbol} is not in the latest snapshot.")
        row = self.snapshot.loc[[symbol]]
        return {"as_of": self.as_of, "stock": _rows(row, self.names)[0]}


    def _group_stats(self, tickers):
        """Equal-weighted stats for the group's stocks that have data on the latest day."""
        df = self.snapshot.loc[self.snapshot.index.intersection(tickers)]
        stats = {"tickers_listed": len(tickers), "stocks_with_data": len(df)}
        for period, column in PERIOD_COLUMN.items():
            values = df[column].dropna()
            stats[period] = {
                "average_pct": _num(values.mean()),
                "median_pct": _num(values.median()),
                "up": int((values > 0).sum()),
                "down": int((values < 0).sum()),
            }
        return stats, df

    def get_group_summary(self, group=None):
        self._need_data()
        if not self.theme_groups:
            raise RuntimeError("No groups defined. Create groups.csv first.")
        note = "Averages are equal-weighted across the stocks in each group."
        if group is None:  # no group asked for: compare all of them
            return {
                "as_of": self.as_of,
                "note": note,
                "groups": {
                    name: self._group_stats(tickers)[0] for name, tickers in self.theme_groups.items()
                },
            }
        name = self._theme_lookup.get(str(group).strip().casefold())
        if name is None:
            raise ValueError(f"Unknown group '{group}'. Available groups: {sorted(self.theme_groups)}")
        stats, df = self._group_stats(self.theme_groups[name])
        members = df.sort_values("month_pct", ascending=False).head(50)
        return {
            "as_of": self.as_of,
            "group": name,
            "note": note,
            **stats,
            "members_sorted_by_month": _rows(members, self.names),
        }

        

    def get_outliers(self, threshold=None):
        self._need_data()
        limit = self.outlier_threshold if threshold is None else float(threshold)
        out = find_outliers(self.snapshot, "day_pct", limit)
        return {"as_of": self.as_of, "threshold_pct": limit, "stocks": _rows(out, self.names)}

    def call(self, name, args):
        """Run a tool by name. Only the names listed here can be called."""
        tools = {
            "get_movers": self.get_movers,
            "get_top_companies": self.get_top_companies,
            "get_ticker_history": self.get_ticker_history,
            "get_outliers": self.get_outliers,
            "get_stock": self.get_stock,
            "get_group_summary": self.get_group_summary,
        }
        if name not in tools:
            raise ValueError(f"Unknown tool: {name}")
        return tools[name](**args)


def _spec(name, description, properties, required=()):
    """Describe a tool to the model in Amazon Bedrock's Converse format."""
    return {
        "toolSpec": {
            "name": name,
            "description": description,
            "inputSchema": {
                "json": {"type": "object", "properties": properties, "required": list(required)}
            },
        }
    }


TOOL_SPECS = [
    _spec(
        "get_movers",
        "Biggest percentage gainers or losers among the tracked stocks over the last trading "
        "day, the last 5 trading days (week) or the last 21 trading days (month).",
        {
            "period": {"type": "string", "enum": ["day", "week", "month"]},
            "direction": {"type": "string", "enum": ["gainers", "losers"]},
            "n": {"type": "integer", "description": "How many stocks to return, 1 to 50 (default 10)."},
        },
        ["period", "direction"],
    ),
    _spec(
        "get_top_companies",
        "Largest tracked companies by market capitalization, with day, week and month returns.",
        {"n": {"type": "integer", "description": "How many companies, 1 to 100 (default 20)."}},
    ),
    _spec(
        "get_ticker_history",
        "Saved daily history (close and returns) for one ticker, newest first.",
        {
            "ticker": {"type": "string", "description": "Ticker symbol, for example INTC."},
            "days": {"type": "integer", "description": "How many saved days, 1 to 120 (default 30)."},
        },
        ["ticker"],
    ),
    _spec(
        "get_outliers",
        "Stocks whose one-day move was unusually large (up or down), which may be real news "
        "or bad data.",
        {"threshold": {"type": "number", "description": "Minimum absolute one-day percent move."}},
    ),
    _spec(
        "get_stock",
        "Latest close and day, week and month returns for one stock.",
        {"ticker": {"type": "string", "description": "Ticker symbol, for example AAPL."}},
        ["ticker"],
    ),
    _spec(
        "get_group_summary",
        "Performance of a themed group of stocks (for example ai_stocks, semiconductors or banks): "
        "average and median return, how many are up or down, and the members. "
        "Leave 'group' out to compare every group at once.",
        {"group": {"type": "string", "description": "Group name. Omit to compare all groups."}},
    ),
    
]


def _parse(value):
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            pass
    return value


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: python tools.py TOOL_NAME key=value ...")
    universe = load_universe(config.TICKERS_CSV)
    names = dict(zip(universe["ticker"], universe["name"]))
    toolbox = Toolbox(
        connect(config.DB_PATH), names, config.SAME_COMPANY_GROUPS, config.OUTLIER_DAY_PCT,
        theme_groups=load_groups(config.GROUPS_CSV),
    )
    args = {k: _parse(v) for k, v in (a.split("=", 1) for a in sys.argv[2:])}
    print(json.dumps(toolbox.call(sys.argv[1], args), indent=2))


if __name__ == "__main__":
    main()