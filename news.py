"""News: recent headlines per stock, plus market-wide and policy/macro headlines.

    python news.py                      # top companies + latest movers/outliers, market, Fed, SEC
    python news.py --tickers NVDA,AAPL  # only these tickers (plus market and policy)

The daily run (main.py) calls refresh_news at the end, also on market holidays.

Headlines are untrusted text written by others. They are stored and shown as
"recent news that may be relevant", never as the reason for a price move.

Why the timeout code below: yfinance has no timeout and Python cannot kill a thread. So every
fetch runs in a *daemon* thread that we wait for at most NEWS_CALL_TIMEOUT_SECONDS. If it never
answers we stop waiting and move on; a daemon thread does not keep Python alive at exit, and
exit_if_threads_stuck() ends the process at once if any such thread is still around.
"""
import argparse
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import config
from analysis import find_outliers, top_companies, top_movers
from storage import connect, latest_snapshot, prune_news, save_news

# Threads we stopped waiting for. They may still be running; see exit_if_threads_stuck().
_abandoned_threads: list[threading.Thread] = []
_abandoned_lock = threading.Lock()


# ---------- small cleaners ----------

def clean_text(value, limit=config.NEWS_MAX_TITLE_CHARS) -> str:
    """One line of plain text: collapse spaces/newlines, cut at `limit` characters."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def safe_url(value) -> str | None:
    """The URL if it is a normal web link (http or https with a host), otherwise None.

    Why: links end up on the page; a "javascript:" link must never get there.
    """
    url = str(value or "").strip()
    parts = urlparse(url)
    if parts.scheme.lower() in ("http", "https") and parts.netloc:
        return url
    return None


def to_utc_text(value) -> str | None:
    """A time as UTC "YYYY-MM-DDTHH:MM:SSZ", or None if it can't be read.

    Accepts ISO text ("2026-11-21T16:23:54Z", current yfinance), Unix seconds (older yfinance)
    and RSS dates ("Fri, 20 Nov 2026 13:03:24 -0500").
    """
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            moment = datetime.fromtimestamp(value, tz=timezone.utc)
        elif re.match(r"^\d{4}-\d{2}-\d{2}T", str(value)):
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        else:
            moment = parsedate_to_datetime(str(value))
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    if moment.tzinfo is None:  # no zone given: treat as UTC rather than guess the local zone
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tag_patterns():
    patterns = {}
    for tag, spec in config.NEWS_TAGS.items():
        words = [re.compile(rf"\b{re.escape(w)}s?\b", re.IGNORECASE) for w in spec["words"]]
        acronyms = [re.compile(rf"\b{re.escape(a)}\b") for a in spec["acronyms"]]
        patterns[tag] = words + acronyms
    return patterns


_TAG_PATTERNS = _tag_patterns()


def tag_title(title: str) -> list[str]:
    """Labels like ["earnings", "policy"] from keywords in a headline (config.NEWS_TAGS)."""
    return [tag for tag, patterns in _TAG_PATTERNS.items() if any(p.search(title) for p in patterns)]


def _item(title, url, source, published) -> dict | None:
    """One clean headline, or None when something essential is missing or unsafe."""
    title, url, published = clean_text(title), safe_url(url), to_utc_text(published)
    if not title or not url or not published:
        return None
    return {"title": title, "url": url, "source": clean_text(source, 80) or None,
            "published_at": published, "tags": tag_title(title)}


def _dedupe(items) -> list[dict]:
    seen, out = set(), []
    for item in items:
        if item and item["url"] not in seen:
            seen.add(item["url"])
            out.append(item)
    return out


# ---------- adapters: turn each source's format into our headline dicts ----------

def parse_yahoo_news(raw_items) -> list[dict]:
    """yfinance news -> headlines. Reads the current nested format and the older flat one.

    Current (yfinance 1.8): {"id", "content": {"title", "pubDate", "provider": {"displayName"},
    "canonicalUrl": {"url"}, "clickThroughUrl": {"url"}, ...}}.
    Older: {"title", "link", "publisher", "providerPublishTime" (Unix seconds)}.
    """
    items = []
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            continue
        content = raw.get("content") if isinstance(raw.get("content"), dict) else raw
        links = [(content.get("canonicalUrl") or {}).get("url"),
                 (content.get("clickThroughUrl") or {}).get("url"),
                 content.get("link")]
        url = next((link for link in links if safe_url(link)), None)
        source = (content.get("provider") or {}).get("displayName") or content.get("publisher")
        published = content.get("pubDate") or content.get("displayTime") or content.get("providerPublishTime")
        items.append(_item(content.get("title"), url, source, published))
    return _dedupe(items)


def parse_rss(data: bytes, source: str) -> list[dict]:
    """RSS 2.0 bytes -> headlines. Skips items marked sponsored. Bad XML raises ET.ParseError.

    Python's XML parser does not fetch external entities, and its expat (2.7.4 here)
    guards against "billion laughs" expansion, so a hostile feed cannot blow up memory.
    """
    root = ET.fromstring(data)
    items = []
    for node in root.findall("./channel/item"):
        sponsored = any(child.tag.endswith("}sponsored") and (child.text or "").strip().lower() == "true"
                        for child in node)
        if sponsored:
            continue
        items.append(_item(node.findtext("title"), node.findtext("link"), source, node.findtext("pubDate")))
    return _dedupe(items)


# ---------- fetching (network) ----------

def fetch_yahoo_news(symbol: str) -> list[dict]:
    """Headlines Yahoo lists for one symbol (a ticker, or ^GSPC for the whole market)."""
    import yfinance as yf  # imported here so tests never need the network

    return parse_yahoo_news(yf.Ticker(symbol).get_news(count=config.NEWS_PER_TICKER))


def fetch_rss(feed: dict, user_agent: str) -> list[dict]:
    """Download and read one RSS feed."""
    import requests

    response = requests.get(
        feed["url"], headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}, timeout=10
    )
    response.raise_for_status()
    return parse_rss(response.content, feed["name"])


# ---------- never hang ----------

def call_with_timeout(func, arg, timeout: float):
    """Run func(arg), but wait at most `timeout` seconds for it.

    The call runs in a daemon thread. If it has not finished in time we raise TimeoutError
    and stop waiting. The thread cannot be killed, so it is remembered as abandoned; being a
    daemon, it can never stop Python from exiting.
    """
    box = {}

    def target():
        try:
            box["value"] = func(arg)
        except BaseException as exc:  # handed to the caller below
            box["error"] = exc

    label = arg.get("name") if isinstance(arg, dict) else arg
    thread = threading.Thread(target=target, name=f"news-fetch-{label}", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        with _abandoned_lock:
            _abandoned_threads.append(thread)
        raise TimeoutError(f"no answer after {timeout:g} s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def fetch_with_retry(func, arg, timeout: float, pause: float, sleep=time.sleep):
    """call_with_timeout, plus one retry after `pause` seconds. The second error is raised."""
    try:
        return call_with_timeout(func, arg, timeout)
    except Exception:
        sleep(pause)
        return call_with_timeout(func, arg, timeout)


def fetch_many(func, symbols, workers, timeout, pause, total_timeout, sleep=time.sleep):
    """Fetch many symbols in parallel. Returns (results, errors): {symbol: items}, {symbol: message}.

    Each worker only ever waits with a time limit, so every worker finishes. No new symbol is
    started after `total_timeout`; the longest this can take is about
    total_timeout + 2 * timeout + pause, whatever the network does.
    """
    deadline = time.monotonic() + total_timeout

    def one(symbol):
        if time.monotonic() > deadline:
            return symbol, None, f"skipped: the {total_timeout:g} s news time limit was reached"
        try:
            return symbol, fetch_with_retry(func, symbol, timeout, pause, sleep), None
        except Exception as exc:
            return symbol, None, f"{type(exc).__name__}: {exc}"

    results, errors = {}, {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for symbol, items, error in pool.map(one, symbols):
            if error is None:
                results[symbol] = items
            else:
                errors[symbol] = error
    return results, errors


def abandoned_thread_count() -> int:
    with _abandoned_lock:
        return sum(1 for thread in _abandoned_threads if thread.is_alive())


def exit_if_threads_stuck(code: int = 0) -> None:
    """End the process now if a fetch thread we gave up on is still running.

    Daemon threads already don't keep Python alive, but a library's exit-time cleanup could
    still wait on something a stuck thread holds. os._exit skips that cleanup, so the process
    is guaranteed to end. Only call this when all real work is saved (database closed).
    """
    stuck = abandoned_thread_count()
    if stuck:
        print(f"{stuck} news request(s) never answered; exiting without waiting for them.", flush=True)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)


# ---------- which tickers, and the whole refresh ----------

def news_tickers(conn) -> list[str]:
    """Top companies by market cap + the latest day's top gainers/losers + outliers (no duplicates)."""
    _, snapshot = latest_snapshot(conn)
    if snapshot.empty:
        return []
    gainers, losers = top_movers(snapshot, "day_pct", config.NEWS_MOVERS_N)
    picked = [
        *top_companies(snapshot, config.TOP_N_COMPANIES, config.SAME_COMPANY_GROUPS).index,
        *gainers.index,
        *losers.index,
        *find_outliers(snapshot, "day_pct", config.OUTLIER_DAY_PCT).index,
    ]
    return list(dict.fromkeys(picked))  # keeps the order, drops repeats


