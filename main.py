"""Daily stock report. Schedule this for 6 pm Central on weekdays.

It skips market holidays by itself: if there is no price bar dated today,
the market was closed, so it saves no prices. It still collects news then
(Fed and SEC publish on holidays too). News is extra: if it fails, prices and
the report are not affected.
"""
import argparse
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from analysis import compute_returns, find_outliers, top_companies
from data import fetch_closes, fetch_market_caps
from news import exit_if_threads_stuck, format_summary, refresh_news
from report import build_report
from storage import connect, save_snapshot, save_prices
from universe import load_universe


def run_news_step():
    """Collect headlines for the latest saved day. Never raises: news must not break the daily run."""
    print("\nCollecting news...", flush=True)
    try:
        conn = connect(config.DB_PATH)
        try:
            summary = refresh_news(conn)
        finally:
            conn.close()
        print(format_summary(summary))
    except Exception as exc:
        print(f"News step failed ({type(exc).__name__}: {exc}). Prices and the report are not affected.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="run on the latest available trading day even if today has no data",
    )
    args = parser.parse_args()
    config.load_env_file()  # settings like SEC_USER_AGENT; launchd gives us no environment

    today = datetime.now(ZoneInfo(config.TIMEZONE)).date()
    universe = load_universe(config.TICKERS_CSV)
    tickers = universe["ticker"].tolist()
    names = dict(zip(universe["ticker"], universe["name"]))

    closes = fetch_closes(tickers, config.HISTORY_PERIOD)
    if closes.empty:
        sys.exit("No price data returned. Check your internet connection.")

    last_day = closes.index[-1].date()
    if last_day != today and not args.force:
        print(f"No market data for {today} (latest is {last_day}); market closed today. Skipping.")
        run_news_step()
        return

    returns = compute_returns(closes).join(fetch_market_caps(tickers, config.MARKET_CAP_WORKERS))
    top50 = top_companies(returns, config.TOP_N_COMPANIES, config.SAME_COMPANY_GROUPS)
    outliers = find_outliers(returns, "day_pct", config.OUTLIER_DAY_PCT)
    report = build_report(
        last_day, returns, top50, names, config.TOP_N_MOVERS,
        outliers=outliers, outlier_threshold=config.OUTLIER_DAY_PCT,
    )

    conn = connect(config.DB_PATH)
    save_snapshot(conn, last_day, returns)
    save_prices(conn, closes)  # keeps the daily closes so the dashboard can draw price charts
    conn.close()

    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = config.REPORTS_DIR / f"{last_day}.md"
    out_path.write_text(report, encoding="utf-8")

    print(report)
    print(f"\nSaved report to {out_path} and history to {config.DB_PATH}")
    run_news_step()


if __name__ == "__main__":
    main()
    exit_if_threads_stuck()  # a news request that never answered must not keep the process alive
