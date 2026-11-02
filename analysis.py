"""Pure calculations: no network, no files. Easy to test."""
import pandas as pd

# Rolling windows measured in trading days.
PERIODS = {"day": 1, "week": 5, "month": 21}


def compute_returns(closes: pd.DataFrame) -> pd.DataFrame:
    """Percent change over 1, 5 and 21 trading days, ending at the latest row.

    Returns one row per ticker with columns: close, day_pct, week_pct, month_pct.
    A period is NaN when there isn't enough history for that ticker.
    """
    closes = closes.sort_index()
    latest = closes.iloc[-1]
    out = pd.DataFrame({"close": latest})
    for name, n in PERIODS.items():
        if len(closes) > n:
            base = closes.iloc[-1 - n]
            out[f"{name}_pct"] = (latest / base - 1.0) * 100.0
        else:
            out[f"{name}_pct"] = float("nan")
    out.index.name = "ticker"
    return out


def top_movers(returns: pd.DataFrame, column: str, n: int = 10):
    """(gainers, losers) for one return column, ignoring tickers with no value."""
    valid = returns[returns[column].notna()]
    return valid.nlargest(n, column), valid.nsmallest(n, column)


def top_companies(returns: pd.DataFrame, n: int = 50, same_company_groups=()) -> pd.DataFrame:
    """The n largest companies by market cap (needs a market_cap column).

    same_company_groups: tuples of tickers that are share classes of one company,
    e.g. [("GOOGL", "GOOG")]. Only the largest ticker in each group is kept.
    """
    valid = returns[returns["market_cap"].notna()]
    ranked = valid.sort_values("market_cap", ascending=False)  # index is now in rank order
    to_drop = []
    for group in same_company_groups:
        present = [t for t in ranked.index if t in group]      # tickers from this group, biggest first
        to_drop += present[1:]                                 # keep the first, drop the rest
    return ranked.drop(index=to_drop).head(n)


def find_outliers(returns: pd.DataFrame, column: str = "day_pct", threshold: float = 20.0):
    """Rows that moved at least `threshold` percent in either direction, biggest first.

    Huge moves are sometimes real (earnings, news) and sometimes bad data,
    so the report flags them for a human to double-check.
    """
    valid = returns[returns[column].notna()]                      # drop tickers with no value
    flagged = valid[valid[column].abs() >= threshold]             # up OR down, threshold included
    return flagged.loc[flagged[column].abs().sort_values(ascending=False).index]  # biggest first