def refresh_news(conn, tickers=None, *, fetch_yahoo=fetch_yahoo_news, fetch_feed=fetch_rss,
                 env=None, now=None, log=print, sleep=time.sleep,
                 call_timeout=None, total_timeout=None) -> dict:
    """Collect headlines and save them. Returns a summary dict (for printing and tests).

    Fail-safe: a source that fails or returns nothing changes nothing in the database.
    Only this (main) thread writes to SQLite; worker threads just download and parse.
    """
    env = os.environ if env is None else env
    now = now or datetime.now(timezone.utc)
    call_timeout = config.NEWS_CALL_TIMEOUT_SECONDS if call_timeout is None else call_timeout
    total_timeout = config.NEWS_TOTAL_TIMEOUT_SECONDS if total_timeout is None else total_timeout
    fetched_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    started = time.monotonic()
    tickers = news_tickers(conn) if tickers is None else [t.strip().upper() for t in tickers if t.strip()]

    market = config.MARKET_NEWS_SYMBOL
    results, errors = fetch_many(fetch_yahoo, [market, *tickers], config.NEWS_WORKERS, call_timeout,
                                 config.NEWS_RETRY_PAUSE_SECONDS, total_timeout, sleep)

    with_news = [t for t in tickers if results.get(t)]
    for ticker in with_news:
        save_news(conn, results[ticker], ticker, fetched_at)

    market_items = results.get(market) or []
    save_news(conn, market_items, "market", fetched_at)
    policy_from_market = [item for item in market_items if "policy" in item["tags"]]
    save_news(conn, policy_from_market, "policy", fetched_at)

    feeds = {}
    for feed in config.NEWS_FEEDS:
        env_name = feed.get("user_agent_env")
        user_agent = env.get(env_name, "").strip() if env_name else config.NEWS_USER_AGENT
        if not user_agent:
            feeds[feed["name"]] = f"skipped: set {env_name} in .env (format: Your Name you@example.com)"
            log(f"Skipping the {feed['name']} feed: {env_name} is not set. "
                f'Add {env_name}="Your Name you@example.com" to .env to include it.')
            continue
        try:
            items = fetch_with_retry(lambda f: fetch_feed(f, user_agent), feed, call_timeout,
                                     config.NEWS_RETRY_PAUSE_SECONDS, sleep)
        except Exception as exc:
            feeds[feed["name"]] = f"failed: {type(exc).__name__}: {exc}"
            log(f"The {feed['name']} feed failed ({type(exc).__name__}: {exc}); its saved headlines are kept.")
            continue
        save_news(conn, items, feed["topic"], fetched_at)
        feeds[feed["name"]] = len(items)

    cutoff = (now - timedelta(days=config.NEWS_KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "tickers_asked": len(tickers),
        "tickers_with_news": len(with_news),
        "tickers_empty": [t for t in tickers if t in results and not results[t]],
        "errors": errors,
        "market_items": len(market_items),
        "policy_from_market": len(policy_from_market),
        "feeds": feeds,
        "pruned": prune_news(conn, cutoff),
        "seconds": round(time.monotonic() - started, 1),
    }


def format_summary(summary: dict) -> str:
    """A few readable lines for the log."""
    lines = [
        f"News: {summary['tickers_with_news']} of {summary['tickers_asked']} tickers got headlines, "
        f"market {summary['market_items']} (policy-tagged {summary['policy_from_market']}), "
        f"in {summary['seconds']} s.",
    ]
    for name, result in summary["feeds"].items():
        lines.append(f"  {name}: {result if isinstance(result, str) else f'{result} headlines'}")
    if summary["tickers_empty"]:
        lines.append(f"  No headlines from Yahoo for: {', '.join(summary['tickers_empty'][:10])}")
    if summary["errors"]:
        shown = list(summary["errors"].items())[:10]
        lines.append(f"  {len(summary['errors'])} failed (saved headlines kept): "
                     + "; ".join(f"{symbol} ({error})" for symbol, error in shown))
    lines.append(f"  Deleted {summary['pruned']} headlines older than {config.NEWS_KEEP_DAYS} days.")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Collect recent headlines into the news table.")
    parser.add_argument("--tickers", help="comma-separated tickers, e.g. NVDA,AAPL (default: top companies + movers)")
    args = parser.parse_args()
    config.load_env_file()
    conn = connect(config.DB_PATH)
    try:
        tickers = args.tickers.split(",") if args.tickers else None
        print(format_summary(refresh_news(conn, tickers)))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
    exit_if_threads_stuck()
