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


def top_companies(returns: pd.DataFrame, n: int = 50) -> pd.DataFrame:
    """The n largest companies by market cap (needs a market_cap column)."""
    valid = returns[returns["market_cap"].notna()]
    return valid.sort_values("market_cap", ascending=False).head(n)
