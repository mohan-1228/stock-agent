"""Read-only JSON API for the dashboard.

Every endpoint asks the same Toolbox the AI agent uses, so the page and the agent
always show the same numbers from the same logic.

Run it (localhost only: the API has no login, so never use --host 0.0.0.0):
    python -m uvicorn api:app --reload
Then open http://127.0.0.1:8000/docs to try every endpoint in the browser.

Everything is read-only except POST /api/ask, which runs the local AI agent (Ollama).
"""
import asyncio
import logging
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path as FilePath
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
import requests

import config
from agent import LocalClient, run_agent_with_trace
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


# ---------- Ask the AI ----------
# The local model runs on an 8 GB Mac and takes about 1 to 3 minutes per question
# (measured 64 s and 74 s for one-tool questions), so only one question runs at a time.

DEFAULT_OLLAMA_MODEL = "qwen3:4b"
ASK_TIMEOUT_SECONDS = 180         # the page gets a clear 504 after this
MODEL_CALL_TIMEOUT_SECONDS = 120  # one reply from Ollama; also limits how long a timed-out run lingers
ASK_RETRY_AFTER_SECONDS = 15
MAX_QUESTION_CHARS = 500
BUSY_MESSAGE = "The AI is answering another question. Try again in a moment."

# One question at a time. Taken by the request, released by the worker thread when the agent
# has really finished (even after a timeout), so a second question never piles onto the model.
ASK_LOCK = threading.Lock()


class AskRequest(BaseModel):
    """Body of POST /api/ask. Spaces around the text are removed before the length check."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    ticker: str | None = Field(default=None, pattern=TICKER_PATTERN)


def ollama_model() -> str:
    return os.environ.get("OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL


def get_ask_client() -> LocalClient:
    """The model client. Always local (Ollama), never Bedrock. Tests replace this function."""
    return LocalClient(ollama_model(), timeout=MODEL_CALL_TIMEOUT_SECONDS)


def _ticker_context(tb: Toolbox, ticker: str) -> str:
    """A sentence about the open stock, built only from our own data (never from the request text).

    The ticker already matched TICKER_PATTERN; get_stock must also find it (404 otherwise).
    We then use the snapshot's symbol and the name from tickers.csv, not what the browser sent.
    """
    stock = _run(tb.get_stock, ticker)["stock"]
    label = f"{stock['ticker']} ({stock['name']})" if stock.get("name") else stock["ticker"]
    return f"the user has the stock panel open for {label}. Use the tools for every number."


def _answer_question(client, question: str, context: str | None, deadline: float) -> dict:
    """Runs in a worker thread. Uses its own database connection (the request's one is closed
    as soon as the response is sent, which can be before this finishes) and always frees the lock."""
    try:
        conn = connect(config.DB_PATH, check_same_thread=False)
        try:
            tb = Toolbox(conn, NAMES, config.SAME_COMPANY_GROUPS, config.OUTLIER_DAY_PCT, theme_groups=GROUPS)
            return run_agent_with_trace(client, question, tb, context=context, deadline=deadline)
        finally:
            conn.close()
    finally:
        ASK_LOCK.release()


def _log_late_failure(future: asyncio.Future) -> None:
    """After a timeout nobody waits for the worker any more; still log it if it crashed."""
    if not future.cancelled() and future.exception() is not None:
        logger.error("Ask worker failed after the timeout", exc_info=future.exception())


@app.post("/api/ask")
async def ask(body: AskRequest, tb: Toolbox = Depends(get_toolbox)):
    """Answer one question with the local AI agent. Returns the answer and the tools it used."""
    if tb.as_of is None:
        raise HTTPException(status_code=503, detail="No saved data yet. Run main.py first.")
    context = _ticker_context(tb, body.ticker) if body.ticker else None

    if not ASK_LOCK.acquire(blocking=False):
        raise HTTPException(
            status_code=429, detail=BUSY_MESSAGE, headers={"Retry-After": str(ASK_RETRY_AFTER_SECONDS)}
        )
    started = time.monotonic()
    try:
        client = get_ask_client()
        # The agent blocks for minutes, so it runs in a worker thread and the server stays responsive.
        future = asyncio.get_running_loop().run_in_executor(
            None, _answer_question, client, body.question, context, started + ASK_TIMEOUT_SECONDS
        )
    except BaseException:
        ASK_LOCK.release()  # the worker never started, so it cannot release the lock itself
        raise

    try:
        # shield: on timeout we stop waiting, but the worker keeps its lock until it really ends.
        result = await asyncio.wait_for(asyncio.shield(future), timeout=ASK_TIMEOUT_SECONDS)
    except TimeoutError:
        future.add_done_callback(_log_late_failure)
        raise HTTPException(
            status_code=504,
            detail=f"The local model took longer than {ASK_TIMEOUT_SECONDS} seconds. "
            "Try a shorter, more specific question.",
        )
    except requests.Timeout:
        raise HTTPException(
            status_code=504,
            detail=f"The local model took longer than {MODEL_CALL_TIMEOUT_SECONDS} seconds to reply. "
            "Try a shorter, more specific question.",
        )
    except requests.ConnectionError:
        raise HTTPException(
            status_code=503,
            detail="The local AI model is not running. Start the Ollama app (or run: ollama serve) and try again.",
        )
    except requests.HTTPError as err:
        if err.response is not None and err.response.status_code == 404:
            model = ollama_model()
            raise HTTPException(
                status_code=503, detail=f"The model {model} is not installed. Run: ollama pull {model}"
            )
        raise  # anything else: the middleware logs it and answers "Internal error."

    return {
        "answer": result["answer"],
        "tools_used": result["tools_used"],
        "as_of": tb.as_of,
        "seconds": round(time.monotonic() - started, 1),
    }


# Serve the dashboard (web/index.html at "/", web/app.js at "/app.js").
# This must stay LAST: FastAPI checks routes in order, so every /api route above
# is matched first and only other paths fall through to the static files.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
