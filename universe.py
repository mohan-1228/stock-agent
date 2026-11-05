"""Loads the list of stocks to track from tickers.csv."""
import pandas as pd
from pathlib import Path


def load_universe(path):
    """Return a DataFrame with columns ticker, name (deduplicated, upper-case tickers)."""
    df = pd.read_csv(path)
    df["ticker"] = df["ticker"].str.strip().str.upper()
    return df.drop_duplicates("ticker").reset_index(drop=True)


def load_groups(path):
    """Read groups.csv (columns: group,tickers) into {group_name: [tickers]}.

    One row per group, with the tickers separated by spaces. A missing file means no groups.
    """
    path = Path(path)
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    groups = {}
    for _, row in df.iterrows():
        name = str(row["group"]).strip().lower()
        groups[name] = sorted(set(str(row["tickers"]).upper().split()))
    return groups
