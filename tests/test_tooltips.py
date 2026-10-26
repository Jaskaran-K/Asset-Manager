"""Every button gets a hover tooltip (see CLAUDE.md).

A button counts as having one if setToolTip is called on it anywhere in the same class
(or function, for local buttons). Factory helpers whose callers set the tooltip are listed
in FACTORIES.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted([ROOT / "main.py", *(ROOT / "ui").rglob("*.py")])
BUTTON_CLASSES = {"QPushButton", "QToolButton", "AnimatedButton", "PlayPauseButton"}
FACTORIES = {"media_button"}  # returns a button; every caller sets its tooltip


def _call_name(call: ast.Call) -> str:
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def _missing(source: str, rel: str) -> list[str]:
    tree = ast.parse(source)
    problems = []

    def walk(scope: ast.AST):
        """ast.walk, but not into factory helpers (their buttons get tooltips from the callers)."""
        todo = [scope]
        while todo:
            node = todo.pop()
            yield node
            todo.extend(
                child for child in ast.iter_child_nodes(node)
                if not (isinstance(child, ast.FunctionDef) and child.name in FACTORIES)
            )

    def check(scope: ast.AST, own_functions_only: bool) -> None:
        created: dict[str, int] = {}
        tipped: set[str] = set()
        for node in walk(scope):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                name = _call_name(node.value)
                target = ast.unparse(node.targets[0])
                is_attr = target.startswith("self.")
                if (name in BUTTON_CLASSES or name in FACTORIES) and is_attr != own_functions_only:
                    created.setdefault(target, node.lineno)
            if isinstance(node, ast.Call) and _call_name(node) == "setToolTip" and isinstance(node.func, ast.Attribute):
                tipped.add(ast.unparse(node.func.value))
        problems.extend(f"{rel}:{line}: {target}" for target, line in created.items() if target not in tipped)

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            check(node, own_functions_only=False)  # self.btn_x: tooltip anywhere in the class
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name not in FACTORIES:
            check(node, own_functions_only=True)  # local buttons: tooltip in the same function
    return problems


def test_every_button_has_a_tooltip():
    problems = [p for path in SOURCES for p in _missing(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)))]
    assert not problems, "Give these buttons a tooltip (setToolTip):\n" + "\n".join(problems)


@pytest.mark.parametrize("snippet, missing", [
    ("def f():\n    b = QPushButton('Go')\n", True),
    ("def f():\n    b = QPushButton('Go')\n    b.setToolTip('Start')\n", False),
    ("class W:\n    def a(self):\n        self.b = QPushButton()\n    def c(self):\n        self.b.setToolTip('x')\n", False),
    ("class W:\n    def a(self):\n        self.b = AnimatedButton()\n", True),
])
def test_checker(snippet, missing):
    assert bool(_missing(snippet, "probe.py")) is missing
