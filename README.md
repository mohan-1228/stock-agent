# Stock Mover Tracker

Builds a daily report after the US market closes:

- Top 10 gainers and losers for **today**, **this week** (last 5 trading days) and **this month** (last 21 trading days)
- The **top 50 companies by market cap**, with their day / week / month returns
- A SQLite history of every day's numbers, so you can track any stock over time

"Best return" = the biggest percent change in the window, so the gainers tables are the best-return rankings.

## Setup

```bash
pip install -r requirements.txt
python main.py --force     # first run: uses the latest trading day
python -m pytest           # unit tests (no internet needed)
python history.py AAPL     # saved history for one ticker
```

Reports are saved to `reports/YYYY-MM-DD.md`; history goes to `data/stocks.db`.

## Schedule it: 6 pm Central, weekdays

Cron (Linux, and macOS if your machine's clock is set to Central):

```
CRON_TZ=America/Chicago
0 18 * * 1-5 cd /full/path/to/stock_agent && /full/path/to/python main.py >> run.log 2>&1
```

On Windows, use Task Scheduler with a weekday trigger at 6:00 PM.

`main.py` skips market holidays by itself: if Yahoo has no price bar dated today, it exits without saving anything.
Use `--force` to run on the latest trading day anyway.

## How it is organized

| File | Job |
|---|---|
| `config.py` | Settings (time zone, window sizes, how many movers to show) |
| `tickers.csv` | The stocks to track |
| `data.py` | Fetches prices and market caps (yfinance) |
| `analysis.py` | Return math and rankings (pure functions, tested) |
| `storage.py` | SQLite history |
| `report.py` | Markdown report |
| `main.py` | Runs the whole pipeline |

## Things to know

- **Fortune 500 list.** There is no free official API. `tickers.csv` is a starter list of about 65 large US companies, most of them Fortune 500 members. Replace it with the current Fortune 500 list (public companies only) from fortune.com, using Yahoo-style symbols (`BRK-B`, not `BRK.B`).
- **Data source.** yfinance is an unofficial Yahoo Finance wrapper. It is fine for learning, but it can break or rate-limit without notice, and prices are split/dividend adjusted.
- **Rolling windows.** Week and month are the last 5 and 21 trading days, not calendar week-to-date or month-to-date. Change `PERIODS` in `analysis.py` if you want calendar versions.
- **Not investment advice.** This is a tracking tool.

## Next step: the agent layer

Everything above is plain Python on purpose. To make it an agent, wrap `compute_returns`, `top_movers`, `top_companies` and `ticker_history` as tools, and let an LLM decide which to call and write the commentary ("3 of today's 5 biggest losers are energy stocks"). Keep the math in the tools so the numbers are always exact.
