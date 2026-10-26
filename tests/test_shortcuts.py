"""Keeps ui/shortcuts.py the single source of truth for shortcuts.

If one of these fails after adding a shortcut: register it in ui/shortcuts.py and bind it
with shortcuts.sequence("id") / shortcuts.matches("id", event) instead of a hard-coded key.
It then shows up on Settings > General > Shortcuts automatically.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from PySide6.QtGui import QKeySequence

from ui import shortcuts

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "ui" / "shortcuts.py"
SOURCES = sorted(
    p for p in [ROOT / "main.py", *(ROOT / "ui").rglob("*.py"), *(ROOT / "core").rglob("*.py")]
    if p != REGISTRY
)
REGISTRY_FUNCS = {"sequence", "matches", "get"}


def _is_registry_call(node: ast.AST) -> bool:
    """shortcuts.sequence(...) / shortcuts.matches(...) / shortcuts.get(...)"""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in REGISTRY_FUNCS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "shortcuts"
    )


def _call_name(node: ast.Call) -> str:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel = path.relative_to(ROOT)
    found = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", "?")
        # Qt.Key.Key_X / Qt.Key_X: a hard-coded key check
        if isinstance(node, ast.Attribute) and node.attr.startswith("Key_"):
            found.append(f"{rel}:{line}: hard-coded key {node.attr} (use shortcuts.matches)")
        if isinstance(node, ast.Call):
            name = _call_name(node)
            # setShortcut / setShortcuts / QShortcut(...) must take shortcuts.sequence("id")
            if name in ("setShortcut", "setShortcuts", "QShortcut") and node.args:
                if not _is_registry_call(node.args[0]):
                    found.append(f"{rel}:{line}: {name}() without shortcuts.sequence(\"id\")")
            # QKeySequence("Ctrl+X") or QKeySequence(QKeySequence.StandardKey...) built outside the registry
            if name == "QKeySequence":
                found.append(f"{rel}:{line}: QKeySequence built outside ui/shortcuts.py")
    return found


def _used_ids() -> dict[str, list[str]]:
    """Shortcut ids referenced in code -> where.

    Counts ids passed straight to shortcuts.sequence/matches/get, and registered ids written
    as string constants elsewhere (e.g. a table of ids that a loop binds).
    """
    registered = {s.id for s in shortcuts.SHORTCUTS}
    used: dict[str, list[str]] = {}
    for path in SOURCES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            where = f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', '?')}"
            if _is_registry_call(node) and node.args and isinstance(node.args[0], ast.Constant):
                used.setdefault(node.args[0].value, []).append(where)
            elif isinstance(node, ast.Constant) and node.value in registered:
                used.setdefault(node.value, []).append(where)
    return used


def test_no_hard_coded_shortcuts_outside_registry():
    problems = [v for path in SOURCES for v in _violations(path)]
    assert not problems, "Register these in ui/shortcuts.py:\n" + "\n".join(problems)


def test_code_only_uses_registered_ids():
    unknown = {i: where for i, where in _used_ids().items() if i not in {s.id for s in shortcuts.SHORTCUTS}}
    assert not unknown, f"Unregistered shortcut ids: {unknown}"


def test_every_key_shortcut_is_wired_up():
    """Each registered key shortcut is actually bound somewhere, so the Shortcuts page never lists dead keys."""
    used = _used_ids()
    unused = [s.id for s in shortcuts.SHORTCUTS if s.sequence is not None and s.id not in used]
    assert not unused, f"Registered but never bound: {unused} (bind them, or set sequence=None for gestures)"


def test_registry_is_well_formed():
    ids = [s.id for s in shortcuts.SHORTCUTS]
    assert len(ids) == len(set(ids))
    for s in shortcuts.SHORTCUTS:
        assert s.category in shortcuts.CATEGORIES, s.id
        assert s.keys and s.action, s.id
        if s.sequence is not None:
            assert not QKeySequence(s.sequence).isEmpty(), f"{s.id}: unparseable sequence {s.sequence!r}"


def test_no_conflicting_keys_within_an_area():
    seen: dict[tuple[str, str], str] = {}
    for s in shortcuts.SHORTCUTS:
        if s.sequence is None:
            continue
        key = (s.category, QKeySequence(s.sequence).toString())
        assert key not in seen, f"{s.id} and {seen[key]} both use {s.sequence} in {s.category}"
        seen[key] = s.id


def test_every_category_listed_on_the_page_has_shortcuts():
    assert all(items for _cat, items in shortcuts.by_category())


@pytest.mark.parametrize("snippet, expected", [
    ('if event.key() == Qt.Key.Key_F2: pass', "hard-coded key"),
    ('btn.setShortcut("Ctrl+K")', "setShortcut()"),
    ('QShortcut(QKeySequence("Ctrl+K"), self)', "QShortcut()"),
    ('btn.setShortcut(shortcuts.sequence("main.tag"))', None),
])
def test_checker_catches_hard_coded_keys(tmp_path, snippet, expected):
    """The checker itself works (so a passing suite really means 'all shortcuts registered')."""
    probe = ROOT / "ui" / "_probe_shortcut_check.py"
    probe.write_text(snippet + "\n", encoding="utf-8")
    try:
        problems = _violations(probe)
    finally:
        probe.unlink()
    if expected is None:
        assert not problems
    else:
        assert any(expected in p for p in problems), problems
