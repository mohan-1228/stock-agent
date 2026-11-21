"""News tests: adapters on saved samples, tags, storage, the never-hang fetching, refresh, tools, main.py.

No network: every fetch is a fake. The samples in tests/fixtures have the real structure of
yfinance 1.8 news and RSS 2.0 feeds (checked 2026-11-21) with made-up headlines and links.
"""
import json
import subprocess
import sys
import textwrap
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

import config
import main
import news
from storage import connect, news_fetched_at, news_for_topic, prune_news, save_news, save_snapshot
from tools import MAX_NEWS_TOOL_ITEMS, NEWS_TOOL_TITLE_CHARS, TOOL_SPECS, Toolbox

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 11, 22, 0, 0, tzinfo=timezone.utc)


def yahoo_sample():
    return json.loads((FIXTURES / "yahoo_news_sample.json").read_text())


def headline(url, title="A headline", published="2026-11-21T10:00:00Z", tags=(), source="Example"):
    return {"url": url, "title": title, "source": source, "published_at": published, "tags": list(tags)}


# ---------- small cleaners ----------

@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-11-21T16:23:54Z", "2026-11-21T16:23:54Z"),
        ("2026-11-21T10:23:54-06:00", "2026-11-21T16:23:54Z"),
        ("Fri, 20 Nov 2026 13:03:24 -0500", "2026-11-20T18:03:24Z"),
        ("Fri, 20 Nov 2026 21:15:00 GMT", "2026-11-20T21:15:00Z"),
        (1795279434, "2026-11-21T16:43:54Z"),  # Unix seconds (older yfinance)
        ("sometime last week", None),
        ("", None),
        (None, None),
    ],
)
def test_to_utc_text(value, expected):
    assert news.to_utc_text(value) == expected


@pytest.mark.parametrize(
    "url, ok",
    [
        ("https://www.example.com/a", True),
        ("http://example.com/b", True),
        ("HTTPS://EXAMPLE.COM/c", True),
        ("javascript:alert(1)", False),
        ("data:text/html,hi", False),
        ("//example.com/no-scheme", False),
        ("https://", False),
        ("", False),
        (None, False),
    ],
)
def test_safe_url_keeps_only_web_links(url, ok):
    assert (news.safe_url(url) is not None) is ok


def test_clean_text_collapses_spaces_and_cuts():
    assert news.clean_text("  a \n  b\tc ") == "a b c"
    assert news.clean_text("x" * 20, limit=10) == "x" * 9 + "…"


# ---------- tags ----------

@pytest.mark.parametrize(
    "title, tags",
    [
        ("Example Corp beats quarterly earnings, raises guidance", ["earnings"]),
        ("Analyst upgrades Example to buy, lifts price target", ["analyst"]),
        ("SEC opens probe into Example's accounting", ["regulation"]),
        ("Example agrees to acquire Rival in $5 billion takeover", ["merger"]),
        ("Fed signals pause as inflation cools", ["policy"]),
        ("Tariffs hit Example's revenue", ["earnings", "policy"]),
        ("Example ships its second chip; investors fed up with delays", []),  # not SEC, not Fed
        ("A quiet day for Example", []),
    ],
)
def test_tag_title(title, tags):
    assert news.tag_title(title) == tags


# ---------- adapters ----------

def test_parse_yahoo_news_reads_the_current_format():
    items = news.parse_yahoo_news(yahoo_sample())
    assert [i["url"] for i in items] == [
        "https://finance.yahoo.com/markets/article/example-chip-maker-earnings-1.html",
        "https://www.example.com/video/fed-pause-2",  # canonicalUrl missing: clickThroughUrl used
    ]  # unsafe link, missing time and the duplicate URL are dropped
    first, second = items
    assert first == {
        "title": "Example Chip Maker Beats Quarterly Earnings Estimates",
        "url": "https://finance.yahoo.com/markets/article/example-chip-maker-earnings-1.html",
        "source": "Yahoo Finance",
        "published_at": "2026-11-21T16:23:54Z",
        "tags": ["earnings"],
    }
    assert second["title"] == "Fed officials signal a pause in rate hikes"  # spaces tidied
    assert second["tags"] == ["policy"]


