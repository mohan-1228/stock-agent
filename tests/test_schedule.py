"""Tests for the daily schedule: the launchd plist template and the run_daily.sh wrapper.

The wrapper tests run the real script with a fake "python" and a temporary log folder
(via STOCK_AGENT_PYTHON and STOCK_AGENT_LOG_DIR), so main.py and the network are never touched.
"""
import os
import plistlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "scripts" / "run_daily.sh"
PLIST = ROOT / "launchd" / "local.stock-agent.daily.plist"

FAKE_PYTHON = """#!/bin/bash
echo "fake main ran $*"
touch "$FAKE_MARKER"
exit "${FAKE_EXIT:-0}"
"""


def load_plist() -> dict:
    with PLIST.open("rb") as f:
        return plistlib.load(f)


def run_wrapper(tmp_path: Path, *args: str, exit_code: int = 0) -> subprocess.CompletedProcess:
    """Run the wrapper with a fake python that prints its arguments and leaves a marker file."""
    fake = tmp_path / "fake_python"
    fake.write_text(FAKE_PYTHON)
    fake.chmod(0o755)
    env = {
        **os.environ,
        "STOCK_AGENT_PYTHON": str(fake),
        "STOCK_AGENT_LOG_DIR": str(tmp_path / "logs"),
        "FAKE_MARKER": str(tmp_path / "ran"),
        "FAKE_EXIT": str(exit_code),
    }
    return subprocess.run([str(WRAPPER), *args], env=env, capture_output=True, text=True, timeout=30)


def dead_pid() -> int:
    """The pid of a process that has already finished."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


# --- plist template ---

def test_plist_is_valid_with_label_and_program():
    plist = load_plist()
    assert plist["Label"] == "local.stock-agent.daily"
    assert plist["ProgramArguments"] == ["/bin/bash", "__PROJECT_DIR__/scripts/run_daily.sh"]
    assert plist["RunAtLoad"] is False


def test_plist_runs_weekdays_at_6_05_pm():
    schedule = load_plist()["StartCalendarInterval"]
    assert schedule == [{"Weekday": day, "Hour": 18, "Minute": 5} for day in range(1, 6)]


def test_plist_has_placeholder_not_real_paths():
    text = PLIST.read_text()
    assert "__PROJECT_DIR__" in text
    assert "/Users/" not in text


# --- wrapper ---

def test_wrapper_exists_and_is_executable():
    assert WRAPPER.is_file()
    assert os.access(WRAPPER, os.X_OK)


def test_wrapper_logs_output_and_passes_exit_code_and_args(tmp_path):
    result = run_wrapper(tmp_path, "--force", exit_code=3)
    log = (tmp_path / "logs" / "daily.log").read_text()
    assert result.returncode == 3
    assert "=== start " in log
    assert "fake main ran main.py --force" in log
    assert "exit=3 ===" in log


def test_wrapper_removes_lock_after_run(tmp_path):
    run_wrapper(tmp_path)
    assert not (tmp_path / "logs" / "run.lock").exists()


def test_wrapper_skips_when_another_run_holds_the_lock(tmp_path):
    lock = tmp_path / "logs" / "run.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(os.getpid()))  # this test process is alive, so the lock is real

    result = run_wrapper(tmp_path)

    assert result.returncode == 0
    assert not (tmp_path / "ran").exists()
    assert lock.exists()  # the other run's lock is left alone
    assert "skipped: another run" in (tmp_path / "logs" / "daily.log").read_text()


def test_wrapper_clears_a_stale_lock_and_runs(tmp_path):
    lock = tmp_path / "logs" / "run.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(dead_pid()))

    result = run_wrapper(tmp_path)

    assert result.returncode == 0
    assert (tmp_path / "ran").exists()
    assert not lock.exists()
    assert "removing stale lock" in (tmp_path / "logs" / "daily.log").read_text()


def test_wrapper_rotates_a_big_log(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "daily.log").write_text("x" * 1_000_001)

    run_wrapper(tmp_path)

    assert (logs / "daily.log.1").stat().st_size == 1_000_001
    new_log = (logs / "daily.log").read_text()
    assert new_log.startswith("=== start ")
    assert "xxxx" not in new_log


def test_wrapper_keeps_a_small_log(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "daily.log").write_text("earlier run\n")

    run_wrapper(tmp_path)

    assert not (logs / "daily.log.1").exists()
    assert (logs / "daily.log").read_text().startswith("earlier run\n")
