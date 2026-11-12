"""Tools > Find Duplicates: lists identical files and moves chosen copies to the Recycle Bin. Tools > Find
Look-alikes does the same for pictures that look the same (core.picture_look: resized or re-saved copies)."""

from __future__ import annotations

from typing import Optional, Sequence

from PySide6.QtCore import QFile, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.duplicates import DuplicateGroup, find_duplicates
from core.picture_look import Look, group_lookalikes
from core.scanner import AssetItem
from core.tag_repository import TagRepository
from ui import file_actions
from ui.tag_widgets import run_in_background

_ASSET_ROLE = Qt.ItemDataRole.UserRole


def format_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def lookalike_groups(looks: Sequence[tuple[AssetItem, Look]], cancelled=lambda: False) -> list[DuplicateGroup]:
    """Look-alike pictures (looks: (picture, its look)) as groups, the biggest file first (usually the copy
    worth keeping)."""
    groups = group_lookalikes({i: look for i, (_asset, look) in enumerate(looks)}, cancelled)
    result = []
    for group in groups:
        files = sorted((looks[i][0] for i in group), key=lambda a: (-a.size_bytes, a.modified_timestamp))
        result.append(DuplicateGroup(files, files[0].size_bytes, True))
    result.sort(key=lambda g: sum(a.size_bytes for a in g.files[1:]), reverse=True)
    return result


