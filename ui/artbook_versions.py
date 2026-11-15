"""Earlier Versions of an Artbook page (core.artbook_store keeps them: one before the first change of each
day the page changes, the last MAX_VERSIONS). Restore one in place (how the page is now becomes a version
first, so that can be undone the same way), or as a new page beside it."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
)

from core import artbook_store as store
from ui.controls import DialogFrame, ghost_button

if TYPE_CHECKING:
    from ui.artbook import ArtbookPanel

THUMB = QSize(160, 120)


def when_text(when: float, now: Optional[float] = None) -> str:
    """"Today, 20:15", "Yesterday, 09:02" or "Monday 9 November 2026, 21:30"."""
    now = time.time() if now is None else now
    day = time.localtime(when)
    today = time.localtime(now)
    clock = time.strftime("%H:%M", day)
    if day[:3] == today[:3]:
        return f"Today, {clock}"
    if day[:3] == time.localtime(now - 86400)[:3]:
        return f"Yesterday, {clock}"
    return f"{time.strftime('%A', day)} {day.tm_mday} {time.strftime('%B %Y', day)}, {clock}"


def counts_text(version: dict[str, Any]) -> str:
    images, notes = version["images"], version["notes"]
    parts = [f"{images} image{'s' if images != 1 else ''}"]
    if notes:
        parts.append(f"{notes} note{'s' if notes != 1 else ''}")
    return ", ".join(parts)


class VersionsDialog(QDialog):
    def __init__(self, artbook: "ArtbookPanel", page_id: str, name: str):
        super().__init__(artbook)
        self.artbook = artbook
        self.page_id = page_id
        self.setWindowTitle(f"Earlier Versions - {name}")
        self.resize(560, 560)
        frame = DialogFrame(
            self, f"Earlier Versions of {name}",
            f"A version is kept before the first change each day the page changes (the last {store.MAX_VERSIONS}). "
            "Restoring keeps how the page is now as a version too, so you can go back.",
        )
        layout = frame.body
        self.list = QListWidget(self)
        self.list.setIconSize(THUMB)
        self.list.setSpacing(4)
        self.list.currentRowChanged.connect(self._update_buttons)
        self.list.itemDoubleClicked.connect(lambda _item: self._restore(as_new=False))
        layout.addWidget(self.list, 1)
        self.lbl_empty = QLabel(
            "No earlier versions yet. One is kept the first time the page changes on a day.", self,
        )
        self.lbl_empty.setWordWrap(True)
        self.lbl_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_empty)

        footer = frame.add_footer("Restore This Version", "Make the page as it was then (how it is now is kept as a version)",
                                  cancel="Close", on_primary=lambda: self._restore(as_new=False))
        self.btn_restore = footer.btn_primary
        self.btn_restore.setDefault(False)
        self.btn_new = footer.add_extra(ghost_button(
            "new_page", "Make a new page from this version, beside this one (this page stays as it is)",
            "Restore as a New Page",
        ))
        self.btn_new.clicked.connect(lambda: self._restore(as_new=True))
        self._fill()

    def _fill(self) -> None:
        self.versions = store.list_versions(self.page_id)
        self.list.clear()
        for version in self.versions:
            entry = QListWidgetItem(f"{when_text(version['time'])}\n{counts_text(version)}", self.list)
            if version["thumbnail"] is not None:
                pixmap = QPixmap(str(version["thumbnail"]))
                if not pixmap.isNull():
                    entry.setIcon(QIcon(pixmap.scaled(THUMB, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)))
            entry.setToolTip(f"Named {version['name']} then" if version["name"] else "")
        self.list.setVisible(bool(self.versions))
        self.lbl_empty.setVisible(not self.versions)
        if self.versions:
            self.list.setCurrentRow(0)
        self._update_buttons()

    def _update_buttons(self, *_args) -> None:
        chosen = 0 <= self.list.currentRow() < len(self.versions)
        self.btn_restore.setEnabled(chosen)
        self.btn_new.setEnabled(chosen)

    def _restore(self, as_new: bool) -> None:
        row = self.list.currentRow()
        if not 0 <= row < len(self.versions):
            return
        version = self.versions[row]
        if self.artbook.restore_version(self.page_id, version["stamp"], as_new=as_new):
            self.artbook.board.show_message(
                f"Made a new page from the version of {when_text(version['time'])}." if as_new else
                f"Restored the version of {when_text(version['time'])}. How it was before is in Earlier Versions."
            )
            self.accept()
