"""Signals emitted from worker threads must be connected to methods, never to a lambda or partial.

PySide6 runs a lambda / functools.partial receiver on the thread that emitted the signal,
so a lambda connected to a worker's signal runs on the worker thread; touching widgets
there freezes or crashes the app (this caused the tag-search crash and the updater
freezing at 100%). A method defined with `def` on a QObject is queued to that object's
thread instead.

Convention the check relies on (see CLAUDE.md): objects that emit from other threads are
named `worker` / `*_worker`, or are a QRunnable's `.signals` holder.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted(p for p in [ROOT / "main.py", *(ROOT / "ui").rglob("*.py"), *(ROOT / "core").rglob("*.py")])
CROSS_THREAD_EMITTER = re.compile(r"worker|\bsignals\b", re.IGNORECASE)


def _is_unsafe_receiver(node: ast.AST) -> bool:
    if isinstance(node, ast.Lambda):
        return True
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
        return name == "partial"
    return False


def _violations(source: str, rel: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        # <emitter>.<signal>.connect(<receiver>)
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "connect"):
            continue
        signal = node.func.value
        if not isinstance(signal, ast.Attribute) or not node.args:
            continue
        emitter = ast.unparse(signal.value)
        if CROSS_THREAD_EMITTER.search(emitter) and _is_unsafe_receiver(node.args[0]):
            found.append(f"{rel}:{node.lineno}: {ast.unparse(signal)} connected to a lambda/partial")
    return found


def test_worker_signals_are_never_connected_to_lambdas():
    problems = [
        v for path in SOURCES for v in _violations(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)))
    ]
    assert not problems, (
        "Connect these to a method of a QObject on the UI thread instead (a lambda runs on the "
        "worker thread):\n" + "\n".join(problems)
    )


@pytest.mark.parametrize("snippet, bad", [
    ("worker.installed.connect(lambda c: self._on_installed(info, c))", True),
    ("self.scan_worker.finished.connect(partial(self._done, 1))", True),
    ("task.signals.done.connect(lambda r: print(r))", True),
    ("worker.installed.connect(self._on_installed)", False),
    ("self.btn_back.clicked.connect(lambda: self.skip(-1))", False),  # UI-thread signal: fine
])
def test_checker(snippet, bad):
    assert bool(_violations(snippet, "probe.py")) is bad