def test_parse_yahoo_news_reads_the_older_flat_format():
    old = [{"title": "Old style headline", "link": "https://example.com/old", "publisher": "Old Wire",
            "providerPublishTime": 1795279434}]
    assert news.parse_yahoo_news(old) == [{
        "title": "Old style headline", "url": "https://example.com/old", "source": "Old Wire",
        "published_at": "2026-11-21T16:43:54Z", "tags": [],
    }]


def test_parse_yahoo_news_survives_junk():
    assert news.parse_yahoo_news(None) == []
    assert news.parse_yahoo_news(["not a dict", {"content": {}}, {}]) == []


def test_parse_rss_reads_items_skips_sponsored_and_broken_ones():
    items = news.parse_rss((FIXTURES / "rss_sample.xml").read_bytes(), "Example Feed")
    assert [i["title"] for i in items] == [
        "Example Board announces interest rate decision",
        "SEC Charges Example Fund With Fraud & Misleading Investors",  # &amp; decoded
        "Not sponsored, says so explicitly",
    ]
    assert items[1]["published_at"] == "2026-11-20T14:28:28Z"  # -0500 converted to UTC
    assert items[1]["source"] == "Example Feed"
    assert items[0]["tags"] == ["policy"] and items[1]["tags"] == ["regulation"]


def test_parse_rss_bad_xml_raises():
    with pytest.raises(ET.ParseError):
        news.parse_rss(b"<html>not a feed", "x")


# ---------- storage ----------

@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "news.db")
    yield c
    c.close()


def test_save_news_is_idempotent_and_one_row_per_url(conn):
    items = [headline("https://e.com/1", tags=["earnings"]), headline("https://e.com/2")]
    save_news(conn, items, "NVDA", "2026-11-22T00:00:00Z")
    save_news(conn, items, "NVDA", "2026-11-22T00:00:00Z")  # same run again
    save_news(conn, items[:1], "market", "2026-11-22T00:00:00Z")  # same article, second topic
    assert conn.execute("SELECT COUNT(*) FROM news").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM news_topics").fetchone()[0] == 3
    assert news_for_topic(conn, "market", 10)[0]["tags"] == ["earnings"]


def test_save_news_with_nothing_changes_nothing(conn):
    save_news(conn, [headline("https://e.com/1")], "NVDA", "2026-11-21T00:00:00Z")
    save_news(conn, [], "NVDA", "2026-11-22T00:00:00Z")
    assert len(news_for_topic(conn, "NVDA")) == 1
    assert news_fetched_at(conn, "NVDA") == "2026-11-21T00:00:00Z"


def test_news_for_topic_newest_first_with_limit(conn):
    save_news(conn, [
        headline("https://e.com/old", published="2026-11-19T10:00:00Z"),
        headline("https://e.com/new", published="2026-11-21T10:00:00Z"),
        headline("https://e.com/mid", published="2026-11-20T10:00:00Z"),
    ], "NVDA", "2026-11-22T00:00:00Z")
    assert [i["url"] for i in news_for_topic(conn, "NVDA", 2)] == ["https://e.com/new", "https://e.com/mid"]
    assert news_for_topic(conn, "AAPL") == [] and news_fetched_at(conn, "AAPL") is None


def test_prune_news_deletes_old_headlines_and_their_topics(conn):
    save_news(conn, [headline("https://e.com/old", published="2026-10-01T00:00:00Z"),
                     headline("https://e.com/new", published="2026-11-21T00:00:00Z")], "NVDA", "2026-11-22T00:00:00Z")
    assert prune_news(conn, "2026-10-23T00:00:00Z") == 1
    assert [i["url"] for i in news_for_topic(conn, "NVDA")] == ["https://e.com/new"]
    assert conn.execute("SELECT COUNT(*) FROM news_topics").fetchone()[0] == 1


# ---------- never hang ----------

