"""Central settings. Change values here, not in the other files."""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TICKERS_CSV = BASE_DIR / "tickers.csv"
EXTRA_TICKERS_CSV = BASE_DIR / "extra_tickers.csv" 
GROUPS_CSV = BASE_DIR / "groups.csv"                 # themed groups (AI, banks, ...) for the agent  # hand-picked names kept across refreshes
DB_PATH = BASE_DIR / "data" / "stocks.db"
REPORTS_DIR = BASE_DIR / "reports"

TIMEZONE = "America/Chicago"   # report runs at 6 pm here, after the 3 pm CT close
HISTORY_PERIOD = "3mo"         # enough daily bars for the 21-day monthly window

TOP_N_MOVERS = 20              # gainers / losers shown per period
TOP_N_COMPANIES = 100           # size of the market-cap leaderboard

OUTLIER_DAY_PCT = 20.0         # one-day moves this big (up or down) get a "verify this" flag


MARKET_CAP_WORKERS = 8         # parallel Yahoo requests when fetching market caps

# Tickers that are share classes of the SAME company. In the market-cap leaderboard
# only the largest ticker of each group is kept, so a company takes one slot.
SAME_COMPANY_GROUPS = [
    ("GOOGL", "GOOG"),   # Alphabet
    ("FOXA", "FOX"),     # Fox Corporation
    ("NWSA", "NWS"),     # News Corp
]