class DuplicatesDialog(QDialog):
    """looks: (picture, its look): find look-alikes among them instead of identical files."""

    def __init__(self, repo: TagRepository, assets: Sequence[AssetItem], parent: Optional[QWidget] = None,
                 looks: Optional[Sequence[tuple[AssetItem, Look]]] = None):
        super().__init__(parent)
        self.lookalikes = looks is not None
        self.setWindowTitle("Find Look-alikes" if self.lookalikes else "Find Duplicates")
        self.resize(860, 560)
        self.removed = 0
        self._closed = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        self.lbl_summary = QLabel(
            f"Comparing {len(looks):,} pictures..." if self.lookalikes else f"Comparing {len(assets):,} files..."
        )
        self.lbl_summary.setObjectName("pageHint")
        self.lbl_summary.setWordWrap(True)
        layout.addWidget(self.lbl_summary)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["File", "Modified", "Folder"])
        self.tree.setColumnWidth(0, 300)
        self.tree.setColumnWidth(1, 130)
        self.tree.itemDoubleClicked.connect(self._on_double_clicked)
        self.tree.itemChanged.connect(self._update_buttons)
        self.tree.currentItemChanged.connect(self._update_buttons)
        layout.addWidget(self.tree, 1)

        buttons = QHBoxLayout()
        self.btn_keep_oldest = QPushButton("Tick Copies (Keep Biggest)" if self.lookalikes else "Tick Copies (Keep Oldest)")
        self.btn_keep_oldest.setToolTip(
            "Tick every picture except the biggest file in each group (usually the best copy)" if self.lookalikes
            else "Tick every file except the oldest one in each group"
        )
        self.btn_keep_oldest.clicked.connect(self._tick_all_but_oldest)
        self.btn_reveal = QPushButton("Show in File Explorer")
        self.btn_reveal.setToolTip("Show the highlighted file in File Explorer")
        self.btn_reveal.clicked.connect(self._reveal_current)
        self.btn_trash = QPushButton("Move Ticked to Recycle Bin")
        self.btn_trash.setObjectName("primaryBtn")
        self.btn_trash.setToolTip("You can restore them from the Recycle Bin. One copy of every file is always kept.")
        self.btn_trash.clicked.connect(self._on_trash)
        btn_close = QPushButton("Close")
        btn_close.setToolTip("Close (files moved to the Recycle Bin can be restored from there)")
        btn_close.clicked.connect(self.accept)
        for btn in (self.btn_keep_oldest, self.btn_reveal):
            buttons.addWidget(btn)
        buttons.addStretch(1)
        buttons.addWidget(self.btn_trash)
        buttons.addWidget(btn_close)
        layout.addLayout(buttons)
        self._update_buttons()

        if self.lookalikes:
            pictures = list(looks)
            run_in_background(
                repo, lambda _repo: lookalike_groups(pictures, cancelled=lambda: self._closed), self._show, self._on_failed
            )
        else:
            snapshot = list(assets)
            run_in_background(
                repo, lambda _repo: find_duplicates(snapshot, cancelled=lambda: self._closed), self._show, self._on_failed
            )

    def done(self, result: int) -> None:
        self._closed = True  # stops the search if it's still running
        super().done(result)

    # Results ------------------------------------------------------------
    def _show(self, groups: list[DuplicateGroup]) -> None:
        if self._closed:
            return
        self.tree.blockSignals(True)
        bold = QFont()
        bold.setBold(True)
        for group in groups:
            if self.lookalikes:
                extra = sum(a.size_bytes for a in group.files[1:])
                label = f"{len(group.files)} look-alikes • {format_bytes(extra)} besides the biggest"
            else:
                label = f"{len(group.files)} copies • {format_bytes(group.size)} each • {format_bytes(group.wasted)} wasted"
            if not group.confirmed:
                label += " • likely (very large files are compared by size and samples)"
            header = QTreeWidgetItem(self.tree, [label])
            header.setFirstColumnSpanned(True)
            header.setFont(0, bold)
            header.setFlags(Qt.ItemFlag.ItemIsEnabled)
            for asset in group.files:
                when = f"{asset.formatted_size} • {asset.formatted_modified_time}" if self.lookalikes else asset.formatted_modified_time
                item = QTreeWidgetItem(header, [asset.name, when, str(asset.path.parent)])
                item.setData(0, _ASSET_ROLE, asset)
                item.setToolTip(2, str(asset.path))
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Unchecked)
            header.setExpanded(True)
        self.tree.blockSignals(False)
        if groups and self.lookalikes:
            self.lbl_summary.setText(
                f"Found {len(groups)} group{'s' if len(groups) != 1 else ''} of pictures that look the same (resized or "
                "re-saved copies, or very close versions), the biggest file first. Check before recycling: a turntable's "
                "frames or an alpha's thumbnail look alike too. Double-click a file to open it."
            )
        elif groups:
            wasted = sum(g.wasted for g in groups)
            self.lbl_summary.setText(
                f"Found {len(groups)} group{'s' if len(groups) != 1 else ''} of identical files, "
                f"using {format_bytes(wasted)} more than needed. Tick the copies you don't need. "
                "Double-click a file to open it."
            )
        else:
            self.lbl_summary.setText("No look-alike pictures found." if self.lookalikes else "No duplicate files found.")
        self._update_buttons()

    def _on_failed(self, message: str) -> None:
        if not self._closed:
            self.lbl_summary.setText(f"Couldn't compare the files: {message}")

    # Helpers -------------------------------------------------------------
    def _file_items(self):
        for i in range(self.tree.topLevelItemCount()):
            header = self.tree.topLevelItem(i)
            yield header, [header.child(j) for j in range(header.childCount())]

    def _ticked(self) -> list[QTreeWidgetItem]:
        return [c for _h, children in self._file_items() for c in children if c.checkState(0) == Qt.CheckState.Checked]

    def _update_buttons(self, *_args) -> None:
        self.btn_trash.setEnabled(bool(self._ticked()))
        self.btn_keep_oldest.setEnabled(self.tree.topLevelItemCount() > 0)
        current = self.tree.currentItem()
        self.btn_reveal.setEnabled(current is not None and current.data(0, _ASSET_ROLE) is not None)

    def _tick_all_but_oldest(self) -> None:
        self.tree.blockSignals(True)
        for _header, children in self._file_items():
            for index, child in enumerate(children):
                child.setCheckState(0, Qt.CheckState.Unchecked if index == 0 else Qt.CheckState.Checked)
        self.tree.blockSignals(False)
        self._update_buttons()

    def _on_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        asset = item.data(0, _ASSET_ROLE)
        if asset is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(asset.path)))

    def _reveal_current(self) -> None:
        item = self.tree.currentItem()
        asset = item.data(0, _ASSET_ROLE) if item else None
        if asset is None:
            return
        file_actions.reveal(asset.path)

    # Recycle Bin -----------------------------------------------------------
    def _on_trash(self) -> None:
        for _header, children in self._file_items():
            if children and all(c.checkState(0) == Qt.CheckState.Checked for c in children):
                QMessageBox.warning(
                    self, "Keep One Copy",
                    f'Every copy of "{children[0].text(0)}" is ticked. Untick at least one so a copy is kept.',
                )
                return
        ticked = self._ticked()
        total = sum(c.data(0, _ASSET_ROLE).size_bytes for c in ticked)
        answer = QMessageBox.question(
            self, "Move to Recycle Bin",
            f"Move {len(ticked)} file{'s' if len(ticked) != 1 else ''} ({format_bytes(total)}) to the Recycle Bin?\n\n"
            "You can restore them from the Recycle Bin.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        failed = []
        for item in ticked:
            asset: AssetItem = item.data(0, _ASSET_ROLE)
            if QFile.moveToTrash(str(asset.path)):
                self.removed += 1
                parent = item.parent()
                parent.removeChild(item)
                if parent.childCount() < 2:  # no longer a duplicate
                    self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(parent))
            else:
                failed.append(asset.name)
        if failed:
            QMessageBox.warning(
                self, "Some Files Weren't Moved",
                "These files couldn't be moved to the Recycle Bin (in use, or on a drive without one):\n"
                + "\n".join(failed[:20]),
            )
        self.lbl_summary.setText(
            f"Moved {self.removed} file{'s' if self.removed != 1 else ''} to the Recycle Bin. "
            + ("No duplicates left." if not self.tree.topLevelItemCount() else "")
        )
        self._update_buttons()
