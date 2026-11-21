"""Central settings. Change values here, not in the other files."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TICKERS_CSV = BASE_DIR / "tickers.csv"
EXTRA_TICKERS_CSV = BASE_DIR / "extra_tickers.csv" 
GROUPS_CSV = BASE_DIR / "groups.csv"                 # themed groups (AI, banks, ...) for the agent  # hand-picked names kept across refreshes
DB_PATH = BASE_DIR / "data" / "stocks.db"
REPORTS_DIR = BASE_DIR / "reports"

TIMEZONE = "America/Chicago"   # report runs at 6 pm here, after the 3 pm CT close
HISTORY_PERIOD = "3mo"         # enough daily bars for the 21-day monthly window

# One-time (or monthly) history backfill: python backfill.py. The daily run still uses HISTORY_PERIOD.
BACKFILL_PERIOD = "5y"         # how far back to download closes
BACKFILL_BATCH_SIZE = 100      # tickers per Yahoo request
BACKFILL_PAUSE_SECONDS = 2.0   # rest between batches so Yahoo doesn't block us

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


# ---------- News (milestone 6) ----------
# Headlines for the top companies + the latest day's top movers and outliers, plus
# market-wide news (Yahoo's S&P 500 page) and policy/macro news (official RSS feeds).
NEWS_MOVERS_N = 20               # top gainers and top losers (day) that also get news
NEWS_PER_TICKER = 10             # headlines asked from Yahoo per ticker
NEWS_WORKERS = 8                 # tickers fetched at the same time (8 took ~5 s for 8 tickers)
NEWS_CALL_TIMEOUT_SECONDS = 20   # stop waiting for one source after this (yfinance has no timeout of its own)
NEWS_RETRY_PAUSE_SECONDS = 2.0   # rest before the one retry of a failed source
NEWS_TOTAL_TIMEOUT_SECONDS = 300 # no new ticker is started after this; the news step then wraps up
NEWS_KEEP_DAYS = 30              # headlines older than this are deleted
NEWS_MAX_TITLE_CHARS = 300
MARKET_NEWS_SYMBOL = "^GSPC"     # Yahoo's S&P 500 news = market-wide headlines from the source we already use
NEWS_USER_AGENT = "stock-agent/0.1 (personal, non-commercial)"

# Policy/macro RSS feeds (official sources). Checked 2026-11-21: both answer with RSS 2.0.
# A feed with "user_agent_env" is only fetched when that environment variable is set (in .env):
# the SEC asks automated tools to identify themselves, e.g. SEC_USER_AGENT="Your Name you@example.com".
NEWS_FEEDS = [
    {"name": "Federal Reserve", "url": "https://www.federalreserve.gov/feeds/press_all.xml", "topic": "policy"},
    {"name": "SEC", "url": "https://www.sec.gov/news/pressreleases.rss", "topic": "policy",
     "user_agent_env": "SEC_USER_AGENT"},
]

# Light labels from keywords in the headline. Labels, not predictions.
# "words" match in any case (whole words, an "s" plural allowed); "acronyms" must match exactly
# (so "SEC" does not match "second" and "Fed" does not match "fed up").
NEWS_TAGS = {
    "earnings": {"words": ["earnings", "quarterly results", "revenue", "guidance", "profit"], "acronyms": ["EPS"]},
    "analyst": {"words": ["analyst", "upgrade", "upgraded", "downgrade", "downgraded", "price target", "rating"],
                "acronyms": []},
    "regulation": {"words": ["antitrust", "lawsuit", "probe", "regulator", "regulatory", "fined", "investigation"],
                   "acronyms": ["SEC", "FTC", "DOJ", "FDA"]},
    "merger": {"words": ["merger", "acquisition", "acquire", "acquires", "acquired", "takeover", "buyout"],
               "acronyms": []},
    "policy": {"words": ["Federal Reserve", "interest rate", "rate cut", "rate hike", "tariff", "trade war",
                         "trade deal", "trade talks", "election", "inflation", "jobs report", "Treasury",
                         "central bank"],
               "acronyms": ["Fed", "FOMC", "CPI", "PCE", "GDP"]},
}

ENV_FILE = BASE_DIR / ".env"     # local secrets/settings, gitignored


def load_env_file(path=ENV_FILE):
    """Copy KEY=VALUE lines from .env into the environment. Values already set win.

    Why: launchd starts the daily job with an almost empty environment, so settings like
    SEC_USER_AGENT have to come from a file. A few lines of our own instead of a new package.
    Understands blank lines, # comments, an optional "export " and optional quotes.
    """
    path = Path(path)
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)
