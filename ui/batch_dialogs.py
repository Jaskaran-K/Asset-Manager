"""Batch rename (core.batch_rename) and batch convert / resize (core.batch_convert): each shows what will
happen to every file, with what's in the way, before anything is changed."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import batch_convert, batch_rename
from core.scanner import AssetItem
from core.texture_sets import TextureSet
from ui.tag_widgets import run_job

PREVIEW_ROWS = 2000  # (the rest are done too; the list shows this many)
TOKENS = (("{name}", "The file's name now"), ("{set}", "Its texture set's name"), ("{map}", "What map it is (Base Color, Normal ...)"),
          ("{n}", "A counter (1, 2, 3; {n:3} = 001)"), ("{folder}", "The folder it's in"))


def _pattern_row(edit: QLineEdit, parent: QWidget) -> QHBoxLayout:
    """The pattern box with buttons that put a word in it."""
    row = QHBoxLayout()
    row.addWidget(edit, 1)
    for token, tip in TOKENS:
        button = QToolButton(parent)
        button.setText(token)
        button.setToolTip(f"Insert {token}: {tip}")
        button.clicked.connect(lambda _checked=False, t=token: edit.insert(t))
        row.addWidget(button)
    return row


class RenameDialog(QDialog):
    """Rename files by a pattern. accepted -> plan().renames are the renames to do."""

    def __init__(self, assets: Sequence[AssetItem], sets_of: Optional[dict[str, TextureSet]] = None,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.assets = list(assets)
        self.sets_of = sets_of or {}
        self._plan = batch_rename.RenamePlan()
        self.setWindowTitle(f"Rename {len(self.assets)} File{'s' if len(self.assets) != 1 else ''}")
        self.resize(860, 620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        form = QFormLayout()
        self.txt_pattern = QLineEdit("{set}_{map}" if self.sets_of else "{name}")
        self.txt_pattern.setToolTip("The new names: words in braces are filled in for each file (its type stays)")
        form.addRow("Pattern", _pattern_row(self.txt_pattern, self))
        find_row = QHBoxLayout()
        self.txt_find = QLineEdit()
        self.txt_find.setPlaceholderText("Find (letter case ignored)")
        self.txt_find.setToolTip("Text to change in the new names")
        self.txt_replace = QLineEdit()
        self.txt_replace.setPlaceholderText("Replace with")
        self.txt_replace.setToolTip("What it becomes (empty: it's taken out)")
        find_row.addWidget(self.txt_find)
        find_row.addWidget(self.txt_replace)
        form.addRow("Find and replace", find_row)
        options = QHBoxLayout()
        self.cmb_map = QComboBox()
        for key, label in batch_rename.MAP_STYLE_NAMES.items():
            self.cmb_map.addItem(label, key)
        self.cmb_map.setToolTip("How {map} writes what each map is")
        self.cmb_case = QComboBox()
        for key, label in (("keep", "Letter case as typed"), ("lower", "lower case"), ("upper", "UPPER CASE")):
            self.cmb_case.addItem(label, key)
        self.cmb_case.setToolTip("Letter case of the new names")
        self.spin_start = QSpinBox()
        self.spin_start.setRange(0, 99999)
        self.spin_start.setValue(1)
        self.spin_start.setToolTip("The number {n} starts at")
        options.addWidget(QLabel("Map words"))
        options.addWidget(self.cmb_map)
        options.addWidget(self.cmb_case)
        options.addWidget(QLabel("{n} starts at"))
        options.addWidget(self.spin_start)
        options.addStretch(1)
        form.addRow("", options)
        layout.addLayout(form)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Now", "Becomes", "Note"])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 290)
        self.tree.setColumnWidth(1, 290)
        layout.addWidget(self.tree, 1)
        self.lbl_summary = QLabel()
        self.lbl_summary.setObjectName("pageHint")
        self.lbl_summary.setWordWrap(True)
        layout.addWidget(self.lbl_summary)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setToolTip("Close without renaming anything")
        cancel.clicked.connect(self.reject)
        self.btn_ok = QPushButton("Rename")
        self.btn_ok.setObjectName("primaryBtn")
        self.btn_ok.setToolTip("Rename the files as listed (Ctrl+Z undoes it)")
        self.btn_ok.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(self.btn_ok)
        layout.addLayout(buttons)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self.update_preview)
        for edit in (self.txt_pattern, self.txt_find, self.txt_replace):
            edit.textChanged.connect(self._timer.start)
        for box in (self.cmb_map, self.cmb_case):
            box.currentIndexChanged.connect(self._timer.start)
        self.spin_start.valueChanged.connect(self._timer.start)
        self.update_preview()

    def options(self) -> batch_rename.RenameOptions:
        return batch_rename.RenameOptions(
            self.txt_pattern.text(), self.txt_find.text(), self.txt_replace.text(), self.cmb_map.currentData(),
            self.cmb_case.currentData(), self.spin_start.value(),
        )

    def plan(self) -> batch_rename.RenamePlan:
        return self._plan

    def update_preview(self) -> None:
        self._plan = batch_rename.plan(self.assets, self.options(), self.sets_of)
        self.tree.clear()
        for row in self._plan.rows[:PREVIEW_ROWS]:
            item = QTreeWidgetItem(self.tree, [row.asset.name, row.new_name, row.problem or ("" if row.changes else "Stays as it is")])
            item.setToolTip(0, str(row.asset.path))
            if row.problem:
                item.setForeground(2, Qt.GlobalColor.red)
        renames, problems = len(self._plan.renames), len(self._plan.problems)
        text = f"{renames} file{'s' if renames != 1 else ''} will be renamed."
        if problems:
            text += f" {problems} can't be (see Note): they stay as they are."
        self.lbl_summary.setText(text)
        self.btn_ok.setEnabled(renames > 0)


class ConvertDialog(QDialog):
    """Make converted / resized copies of pictures. They're made while it's open; created lists them
    (Move to Recycle Bin takes them back)."""

    def __init__(self, assets: Sequence[AssetItem], sets_of: Optional[dict[str, TextureSet]] = None,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.assets = [a for a in assets if a.is_texture]
        self.sets_of = sets_of or {}
        self.created: list[Path] = []
        self.failed: list[tuple[str, str]] = []
        self._plan = batch_convert.ConvertPlan()
        self._running = False
        self._stop = False
        self.setWindowTitle(f"Convert and Resize {len(self.assets)} Picture{'s' if len(self.assets) != 1 else ''}")
        self.resize(900, 660)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        form = QFormLayout()
        what = QHBoxLayout()
        self.cmb_format = QComboBox()
        self.cmb_format.addItem("Same type (others as PNG)", "same")
        for key, label in batch_convert.FORMAT_NAMES.items():
            self.cmb_format.addItem(label, key)
        self.cmb_format.setToolTip("The type of the new files")
        self.cmb_size = QComboBox()
        for side in batch_convert.SIZES:
            self.cmb_size.addItem(batch_convert.size_name(side) if side else "Full size", side)
        self.cmb_size.addItem("Custom...", -1)
        self.cmb_size.setToolTip("The longest side of the new files (pictures are never made bigger)")
        self.spin_size = QSpinBox()
        self.spin_size.setRange(16, 16384)
        self.spin_size.setValue(3000)
        self.spin_size.setSuffix(" px")
        self.spin_size.setToolTip("The longest side, in pixels")
        self.spin_size.setVisible(False)
        self.chk_pot = QCheckBox("Each side down to a power of two", self)
        self.chk_pot.setToolTip("512, 1024, 2048 ...: what game engines like")
        for widget in (self.cmb_format, self.cmb_size, self.spin_size, self.chk_pot):
            what.addWidget(widget)
        what.addStretch(1)
        form.addRow("Make", what)
        note = QLabel("PNG and TIF keep 16-bit heights and masks (and PNG 16-bit colour from PSD and EXR); JPG and TGA are "
                      "8-bit. EXR values are clipped to 0-1. The originals are never changed.")
        note.setObjectName("mutedLabel")
        note.setWordWrap(True)
        form.addRow("", note)

        where = QVBoxLayout()
        sub_row = QHBoxLayout()
        self.radio_sub = QRadioButton("In a folder next to each file, named", self)
        self.radio_sub.setChecked(True)
        self.radio_sub.setToolTip("A subfolder beside every picture (beside its archive, for one inside an archive)")
        self.txt_sub = QLineEdit("converted")
        self.txt_sub.setToolTip("The subfolder's name")
        sub_row.addWidget(self.radio_sub)
        sub_row.addWidget(self.txt_sub, 1)
        where.addLayout(sub_row)
        folder_row = QHBoxLayout()
        self.radio_folder = QRadioButton("All in one folder", self)
        self.radio_folder.setToolTip("Every new file in one folder you choose")
        self.txt_folder = QLineEdit()
        self.txt_folder.setPlaceholderText("Choose a folder")
        browse = QPushButton("Browse...")
        browse.setToolTip("Choose the folder for the new files")
        browse.clicked.connect(self._browse)
        folder_row.addWidget(self.radio_folder)
        folder_row.addWidget(self.txt_folder, 1)
        folder_row.addWidget(browse)
        where.addLayout(folder_row)
        form.addRow("Where", where)
        self.txt_pattern = QLineEdit("{name}")
        self.txt_pattern.setToolTip("Names of the new files (as in Rename: words in braces are filled in)")
        form.addRow("Names", _pattern_row(self.txt_pattern, self))
        self.chk_replace = QCheckBox("Replace files of the same name (never an original)", self)
        self.chk_replace.setToolTip("Write over files already in the target folder (from an earlier conversion)")
        form.addRow("", self.chk_replace)
        layout.addLayout(form)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Picture", "New file", "Note"])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 260)
        self.tree.setColumnWidth(1, 380)
        layout.addWidget(self.tree, 1)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.lbl_summary = QLabel()
        self.lbl_summary.setObjectName("pageHint")
        self.lbl_summary.setWordWrap(True)
        layout.addWidget(self.lbl_summary)
        buttons = QHBoxLayout()
        self.btn_trash = QPushButton("Move New Files to Recycle Bin")
        self.btn_trash.setToolTip("Take back what was made here (the new files go to the Recycle Bin)")
        self.btn_trash.clicked.connect(self._trash_created)
        self.btn_trash.setVisible(False)
        buttons.addWidget(self.btn_trash)
        buttons.addStretch(1)
        self.btn_close = QPushButton("Close")
        self.btn_close.setToolTip("Close (the new files stay)")
        self.btn_close.clicked.connect(self.accept)
        self.btn_ok = QPushButton("Convert")
        self.btn_ok.setObjectName("primaryBtn")
        self.btn_ok.setToolTip("Make the new files as listed")
        self.btn_ok.clicked.connect(self._start)
        buttons.addWidget(self.btn_close)
        buttons.addWidget(self.btn_ok)
        layout.addLayout(buttons)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self.update_preview)
        for edit in (self.txt_sub, self.txt_folder, self.txt_pattern):
            edit.textChanged.connect(self._timer.start)
        for box in (self.cmb_format,):
            box.currentIndexChanged.connect(self._timer.start)
        self.cmb_size.currentIndexChanged.connect(self._on_size)
        self.spin_size.valueChanged.connect(self._timer.start)
        for check in (self.chk_pot, self.chk_replace, self.radio_sub, self.radio_folder):
            check.toggled.connect(self._timer.start)
        self.update_preview()

    def _on_size(self) -> None:
        custom = self.cmb_size.currentData() == -1
        self.spin_size.setVisible(custom)
        side = self.cmb_size.currentData()
        if side and side > 0 and self.txt_sub.text() in ("converted", *(batch_convert.size_name(s) for s in batch_convert.SIZES if s)):
            self.txt_sub.setText(batch_convert.size_name(side))  # ("2K": a folder named after the size)
        self._timer.start()

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Folder for the New Files", self.txt_folder.text() or str(Path.home()))
        if chosen:
            self.txt_folder.setText(str(Path(chosen)))
            self.radio_folder.setChecked(True)

    def options(self) -> batch_convert.ConvertOptions:
        side = self.cmb_size.currentData()
        return batch_convert.ConvertOptions(
            self.cmb_format.currentData(), self.spin_size.value() if side == -1 else side, self.chk_pot.isChecked(),
            self.txt_folder.text().strip() if self.radio_folder.isChecked() else "", self.txt_sub.text().strip() or "converted",
            self.chk_replace.isChecked(),
        )

    def update_preview(self) -> None:
        if self._running:
            return
        names = None
        if self.txt_pattern.text().strip() not in ("", "{name}"):
            rename = batch_rename.RenameOptions(self.txt_pattern.text())
            names = [batch_rename.new_name(a, rename, n, self.sets_of.get(str(a.path))) for n, a in enumerate(self.assets, 1)]
        self._plan = batch_convert.plan(self.assets, self.options(), names)
        self.tree.clear()
        for row in self._plan.rows[:PREVIEW_ROWS]:
            item = QTreeWidgetItem(self.tree, [row.asset.name, str(row.target), row.problem])
            item.setToolTip(1, str(row.target))
            if row.problem:
                item.setForeground(2, Qt.GlobalColor.red)
        jobs, problems = len(self._plan.jobs), len(self._plan.rows) - len(self._plan.jobs)
        no_folder = self.radio_folder.isChecked() and not self.txt_folder.text().strip()
        text = f"{jobs} new file{'s' if jobs != 1 else ''} will be made."
        if problems:
            text += f" {problems} picture{'s' if problems != 1 else ''} won't be (see Note)."
        self.lbl_summary.setText("Choose the folder for the new files." if no_folder else text)
        self.btn_ok.setEnabled(jobs > 0 and not no_folder)

    # ------------------ making them ------------------
    def _start(self) -> None:
        jobs = list(self._plan.jobs)
        if not jobs:
            return
        self._running, self._stop = True, False
        self.created, self.failed = [], []
        for widget in (self.cmb_format, self.cmb_size, self.spin_size, self.chk_pot, self.txt_sub, self.txt_folder,
                       self.txt_pattern, self.chk_replace, self.radio_sub, self.radio_folder):
            widget.setEnabled(False)
        self.progress.setRange(0, len(jobs))
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.btn_ok.setText("Stop")
        self.btn_ok.setToolTip("Stop after the picture being made")
        self.btn_ok.clicked.disconnect(self._start)
        self.btn_ok.clicked.connect(self._request_stop)
        self._next(jobs, 0, self.options())

    def _request_stop(self) -> None:
        self._stop = True
        self.btn_ok.setEnabled(False)

    def _next(self, jobs: list, index: int, options: batch_convert.ConvertOptions) -> None:
        if self._stop or index >= len(jobs):
            self._finished(len(jobs))
            return
        row = jobs[index]
        self.lbl_summary.setText(f"Making {row.target.name} ({index + 1} of {len(jobs)})...")

        def done(_size) -> None:
            self.created.append(row.target)
            self.progress.setValue(index + 1)
            self._next(jobs, index + 1, options)

        def failed(message: str) -> None:
            self.failed.append((row.asset.name, message))
            self.progress.setValue(index + 1)
            self._next(jobs, index + 1, options)

        run_job(lambda: batch_convert.convert(row, options, lambda: self._stop), done, failed)

    def _finished(self, total: int) -> None:
        self._running = False
        self.progress.setVisible(False)
        self.btn_ok.setVisible(False)
        self.btn_trash.setVisible(bool(self.created))
        text = f"Made {len(self.created)} new file{'s' if len(self.created) != 1 else ''}"
        if self._stop and len(self.created) + len(self.failed) < total:
            text += f" (stopped: {total - len(self.created) - len(self.failed)} not made)"
        text += "."
        if self.failed:
            text += f" {len(self.failed)} couldn't be made: " + "; ".join(f"{name} ({why})" for name, why in self.failed[:5])
        self.lbl_summary.setText(text)

    def _trash_created(self) -> None:
        from PySide6.QtCore import QFile

        answer = QMessageBox.question(
            self, "Move to Recycle Bin", f"Move the {len(self.created)} new files to the Recycle Bin?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        kept = [path for path in self.created if path.exists() and not QFile.moveToTrash(str(path))]
        self.lbl_summary.setText(
            f"Moved {len(self.created) - len(kept)} new files to the Recycle Bin."
            + (f" {len(kept)} couldn't be moved." if kept else "")
        )
        self.created = kept
        self.btn_trash.setVisible(bool(kept))

    def reject(self) -> None:
        if self._running:
            self._request_stop()
            return
        super().reject()

    def done(self, result: int) -> None:
        if self._running:
            self._request_stop()
            return
        super().done(result)
