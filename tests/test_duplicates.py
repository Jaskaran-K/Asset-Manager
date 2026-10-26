"""Duplicate finder: identical content is grouped, look-alikes aren't, one copy is always kept."""

from __future__ import annotations

import os

import pytest

from core import duplicates
from core.duplicates import find_duplicates
from core.scanner import AssetScanner
from core.tag_repository import TagRepository


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "lib"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    big = os.urandom(300_000)
    (root / "a" / "rock.png").write_bytes(big)
    (root / "b" / "rock copy.png").write_bytes(big)
    (root / "b" / "rock again.png").write_bytes(big)
    # Same size and same first/last 64 KB, different middle: only the full hash tells them apart
    (root / "a" / "sneaky.png").write_bytes(big[:150_000] + b"X" + big[150_001:])
    (root / "a" / "tree.png").write_bytes(b"tree")
    (root / "b" / "tree2.png").write_bytes(b"tree")
    (root / "a" / "empty1.png").write_bytes(b"")
    (root / "b" / "empty2.png").write_bytes(b"")
    os.utime(root / "b" / "rock again.png", (1_600_000_000, 1_600_000_000))  # the oldest copy
    return root


def test_groups_identical_files(lib):
    groups = find_duplicates(AssetScanner().scan(lib))
    assert [sorted(a.name for a in g.files) for g in groups] == [
        ["rock again.png", "rock copy.png", "rock.png"],  # most wasted space first
        ["tree.png", "tree2.png"],
    ]
    assert groups[0].files[0].name == "rock again.png"  # oldest first
    assert groups[0].wasted == 2 * 300_000 and all(g.confirmed for g in groups)


def test_huge_files_are_only_likely(lib, monkeypatch):
    monkeypatch.setattr(duplicates, "FULL_HASH_LIMIT", 1000)
    groups = find_duplicates(AssetScanner().scan(lib))
    rock = next(g for g in groups if g.size == 300_000)
    assert not rock.confirmed and len(rock.files) == 4  # sneaky.png can't be told apart without a full read


def test_dialog_keeps_one_copy_and_recycles_the_rest(lib, qapp, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox

    from ui import duplicates_dialog

    assets = AssetScanner().scan(lib)
    dialog = duplicates_dialog.DuplicatesDialog(TagRepository(":memory:"), assets)
    dialog._show(find_duplicates(assets))

    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a[2])))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    trashed = []
    monkeypatch.setattr(duplicates_dialog.QFile, "moveToTrash", staticmethod(lambda p: trashed.append(p) or True))

    for _header, children in dialog._file_items():  # tick every copy: refused
        for child in children:
            child.setCheckState(0, Qt.CheckState.Checked)
    dialog._on_trash()
    assert trashed == [] and "Untick at least one" in warnings[0]

    dialog._tick_all_but_oldest()
    dialog._on_trash()
    assert sorted(os.path.basename(p) for p in trashed) == ["rock copy.png", "rock.png", "tree2.png"]
    assert dialog.removed == 3 and dialog.tree.topLevelItemCount() == 0
    dialog.deleteLater()
