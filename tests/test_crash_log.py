"""core.crash_log: sessions that end abnormally are kept, clean ones leave nothing behind."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from core import crash_log


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    folder = tmp_path / "logs"
    folder.mkdir()
    monkeypatch.setattr(crash_log, "LOG_DIR", folder)
    return folder


def _run_child(log_dir, body: str) -> subprocess.CompletedProcess:
    """Run a session in a separate Python process (faulthandler and excepthooks are process-wide)."""
    script = textwrap.dedent(f"""
        import sys
        from core import crash_log
        from pathlib import Path
        crash_log.LOG_DIR = Path({str(log_dir)!r})
        crash_log.start_session("Asset Manager", "9.9.9")
    """) + textwrap.dedent(body)
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)


def test_clean_session_leaves_no_log(log_dir):
    result = _run_child(log_dir, "pass")
    assert result.returncode == 0, result.stderr
    assert list(log_dir.iterdir()) == []


def test_unhandled_error_is_logged_after_a_clean_exit(log_dir):
    _run_child(log_dir, "raise RuntimeError('boom in a slot')")
    text = crash_log.crash_log_path().read_text(encoding="utf-8")
    assert "Asset Manager 9.9.9" in text and "RuntimeError: boom in a slot" in text
    assert not list(log_dir.glob("session-*.log"))


def test_hard_crash_is_reported_on_the_next_launch(log_dir):
    result = _run_child(log_dir, "import faulthandler; faulthandler._sigsegv()")
    assert result.returncode != 0
    assert list(log_dir.glob("session-*.log")), "a crashed session leaves its log behind"
    assert crash_log.collect_previous_sessions() is True
    text = crash_log.crash_log_path().read_text(encoding="utf-8")
    assert "crashed or stopped responding" in text and "Fatal Python error" in text
    assert "File " in text  # with the Python stack of the crash
    assert crash_log.collect_previous_sessions() is False  # reported once


def test_hang_watchdog_dumps_stacks(log_dir, monkeypatch):
    _run_child(log_dir, """
        import os, time
        crash_log.HANG_SECONDS = 1
        crash_log.heartbeat()
        time.sleep(2.5)   # the UI thread "freezes"
        os._exit(1)       # then the user kills it
    """)
    assert crash_log.collect_previous_sessions() is True
    assert "Timeout (" in crash_log.crash_log_path().read_text(encoding="utf-8")


def test_killed_session_is_logged_without_a_notice(log_dir):
    _run_child(log_dir, "import os; os._exit(1)")
    assert crash_log.collect_previous_sessions() is False
    assert "did not close normally" in crash_log.crash_log_path().read_text(encoding="utf-8")


def test_running_instances_are_left_alone(log_dir):
    live = log_dir / "session-1.log"
    with open(live, "w", encoding="utf-8") as f:
        f.write("=== header ===\nWindows fatal exception\n")
        f.flush()
        assert crash_log.collect_previous_sessions() is False
    assert live.exists()