def test_call_with_timeout_returns_value_and_passes_errors():
    assert news.call_with_timeout(lambda x: x * 2, 21, 1) == 42
    with pytest.raises(KeyError):
        news.call_with_timeout(lambda x: {}[x], "missing", 1)


def test_call_with_timeout_gives_up_on_a_call_that_never_returns():
    never = threading.Event()
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="no answer after 0.2 s"):
        news.call_with_timeout(lambda _: never.wait(), "STUCK", 0.2)
    assert time.monotonic() - started < 2
    stuck = [t for t in threading.enumerate() if t.name == "news-fetch-STUCK"]
    assert stuck and all(t.daemon for t in stuck)  # a daemon thread cannot keep Python alive
    never.set()  # let it finish so it doesn't linger in the test run


def test_fetch_with_retry_retries_once_after_a_pause():
    calls, pauses = [], []

    def flaky(symbol):
        calls.append(symbol)
        if len(calls) == 1:
            raise ConnectionError("blip")
        return ["ok"]

    assert news.fetch_with_retry(flaky, "NVDA", 1, 2.0, sleep=pauses.append) == ["ok"]
    assert calls == ["NVDA", "NVDA"] and pauses == [2.0]


def test_fetch_with_retry_raises_the_second_error():
    def always(symbol):
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        news.fetch_with_retry(always, "NVDA", 1, 0, sleep=lambda s: None)


def test_fetch_many_finishes_even_when_one_fetch_never_returns():
    never = threading.Event()

    def fetch(symbol):
        if symbol == "STUCK":
            never.wait()
        return [symbol]

    started = time.monotonic()
    results, errors = news.fetch_many(fetch, ["AAA", "STUCK", "BBB"], workers=2, timeout=0.2,
                                      pause=0, total_timeout=10, sleep=lambda s: None)
    assert time.monotonic() - started < 3
    assert results == {"AAA": ["AAA"], "BBB": ["BBB"]}
    assert "TimeoutError" in errors["STUCK"]
    never.set()


def test_fetch_many_starts_nothing_new_after_the_total_limit():
    results, errors = news.fetch_many(lambda s: [s], ["AAA", "BBB"], workers=1, timeout=1,
                                      pause=0, total_timeout=-1, sleep=lambda s: None)
    assert results == {} and set(errors) == {"AAA", "BBB"}
    assert "time limit" in errors["AAA"]


STUCK_SCRIPT = textwrap.dedent('''
    import sys, threading
    import news
    from storage import connect

    never = threading.Event()

    def fetch(symbol):
        if symbol == "STUCK":
            never.wait()  # never returns, like a hung yfinance call
        return []

    conn = connect(sys.argv[1])
    summary = news.refresh_news(conn, ["OK", "STUCK"], fetch_yahoo=fetch, fetch_feed=lambda f, ua: [],
                                env={}, log=lambda m: None, sleep=lambda s: None,
                                call_timeout=0.3, total_timeout=5)
    conn.close()
    print("errors:", sorted(summary["errors"]), "stuck threads:", news.abandoned_thread_count(), flush=True)
    if sys.argv[2] == "exit-helper":
        news.exit_if_threads_stuck()
        print("not reached")
    # otherwise: the script simply ends, with a thread still stuck forever
''')


@pytest.mark.parametrize("ending", ["plain-end", "exit-helper"])
def test_process_exits_even_with_a_fetch_that_never_returns(tmp_path, ending):
    script = tmp_path / "stuck.py"
    script.write_text(STUCK_SCRIPT)
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path / "t.db"), ending],
        cwd=ROOT, env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=30,  # the test fails (instead of hanging) if it never exits
    )
    assert time.monotonic() - started < 20
    assert result.returncode == 0, result.stderr
    assert "errors: ['STUCK'] stuck threads: 2" in result.stdout  # the first try and the one retry
    assert "not reached" not in result.stdout
    if ending == "exit-helper":
        assert "never answered; exiting without waiting" in result.stdout


# ---------- refresh_news (fakes only) ----------

