"""Unzip the course: a folder's archives unzipped where they are, every file checked, then (if you like, and only
when every file checked out) the archives to the Recycle Bin. The work is core.course_unzip, one archive at a time
on a worker thread (ui.archive_jobs)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHeaderView, QLabel, QMessageBox, QProgressBar, QPushButton, QTreeWidget, QTreeWidgetItem,
    QWidget,
)

from core import course_unzip as unzip
from ui.archive_jobs import UnpackJob, unpack_in_background
from ui.controls import DialogFrame
from ui.duotone import duotone_icon
from ui.tag_widgets import run_job

COLUMNS = ("Archive", "What's happening", "Size")


class UnzipDialog(QDialog):
    """See the module docstring. exec(); afterwards .results (core.course_unzip.ArchiveResult each) and .trashed."""

    def __init__(self, folder: Path, archives: list[Path], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.folder = folder
        self.archives = archives
        self.plans: list[unzip.ArchivePlan] = []
        self.results: list[unzip.ArchiveResult] = []
        self.trashed = 0
        self._job: Optional[UnpackJob] = None
        self._at = 0
        self._running = False
        self.setWindowTitle("Unzip the course")
        self.resize(760, 560)
        frame = DialogFrame(self, "Unzip the course",
                            f"{len(archives)} archive{'s' if len(archives) != 1 else ''} in “{folder.name}”: each is "
                            "unzipped where it is, then every file is checked against it. Nothing is deleted unless "
                            "you say so, and only when every file checked out.")
        self.space = QLabel("Reading what's in them...", self)
        self.space.setObjectName("pageHint")
        self.space.setWordWrap(True)
        frame.body.addWidget(self.space)
        self.rows = QTreeWidget(self)
        self.rows.setRootIsDecorated(False)
        self.rows.setHeaderLabels(list(COLUMNS))
        self.rows.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.rows.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.rows.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        for path in archives:
            row = QTreeWidgetItem([path.name, "Waiting", ""])
            row.setToolTip(0, str(path))
            row.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.rows.addTopLevelItem(row)
        frame.body.addWidget(self.rows, 1)
        self.overall = QLabel("", self)
        frame.body.addWidget(self.overall)
        self.bar = QProgressBar(self)
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        frame.body.addWidget(self.bar)
        # (parented: a parentless check box handed to a layout can be freed under it)
        self.delete_after = QCheckBox(f"When every file checks out: move the archive{'s' if len(archives) != 1 else ''} "
                                      "to the Recycle Bin (asked again at the end)", self)
        self.delete_after.setToolTip("Only archives whose every file was unzipped and matches are offered")
        frame.body.addWidget(self.delete_after)
        footer = frame.add_footer("Unzip", "Unzip every archive here and check each file", "Cancel",
                                  "Stop (files already unzipped stay)", on_primary=self._start)
        self.btn_unzip: QPushButton = footer.btn_primary
        self.btn_unzip.setEnabled(False)
        self.btn_trash = QPushButton("Move the Archives to the Recycle Bin", self)
        self.btn_trash.setToolTip("Every file checked out: the archives can go (they can be restored from the Recycle Bin)")
        self.btn_trash.clicked.connect(self._offer_trash)
        self.btn_trash.hide()
        footer.add_extra(self.btn_trash)
        self.footer = footer
        run_job(lambda: unzip.plan(archives), self._on_planned, self._on_plan_failed)

    # ------------------ reading the archives ------------------
    def _on_planned(self, plans: list) -> None:
        self.plans = plans
        for n, item in enumerate(plans):
            row = self.rows.topLevelItem(n)
            row.setText(2, unzip.format_size(item.size) if not item.error else "")
            if item.error:
                row.setText(1, item.error)
                row.setIcon(1, duotone_icon("warning", "warning"))
            else:
                where = "here" if item.target == item.path.parent else f"into “{item.target.name}”"
                row.setText(1, f"Waiting ({len(item.members)} files, {where})")
        needed = unzip.space_needed(plans)
        free = unzip.free_space(self.folder)
        drive = self.folder.anchor.rstrip("\\") or str(self.folder)
        self.space.setText(f"{unzip.format_size(needed)} needed, {unzip.format_size(free)} free on {drive}."
                           + (" Not enough room: free some space first." if needed > free else ""))
        self.btn_unzip.setEnabled(any(not p.error for p in plans) and needed <= free)

    def _on_plan_failed(self, message: str) -> None:
        self.space.setText(f"Couldn't read the archives: {message}")

    # ------------------ unzipping, one at a time ------------------
    def _start(self) -> None:
        if self._running or not self.plans:
            return
        self._running = True
        self.btn_unzip.setEnabled(False)
        self.btn_unzip.setText("Unzipping…")
        self._at = 0
        self.results = []
        self._next()

    def _next(self) -> None:
        if self._at >= len(self.plans):
            self._finished()
            return
        item = self.plans[self._at]
        row = self.rows.topLevelItem(self._at)
        row.setText(1, "Unzipping…")
        row.setIcon(1, duotone_icon("extract"))
        self.rows.scrollToItem(row)
        self._job = unpack_in_background(lambda progress, stop: unzip.unzip(item, progress, stop),
                                         self._on_archive_done, self._on_archive_failed, self._on_progress)

    def _on_progress(self, done: int, total: int) -> None:
        row = self.rows.topLevelItem(self._at)
        if row is None or total <= 0:
            return
        half = total / 2
        row.setText(1, f"Unzipping… {int(100 * done / half)}%" if done < half else
                    f"Checking files… {int(100 * (done - half) / half)}%")
        whole = sum(max(1, p.size) for p in self.plans) or 1
        before = sum(max(1, p.size) for p in self.plans[:self._at])
        share = max(1, self.plans[self._at].size) * done / total
        self.bar.setValue(int(1000 * (before + share) / whole))

    def _on_archive_done(self, result: unzip.ArchiveResult) -> None:
        self.results.append(result)
        row = self.rows.topLevelItem(self._at)
        if result.verified:
            row.setText(1, f"Unzipped · {result.checked} file{'s' if result.checked != 1 else ''} checked")
            row.setIcon(1, duotone_icon("check", "success"))
        else:
            problems = result.failed + [f"{name}: a different file of that name was already there (yours is kept)" for name in result.kept]
            row.setText(1, f"{result.checked} of {result.files} checked · {len(problems)} problem{'s' if len(problems) != 1 else ''}")
            row.setIcon(1, duotone_icon("warning", "warning"))
            row.setToolTip(1, "\n".join(problems[:20]) + ("\n..." if len(problems) > 20 else ""))
        checked = sum(r.checked for r in self.results)
        files = sum(len(p.members) for p in self.plans)
        self.overall.setText(f"Overall: {checked} of {files} files unzipped and checked")
        self._at += 1
        self._next()

    def _on_archive_failed(self, message: str) -> None:
        self.results.append(unzip.ArchiveResult(self.plans[self._at].path, failed=[message]))
        row = self.rows.topLevelItem(self._at)
        row.setText(1, f"Couldn't unzip: {message}")
        row.setIcon(1, duotone_icon("warning", "warning"))
        self._at += 1
        self._next()

    def verified_archives(self) -> list[Path]:
        return [r.path for r in self.results if r.verified]

    def _finished(self) -> None:
        self._running = False
        self._job = None
        self.bar.setValue(1000)
        verified = self.verified_archives()
        bad = [r for r in self.results if not r.verified]
        self.btn_unzip.setText("Done")
        self.btn_unzip.setToolTip("Make the course from what was unzipped")
        self.btn_unzip.setEnabled(True)
        self.btn_unzip.clicked.disconnect()
        self.btn_unzip.clicked.connect(self.accept)
        if self.footer.btn_cancel is not None:
            self.footer.btn_cancel.hide()
        if bad:
            names = ", ".join(r.path.name for r in bad[:3]) + (" ..." if len(bad) > 3 else "")
            self.overall.setText(f"{self.overall.text()}. Not every file checked out in {names}: "
                                 "hover its row for why. Those archives aren't offered for deleting.")
        if verified and not bad and self.delete_after.isChecked():
            self._offer_trash()
        elif verified:
            self.btn_trash.setText(f"Move {len(verified)} Checked Archive{'s' if len(verified) != 1 else ''} to the Recycle Bin")
            self.btn_trash.show()

    def _offer_trash(self) -> None:
        verified = [p for p in self.verified_archives() if p.exists()]
        if not verified:
            return
        size = sum(sum(part.stat().st_size for part in unzip.archive_parts(p) if part.exists()) for p in verified)
        answer = QMessageBox.question(
            self, "Move to the Recycle Bin",
            f"Every file in {len(verified)} archive{'s' if len(verified) != 1 else ''} checked out. Move "
            f"{'them' if len(verified) != 1 else 'it'} to the Recycle Bin? That frees {unzip.format_size(size)}.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        moved, freed, failed = unzip.trash_archives(verified)
        self.trashed = moved
        self.btn_trash.hide()
        note = f" (couldn't move {', '.join(failed[:3])})" if failed else ""
        self.overall.setText(f"Moved {moved} archive{'s' if moved != 1 else ''} to the Recycle Bin, "
                             f"freeing {unzip.format_size(freed)}{note}.")

    def reject(self) -> None:
        if self._job is not None:
            self._job.cancel()
            self._job = None
        self._running = False
        super().reject()
