"""Read-only JSON API for the dashboard.

Every endpoint asks the same Toolbox the AI agent uses, so the page and the agent
always show the same numbers from the same logic.

Run it (localhost only: the API has no login, so never use --host 0.0.0.0):
    python -m uvicorn api:app --reload
Then open http://127.0.0.1:8000/docs to try every endpoint in the browser.
"""
import logging
from collections.abc import Iterator
from pathlib import Path as FilePath
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import config
from storage import connect
from tools import MAX_PRICE_DAYS, MAX_STEP, Toolbox
from universe import load_groups, load_universe

# uvicorn already prints this logger to the console, so our error logs show up next to its own.
logger = logging.getLogger("uvicorn.error")

# Ticker names and groups change rarely, so read the CSV files once at startup.
_universe = load_universe(config.TICKERS_CSV)
NAMES = dict(zip(_universe["ticker"], _universe["name"]))
GROUPS = load_groups(config.GROUPS_CSV)

TICKER_PATTERN = r"^[A-Za-z0-9.\-]{1,10}$"  # AAPL, BRK-B, BRK.B
GROUP_PATTERN = r"^[A-Za-z0-9_\-]{1,40}$"  # ai_stocks, banks
WEB_DIR = FilePath(__file__).parent / "web"  # the dashboard page (index.html, app.js)

app = FastAPI(title="Stock Agent API", description="Information only, not investment advice.")


@app.middleware("http")
async def hide_internal_errors(request: Request, call_next):
    """Turn any unexpected crash into a plain 500.

    The full error goes to the server console (for you); the browser only sees
    "Internal error." so file paths and stack traces never leak.
    """
    try:
        return await call_next(request)
    except Exception:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "Internal error."}, status_code=500)


def get_toolbox() -> Iterator[Toolbox]:
    """A fresh Toolbox for each request, closed afterwards.

    Fresh, so the page sees new data as soon as main.py saves it (no restart needed).
    The connection allows other threads because FastAPI may set up this dependency and
    run the endpoint on different worker threads; each request still has its own connection.
    """
    conn = connect(config.DB_PATH, check_same_thread=False)
    try:
        yield Toolbox(
            conn, NAMES, config.SAME_COMPANY_GROUPS, config.OUTLIER_DAY_PCT, theme_groups=GROUPS
        )
    finally:
        conn.close()


def _run(tool, *args):
    """Call a Toolbox method and turn its errors into clear HTTP answers.

    ValueError means "not found" (unknown ticker or group): 404.
    RuntimeError means "no data yet" (main.py never ran, no groups.csv): 503.
    """
    try:
        return tool(*args)
    except ValueError as err:
        raise HTTPException(status_code=404, detail=str(err))
    except RuntimeError as err:
        raise HTTPException(status_code=503, detail=str(err))


@app.get("/api/health")
def health():
    """Is the server up? Does not touch the database."""
    return {"status": "ok"}


@app.get("/api/summary")
def summary(tb: Toolbox = Depends(get_toolbox)):
    outliers = _run(tb.get_outliers)
    return {"as_of": tb.as_of, "stocks": len(tb.snapshot), "outliers": len(outliers["stocks"])}


@app.get("/api/movers")
def movers(
    period: Literal["day", "week", "month"],
    direction: Literal["gainers", "losers"],
    n: int = Query(20, ge=1, le=50),
    tb: Toolbox = Depends(get_toolbox),
):
    return _run(tb.get_movers, period, direction, n)


@app.get("/api/companies")
def companies(n: int = Query(100, ge=1, le=100), tb: Toolbox = Depends(get_toolbox)):
    return _run(tb.get_top_companies, n)


@app.get("/api/stock/{ticker}")
def stock(ticker: str = Path(pattern=TICKER_PATTERN), tb: Toolbox = Depends(get_toolbox)):
    """One stock's numbers, plus which groups it belongs to and whether it is flagged."""
    result = _run(tb.get_stock, ticker)
    symbol = result["stock"]["ticker"]
    flagged = {row["ticker"] for row in _run(tb.get_outliers)["stocks"]}
    result["stock"]["groups"] = sorted(
        name for name, members in tb.theme_groups.items() if symbol in members
    )
    result["stock"]["is_outlier"] = symbol in flagged
    return result


@app.get("/api/stock/{ticker}/prices")
def stock_prices(
    ticker: str = Path(pattern=TICKER_PATTERN),
    days: int = Query(63, ge=1, le=MAX_PRICE_DAYS),
    step: int = Query(1, ge=1, le=MAX_STEP),
    tb: Toolbox = Depends(get_toolbox),
):
    return _run(tb.get_price_series, ticker, days, step)


@app.get("/api/groups")
def groups(tb: Toolbox = Depends(get_toolbox)):
    return _run(tb.get_group_summary)


@app.get("/api/groups/{name}")
def group(name: str = Path(pattern=GROUP_PATTERN), tb: Toolbox = Depends(get_toolbox)):
    return _run(tb.get_group_summary, name)


@app.get("/api/outliers")
def outliers(tb: Toolbox = Depends(get_toolbox)):
    return _run(tb.get_outliers)


# Serve the dashboard (web/index.html at "/", web/app.js at "/app.js").
# This must stay LAST: FastAPI checks routes in order, so every /api route above
# is matched first and only other paths fall through to the static files.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
