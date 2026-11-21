#!/bin/bash
# Runs the daily pipeline (main.py). launchd starts this at 6:05 pm Chicago time, Monday to Friday.
# Why a wrapper: launchd starts jobs with almost nothing set up (working directory "/", a bare PATH,
# no virtual environment), so this script sets up what main.py needs and keeps a log of every run.
# Usage: scripts/run_daily.sh            (normal daily run)
#        scripts/run_daily.sh --force    (catch up a missed day by hand, after the close)
set -u

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

# The two overrides exist only so tests can use a fake Python and a temporary log folder.
PYTHON="${STOCK_AGENT_PYTHON:-$PROJECT_DIR/.venv/bin/python}"
LOG_DIR="${STOCK_AGENT_LOG_DIR:-$PROJECT_DIR/logs}"
LOG_FILE="$LOG_DIR/daily.log"
LOCK_DIR="$LOG_DIR/run.lock"
MAX_LOG_BYTES=1000000   # about 1 MB, roughly 3 months of runs; one older file is kept as daily.log.1

stamp() { date "+%Y-%m-%d %H:%M:%S %Z"; }

mkdir -p "$LOG_DIR"

# Keep the log from growing forever: when it is over the limit, it becomes daily.log.1 (replacing the old one).
if [ -f "$LOG_FILE" ]; then
    size="$(wc -c < "$LOG_FILE" | tr -d ' ')"
    if [ "$size" -gt "$MAX_LOG_BYTES" ]; then
        mv -f "$LOG_FILE" "$LOG_FILE.1"
    fi
fi

# One run at a time. mkdir either creates the folder or fails, in one step, so two runs cannot both win.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    old_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null)"
    if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
        echo "=== $(stamp) skipped: another run (pid $old_pid) is still going ===" >> "$LOG_FILE"
        exit 0
    fi
    # The run that made the lock is gone (crash, power cut), so the lock is stale.
    echo "=== $(stamp) removing stale lock (pid ${old_pid:-unknown}) ===" >> "$LOG_FILE"
    rm -rf "$LOCK_DIR"
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        echo "=== $(stamp) skipped: another run took the lock first ===" >> "$LOG_FILE"
        exit 0
    fi
fi
echo "$$" > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT

echo "=== start $(stamp) ===" >> "$LOG_FILE"
export PYTHONUNBUFFERED=1   # write output lines as they happen, so the log stays in order
if command -v caffeinate > /dev/null 2>&1; then
    caffeinate -i "$PYTHON" main.py "$@" >> "$LOG_FILE" 2>&1   # macOS: no idle sleep during the run
else
    "$PYTHON" main.py "$@" >> "$LOG_FILE" 2>&1                 # Linux (CI) has no caffeinate
fi
code=$?
echo "=== end $(stamp) exit=$code ===" >> "$LOG_FILE"
exit "$code"
