"""Turns the numbers into a readable Markdown report."""
import pandas as pd

from analysis import top_movers

PERIOD_SECTIONS = [
    ("Today", "day_pct"),
    ("This week (last 5 trading days)", "week_pct"),
    ("This month (last 21 trading days)", "month_pct"),
]


def _pct(x):
    return "n/a" if pd.isna(x) else f"{x:+.2f}%"


def _money(x):
    return f"${x:,.2f}"


def _cap(x):
    if pd.isna(x):
        return "n/a"
    if x >= 1e12:
        return f"${x / 1e12:.2f}T"
    return f"${x / 1e9:,.1f}B"


def _table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)

def _label(ticker, flagged):
    """Ticker with a * when it is flagged for verification."""
    return f"{ticker}*" if ticker in flagged else ticker


def _movers_table(df, names, column, flagged=()):
    rows = [
        (i, _label(t, flagged), names.get(t, ""), _money(r["close"]), _pct(r[column]))
        for i, (t, r) in enumerate(df.iterrows(), start=1)
    ]
    return _table(["#", "Ticker", "Company", "Close", "Return"], rows)


def _top_table(df, names, flagged=()):
    rows = [
        (
            i, _label(t, flagged), names.get(t, ""), _cap(r["market_cap"]), _money(r["close"]),
            _pct(r["day_pct"]), _pct(r["week_pct"]), _pct(r["month_pct"]),
        )
        for i, (t, r) in enumerate(df.iterrows(), start=1)
    ]
    return _table(["#", "Ticker", "Company", "Mkt cap", "Close", "Day", "Week", "Month"], rows)


def build_report(
    trade_date, returns, top_companies_df, names, n_movers=10,
    outliers=None, outlier_threshold=20.0,
) -> str:
    covered = int(returns["close"].notna().sum())
    missing = sorted(returns.index[returns["close"].isna()])

    lines = [
        f"# Daily Stock Report: {trade_date:%A, %B %d, %Y}",
        "",
        f"Tracking {len(returns)} stocks ({covered} with data). "
        "Week and month are rolling windows of 5 and 21 trading days, ending today.",
    ]
    if missing:
        lines.append(f"No data today for: {', '.join(missing)}.")
    lines.append("")

    flagged = set()
    if outliers is not None and not outliers.empty:
        flagged = set(outliers.index)
        lines += [
            "## Verify these moves",
            "",
            f"These stocks moved {outlier_threshold:g}% or more in one day. Moves this large are "
            "sometimes real (earnings, major news) and sometimes bad data. Check each one on a "
            "second source before trusting it. They are marked with * in the tables below.",
            "",
            _movers_table(outliers, names, "day_pct"),
            "",
        ]

    for label, column in PERIOD_SECTIONS:
        gainers, losers = top_movers(returns, column, n_movers)
        lines += [
            f"## {label}",
            "",
            f"### Top {n_movers} gainers",
            "",
            _movers_table(gainers, names, column, flagged),
            "",
            f"### Top {n_movers} losers",
            "",
            _movers_table(losers, names, column, flagged),
            "",
        ]

    lines += [
        f"## Top {len(top_companies_df)} companies by market cap",
        "",
        _top_table(top_companies_df, names, flagged),
        "",
    ]
    return "\n".join(lines)



































# def _movers_table(df, names, column):
#     rows = [
#         (i, t, names.get(t, ""), _money(r["close"]), _pct(r[column]))
#         for i, (t, r) in enumerate(df.iterrows(), start=1)
#     ]
#     return _table(["#", "Ticker", "Company", "Close", "Return"], rows)


# def _top_table(df, names):
#     rows = [
#         (
#             i, t, names.get(t, ""), _cap(r["market_cap"]), _money(r["close"]),
#             _pct(r["day_pct"]), _pct(r["week_pct"]), _pct(r["month_pct"]),
#         )
#         for i, (t, r) in enumerate(df.iterrows(), start=1)
#     ]
#     return _table(["#", "Ticker", "Company", "Mkt cap", "Close", "Day", "Week", "Month"], rows)


# def build_report(trade_date, returns, top_companies_df, names, n_movers=10) -> str:
#     covered = int(returns["close"].notna().sum())
#     missing = sorted(returns.index[returns["close"].isna()])

#     lines = [
#         f"# Daily Stock Report: {trade_date:%A, %B %d, %Y}",
#         "",
#         f"Tracking {len(returns)} stocks ({covered} with data). "
#         "Week and month are rolling windows of 5 and 21 trading days, ending today.",
#     ]
#     if missing:
#         lines.append(f"No data today for: {', '.join(missing)}.")
#     lines.append("")

#     for label, column in PERIOD_SECTIONS:
#         gainers, losers = top_movers(returns, column, n_movers)
#         lines += [
#             f"## {label}",
#             "",
#             f"### Top {n_movers} gainers",
#             "",
#             _movers_table(gainers, names, column),
#             "",
#             f"### Top {n_movers} losers",
#             "",
#             _movers_table(losers, names, column),
#             "",
#         ]

#     lines += [
#         f"## Top {len(top_companies_df)} companies by market cap",
#         "",
#         _top_table(top_companies_df, names),
#         "",
#     ]
#     return "\n".join(lines)
