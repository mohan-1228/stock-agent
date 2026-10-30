"""Central settings. Change values here, not in the other files."""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TICKERS_CSV = BASE_DIR / "tickers.csv"
DB_PATH = BASE_DIR / "data" / "stocks.db"
REPORTS_DIR = BASE_DIR / "reports"

TIMEZONE = "America/Chicago"   # report runs at 6 pm here, after the 3 pm CT close
HISTORY_PERIOD = "3mo"         # enough daily bars for the 21-day monthly window

TOP_N_MOVERS = 10              # gainers / losers shown per period
TOP_N_COMPANIES = 50           # size of the market-cap leaderboard
