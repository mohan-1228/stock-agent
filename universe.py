"""Loads the list of stocks to track from tickers.csv."""
import pandas as pd


def load_universe(path):
    """Return a DataFrame with columns ticker, name (deduplicated, upper-case tickers)."""
    df = pd.read_csv(path)
    df["ticker"] = df["ticker"].str.strip().str.upper()
    return df.drop_duplicates("ticker").reset_index(drop=True)