def snapshot():
    """6 stocks: AAA..CCC are big, DDD/EEE/FFF small; FFF moved 25% (an outlier)."""
    return pd.DataFrame(
        {
            "close": [100.0, 50.0, 20.0, 10.0, 5.0, 2.0],
            "day_pct": [1.0, -1.0, 0.5, 3.0, -4.0, 25.0],
            "week_pct": [1.0] * 6,
            "month_pct": [1.0] * 6,
            "market_cap": [3e12, 2e12, 1e12, 1e9, 1e9, 1e9],
        },
        index=pd.Index(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"], name="ticker"),
    )


@pytest.fixture
def db(conn):
    save_snapshot(conn, "2026-11-20", snapshot())
    return conn


class FakeYahoo:
    """Returns one made-up headline per symbol; symbols in `fail` raise."""

    def __init__(self, fail=(), empty=(), extra=None):
        self.fail, self.empty, self.extra = set(fail), set(empty), extra or {}
        self.asked = []

    def __call__(self, symbol):
        self.asked.append(symbol)
        if symbol in self.fail:
            raise ConnectionError(f"{symbol} down")
        if symbol in self.empty:
            return []
        return [headline(f"https://e.com/{symbol}", title=f"News about {symbol}")] + self.extra.get(symbol, [])


def run_refresh(conn, tickers=None, yahoo=None, feed=None, env=None, logs=None):
    return news.refresh_news(
        conn, tickers, fetch_yahoo=yahoo or FakeYahoo(),
        fetch_feed=feed or (lambda f, ua: [headline(f"https://gov.example/{f['name']}", title=f"{f['name']} release")]),
        env={} if env is None else env, now=NOW, log=(logs.append if logs is not None else lambda m: None),
        sleep=lambda s: None,
    )


def test_news_tickers_are_top_companies_movers_and_outliers(db, monkeypatch):
    monkeypatch.setattr(config, "TOP_N_COMPANIES", 2)
    monkeypatch.setattr(config, "NEWS_MOVERS_N", 1)
    assert news.news_tickers(db) == ["AAA", "BBB", "FFF", "EEE"]  # top 2, best and worst day, outlier FFF (already in)


def test_news_tickers_without_data_is_empty(conn):
    assert news.news_tickers(conn) == []


def test_refresh_saves_tickers_market_and_policy(db):
    policy_item = headline("https://e.com/fed", title="Fed holds interest rate steady", tags=["policy"])
    yahoo = FakeYahoo(extra={"^GSPC": [policy_item]})
    summary = run_refresh(db, ["aaa", "BBB"], yahoo=yahoo, env={"SEC_USER_AGENT": "Test Person test@example.com"})
    assert sorted(yahoo.asked) == ["AAA", "BBB", "^GSPC"]  # tickers upper-cased, plus the market symbol
    assert [i["url"] for i in news_for_topic(db, "AAA")] == ["https://e.com/AAA"]
    assert {i["url"] for i in news_for_topic(db, "market")} == {"https://e.com/^GSPC", "https://e.com/fed"}
    assert {i["url"] for i in news_for_topic(db, "policy")} == {
        "https://e.com/fed", "https://gov.example/Federal Reserve", "https://gov.example/SEC"}
    assert news_fetched_at(db, "AAA") == "2026-11-22T00:00:00Z"
    assert summary["tickers_with_news"] == 2 and summary["market_items"] == 2
    assert summary["policy_from_market"] == 1
    assert summary["feeds"] == {"Federal Reserve": 1, "SEC": 1}


def test_refresh_without_tickers_uses_the_latest_snapshot(db, monkeypatch):
    monkeypatch.setattr(config, "TOP_N_COMPANIES", 1)
    monkeypatch.setattr(config, "NEWS_MOVERS_N", 1)
    yahoo = FakeYahoo()
    run_refresh(db, yahoo=yahoo)
    assert sorted(yahoo.asked) == ["AAA", "EEE", "FFF", "^GSPC"]


def test_refresh_failure_or_empty_result_keeps_saved_headlines(db):
    save_news(db, [headline("https://e.com/kept-aaa")], "AAA", "2026-11-20T00:00:00Z")
    save_news(db, [headline("https://e.com/kept-bbb")], "BBB", "2026-11-20T00:00:00Z")
    summary = run_refresh(db, ["AAA", "BBB"], yahoo=FakeYahoo(fail={"AAA"}, empty={"BBB"}))
    assert [i["url"] for i in news_for_topic(db, "AAA")] == ["https://e.com/kept-aaa"]
    assert [i["url"] for i in news_for_topic(db, "BBB")] == ["https://e.com/kept-bbb"]
    assert "ConnectionError" in summary["errors"]["AAA"]
    assert summary["tickers_empty"] == ["BBB"]


def test_refresh_skips_sec_without_user_agent_and_says_so(db):
    logs, seen = [], []

    def feed(f, user_agent):
        seen.append((f["name"], user_agent))
        return []

    summary = run_refresh(db, [], feed=feed, env={}, logs=logs)
    assert seen == [("Federal Reserve", config.NEWS_USER_AGENT)]  # SEC not requested at all
    assert summary["feeds"]["SEC"].startswith("skipped: set SEC_USER_AGENT")
    assert any("Skipping the SEC feed" in line and 'SEC_USER_AGENT="Your Name you@example.com"' in line for line in logs)


def test_refresh_sends_the_sec_user_agent_when_set(db):
    seen = []
    run_refresh(db, [], feed=lambda f, ua: seen.append((f["name"], ua)) or [],
                env={"SEC_USER_AGENT": "  Test Person test@example.com  "})
    assert ("SEC", "Test Person test@example.com") in seen


def test_refresh_one_feed_failing_does_not_stop_the_others(db):
    save_news(db, [headline("https://gov.example/old-fed")], "policy", "2026-11-20T00:00:00Z")
    logs = []

    def feed(f, ua):
        if f["name"] == "Federal Reserve":
            raise ConnectionError("fed down")
        return [headline("https://gov.example/sec-new")]

    summary = run_refresh(db, [], feed=feed, env={"SEC_USER_AGENT": "T t@example.com"}, logs=logs)
    assert summary["feeds"]["Federal Reserve"].startswith("failed: ConnectionError")
    assert summary["feeds"]["SEC"] == 1
    assert {i["url"] for i in news_for_topic(db, "policy")} == {"https://gov.example/old-fed", "https://gov.example/sec-new"}
    assert any("headlines are kept" in line for line in logs)


def test_refresh_prunes_headlines_older_than_keep_days(db):
    save_news(db, [headline("https://e.com/ancient", published="2026-10-01T00:00:00Z")], "AAA", "2026-10-01T00:00:00Z")
    summary = run_refresh(db, [])
    assert summary["pruned"] == 1
    assert news_for_topic(db, "AAA") == []


def test_format_summary_is_readable(db):
    summary = run_refresh(db, ["AAA", "BBB"], yahoo=FakeYahoo(fail={"BBB"}))
    text = news.format_summary(summary)
    assert text.startswith("News: 1 of 2 tickers got headlines, market 1")
    assert "Federal Reserve: 1 headlines" in text
    assert "SEC: skipped" in text
    assert "1 failed (saved headlines kept): BBB (ConnectionError" in text


# ---------- .env loader ----------

def test_load_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(textwrap.dedent('''
        # a comment
        SEC_USER_AGENT="Test Person test@example.com"
        export SINGLE='single quoted'
        PLAIN = plain value
        ALREADY=from file
        not a setting line
    '''))
    for key in ("SEC_USER_AGENT", "SINGLE", "PLAIN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ALREADY", "from environment")
    config.load_env_file(env_file)
    import os
    assert os.environ["SEC_USER_AGENT"] == "Test Person test@example.com"
    assert os.environ["SINGLE"] == "single quoted"
    assert os.environ["PLAIN"] == "plain value"
    assert os.environ["ALREADY"] == "from environment"  # the real environment wins


def test_load_env_file_missing_is_fine(tmp_path):
    config.load_env_file(tmp_path / "nope.env")


def test_env_example_is_committed_but_env_is_ignored():
    assert "SEC_USER_AGENT=" in (ROOT / ".env.example").read_text()
    assert ".env" in (ROOT / ".gitignore").read_text().splitlines()


# ---------- tools ----------

@pytest.fixture
def toolbox(db):
    long_title = "Example " + "word " * 60
    save_news(db, [headline(f"https://e.com/aaa{i}", title=long_title if i == 0 else f"AAA story {i}",
                            published=f"2026-11-21T{10 + i:02d}:00:00Z") for i in range(10)], "AAA", "2026-11-22T00:00:00Z")
    save_news(db, [headline("https://e.com/m", title="Market story")], "market", "2026-11-22T00:00:00Z")
    return Toolbox(db, {"AAA": "Alpha"})


def test_get_news_is_short_for_the_model(toolbox):
    result = toolbox.get_news("aaa", n=50)
    assert result["topic"] == "AAA" and result["as_of"] == "2026-11-20"
    assert result["fetched_at"] == "2026-11-22T00:00:00Z"
    assert len(result["items"]) == MAX_NEWS_TOOL_ITEMS
    assert all(len(i["title"]) <= NEWS_TOOL_TITLE_CHARS for i in result["items"])
    assert set(result["items"][0]) == {"title", "source", "url", "published_at", "tags"}
    assert "not an explanation of price moves" in result["note"]


def test_get_news_unknown_ticker_raises(toolbox):
    with pytest.raises(ValueError):
        toolbox.get_news("ZZZ")


def test_get_news_known_ticker_without_news_explains_why(toolbox):
    result = toolbox.get_news("BBB")
    assert result["items"] == [] and "collected for the top" in result["note"]


def test_market_and_policy_tools(toolbox):
    assert [i["title"] for i in toolbox.get_market_news()["items"]] == ["Market story"]
    policy = toolbox.get_policy_news()
    assert policy["items"] == [] and "python news.py" in policy["note"]


def test_api_style_feed_allows_more_items_and_full_titles(toolbox):
    result = toolbox.ticker_news("AAA", 20)
    assert len(result["items"]) == 10
    assert any(len(i["title"]) > NEWS_TOOL_TITLE_CHARS for i in result["items"])


def test_news_tools_are_registered_for_the_model(toolbox):
    names = {spec["toolSpec"]["name"] for spec in TOOL_SPECS}
    assert {"get_news", "get_market_news", "get_policy_news"} <= names
    assert toolbox.call("get_news", {"ticker": "AAA", "n": 1})["items"][0]["title"].startswith("AAA story")


def test_agent_prompt_treats_headlines_as_untrusted():
    from agent import SYSTEM_PROMPT

    assert "untrusted text written by others" in SYSTEM_PROMPT
    assert "Never say a headline caused a price move" in SYSTEM_PROMPT


# ---------- main.py ----------

def test_main_runs_news_even_when_the_market_was_closed(monkeypatch, capsys):
    closes = pd.DataFrame({"AAA": [1.0, 2.0]}, index=pd.to_datetime(["2000-01-03", "2000-01-04"]))
    calls = []
    monkeypatch.setattr(main, "load_universe", lambda path: pd.DataFrame({"ticker": ["AAA"], "name": ["Alpha"]}))
    monkeypatch.setattr(main, "fetch_closes", lambda tickers, period: closes)
    monkeypatch.setattr(main, "run_news_step", lambda: calls.append("news"))
    monkeypatch.setattr(config, "load_env_file", lambda *a: None)
    monkeypatch.setattr(sys, "argv", ["main.py"])
    main.main()
    assert "market closed today. Skipping." in capsys.readouterr().out
    assert calls == ["news"]


def test_news_step_failure_never_breaks_the_daily_run(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")

    def boom(conn):
        raise RuntimeError("yahoo changed everything")

    monkeypatch.setattr(main, "refresh_news", boom)
    main.run_news_step()  # must not raise
    out = capsys.readouterr().out
    assert "News step failed (RuntimeError: yahoo changed everything)" in out
    assert "Prices and the report are not affected." in out
