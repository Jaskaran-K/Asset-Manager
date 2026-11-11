"""Report a Problem (Settings > About): what goes in the zip (core.problem_report), whether your settings do,
and where it's saved. Nothing is uploaded; Open a GitHub Issue opens the issue form in the browser."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QStandardPaths, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from core import problem_report
from ui import file_actions
from ui.tag_widgets import run_job


class ReportDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Report a Problem")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Makes a zip you can send with a bug report, or keep. <b>Nothing is uploaded.</b><br><br>"
            "In it: the app's and Windows' versions; your processor, memory and graphics card; how big the "
            "library is (counts only, no file or tag names); and the crash and session logs."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.chk_settings = QCheckBox("Include my settings (they have the paths of your library folders)", self)
        self.chk_settings.setToolTip("Helps with problems in scanning, profiles or categories. Off: settings stay out")
        layout.addWidget(self.chk_settings)
        self.lbl_status = QLabel("", self)
        self.lbl_status.setObjectName("pageHint")
        self.lbl_status.setWordWrap(True)
        layout.addWidget(self.lbl_status)
        buttons = QHBoxLayout()
        self.btn_save = QPushButton("Save Report...", self)
        self.btn_save.setObjectName("primaryBtn")
        self.btn_save.setToolTip("Choose where to save the zip, then show it in Explorer")
        self.btn_save.clicked.connect(self._on_save)
        self.btn_issue = QPushButton("Open a GitHub Issue", self)
        self.btn_issue.setToolTip("Open a new issue on GitHub in the browser, with the version filled in (attach the zip there)")
        self.btn_issue.clicked.connect(self._on_issue)
        close = QPushButton("Close", self)
        close.setToolTip("Close this window")
        close.clicked.connect(self.reject)
        buttons.addWidget(self.btn_save)
        buttons.addWidget(self.btn_issue)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.saved: Optional[Path] = None

    def suggested_path(self) -> Path:
        folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation) or str(Path.home())
        return Path(folder) / f"AssetManager-report-{time.strftime('%Y-%m-%d_%H%M')}.zip"

    def _on_save(self) -> None:
        chosen, _filter = QFileDialog.getSaveFileName(self, "Save the Report", str(self.suggested_path()), "Zip file (*.zip)")
        if chosen:
            self.save_to(Path(chosen))

    def save_to(self, target: Path) -> None:
        if target.suffix.lower() != ".zip":
            target = target.with_name(target.name + ".zip")
        include = self.chk_settings.isChecked()
        self.btn_save.setEnabled(False)
        self.lbl_status.setText("Making the report...")
        run_job(lambda: problem_report.make_report(target, include), self._on_saved, self._on_failed)

    def _on_saved(self, path: Path) -> None:
        self.saved = path
        self.btn_save.setEnabled(True)
        self.lbl_status.setText(f"Saved {path.name}.")
        file_actions.reveal(path)

    def _on_failed(self, error: str) -> None:
        self.btn_save.setEnabled(True)
        self.lbl_status.setText(f"It couldn't be saved: {error}")

    def _on_issue(self) -> None:
        QDesktopServices.openUrl(QUrl(problem_report.issue_url()))
