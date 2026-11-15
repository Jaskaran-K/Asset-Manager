"""Asking before an AI tool is downloaded (core.ai_tools), then downloading it with its progress shown."""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QDialog, QLabel, QProgressBar, QWidget

from core import ai_tools
from ui.archive_jobs import UnpackJob, unpack_in_background
from ui.controls import DialogFrame, section_heading
from ui.duotone import set_icon

WHAT_THEY_DO = {
    "search": "AI Search finds files by what's in their pictures: describe what you're looking for. It also gives "
              "More Like This and tag suggestions. Your files are read once, in the background (models, videos and "
              "PDFs by their thumbnails).",
}
for _model in ai_tools.CUTOUT_MODELS:
    WHAT_THEY_DO[_model] = ("Remove Background cuts the subject out of a picture (a render, a photo of an object, a character). "
                            f"{ai_tools.TOOLS[_model].about}.")


class AIDownloadDialog(QDialog):
    """Say what an AI tool is and how big it is; Download downloads it (Cancel stops it)."""

    def __init__(self, key: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.key = key
        tool = ai_tools.TOOLS[key]
        self._job: Optional[UnpackJob] = None
        model = f" ({tool.model.split(' (')[0]})" if key in ai_tools.CUTOUT_MODELS else ""
        self.setWindowTitle(f"Download {tool.name}{model}")
        self.setMinimumWidth(480)
        size = ai_tools.needed_bytes(key) / 1e6
        runtime = "" if ai_tools.runtime_installed() else ", with ONNX Runtime, which runs the AI tools"
        frame = DialogFrame(self, f"Download {tool.name}{model}", WHAT_THEY_DO[key])
        layout = frame.body
        layout.addWidget(section_heading("What's downloaded"))
        text = QLabel(
            f"{tool.model}, once ({size:.0f} MB{runtime}). It runs on this computer, on the graphics card when it can: "
            "nothing is sent anywhere. Settings > Storage removes it."
        )
        text.setWordWrap(True)
        layout.addWidget(text)
        self.status = QLabel("")
        self.status.setObjectName("pageHint")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.hide()
        layout.addWidget(self.progress)
        footer = frame.add_footer(f"Download ({size:.0f} MB)", f"Download {tool.name} now",
                                  cancel_tip="Don't download it (or stop the download)", on_primary=self.start)
        self.btn_cancel, self.btn_download = footer.btn_cancel, footer.btn_primary
        set_icon(self.btn_download, "ai" if key == "search" else "cutout", "on_accent")
        if not ai_tools.supported():
            self.status.setText("The AI tools aren't available for this version of the app.")
            self.btn_download.setEnabled(False)

    def start(self) -> None:
        self.btn_download.setEnabled(False)
        self.progress.setValue(0)
        self.progress.show()
        self.status.setText("Downloading...")
        self._job = UnpackJob()
        key = self.key
        unpack_in_background(lambda progress, stop: ai_tools.install([key], progress, stop),
                             self._on_done, self._on_failed, self._on_progress, self._job)

    def _on_progress(self, done: int, total: int) -> None:
        self.progress.setValue(int(1000 * done / max(total, 1)))
        self.status.setText(f"Downloading: {done / 1e6:.0f} of {total / 1e6:.0f} MB")

    def _on_done(self, _result) -> None:
        self._job = None
        self.accept()

    def _on_failed(self, message: str) -> None:
        self._job = None
        self.progress.hide()
        self.status.setText(message)
        self.btn_download.setEnabled(True)
        self.btn_download.setText("Try Again")

    def done(self, result: int) -> None:
        if self._job is not None:
            self._job.cancel()
            self._job = None
        super().done(result)


def ensure_tool(key: str, parent: Optional[QWidget] = None) -> bool:
    """True if the tool is there, downloading it first if the user agrees."""
    if ai_tools.is_installed(key):
        return True
    dialog = AIDownloadDialog(key, parent)
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    dialog.deleteLater()
    return accepted and ai_tools.is_installed(key)
