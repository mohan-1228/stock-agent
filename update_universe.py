"""Refresh tickers.csv: the S&P 500 (from Wikipedia) plus your hand-picked extras.

    python update_universe.py --dry-run   # show what would change, write nothing
    python update_universe.py             # rewrite tickers.csv

Put companies you always want tracked (TSMC, other AI names, anything outside
the S&P 500) in extra_tickers.csv. They survive every refresh.
"""
import argparse
import sys
from io import StringIO

import pandas as pd

import config

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
USER_AGENT = "stock-agent/1.0 (personal learning project)"
MIN_EXPECTED = 400  # the index has about 500 rows; far fewer means the page layout changed


def normalize_symbol(symbol) -> str:
    """Wikipedia writes BRK.B; Yahoo Finance wants BRK-B."""
    return str(symbol).strip().upper().replace(".", "-")


def parse_sp500_html(html: str) -> pd.DataFrame:
    """Pull ticker and company name out of the page's constituents table."""
    table = pd.read_html(StringIO(html), attrs={"id": "constituents"})[0]
    return pd.DataFrame(
        {
            "ticker": table["Symbol"].map(normalize_symbol),
            "name": table["Security"].astype(str).str.strip(),
        }
    )


def fetch_sp500() -> pd.DataFrame:
    import requests

    resp = requests.get(WIKI_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return parse_sp500_html(resp.text)


def merge_universe(sp500: pd.DataFrame, extras: pd.DataFrame) -> pd.DataFrame:
    """S&P 500 rows first, then extras not already present. One row per ticker."""
    combined = pd.concat([sp500, extras], ignore_index=True)
    combined["ticker"] = combined["ticker"].str.strip().str.upper()
    return combined.drop_duplicates("ticker", keep="first").reset_index(drop=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="show changes but do not write tickers.csv")
    args = parser.parse_args()

    try:
        sp500 = fetch_sp500()
    except Exception as exc:  # network down, page changed, etc.
        sys.exit(f"Could not get the S&P 500 list ({exc}). tickers.csv left untouched.")
    if len(sp500) < MIN_EXPECTED:
        sys.exit(
            f"Only {len(sp500)} S&P 500 rows found (expected about 500). "
            "The page layout may have changed. tickers.csv left untouched."
        )

    if config.EXTRA_TICKERS_CSV.exists():
        extras = pd.read_csv(config.EXTRA_TICKERS_CSV)
    else:
        extras = pd.DataFrame(columns=["ticker", "name"])
    merged = merge_universe(sp500, extras)

    old = set(pd.read_csv(config.TICKERS_CSV)["ticker"]) if config.TICKERS_CSV.exists() else set()
    new = set(merged["ticker"])
    added, removed = sorted(new - old), sorted(old - new)

    print(f"S&P 500 rows: {len(sp500)} | extras: {len(extras)} | total tickers: {len(merged)}")
    print(f"Added ({len(added)}): {', '.join(added) or 'none'}")
    print(f"Removed ({len(removed)}): {', '.join(removed) or 'none'}")
    if removed:
        print("Anything in 'Removed' you still want? Add it to extra_tickers.csv and run again.")

    if args.dry_run:
        print("Dry run: tickers.csv not changed.")
        return
    merged.to_csv(config.TICKERS_CSV, index=False)
    print(f"Wrote {config.TICKERS_CSV}")


if __name__ == "__main__":
    main()