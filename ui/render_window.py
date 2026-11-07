"""The 3D viewer's Render window: what's on screen as a picture, by LuxCore or by the viewer itself.

LuxCore gives real light (cast shadows, light between parts of the model), downloads once (core.render_engine)
and runs in a process of its own (core.render_worker), watched through files in its folder. The viewer's own
renderer needs nothing and is instant: the same picture as on screen, at any size, without cast shadows.
It's the way out when LuxCore can't run on a computer.
"""

from __future__ import annotations

import json
import shutil
import sys
import uuid
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QProcess, QStandardPaths, Qt, QTimer, Slot
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core import environment, render_engine, render_scene, render_worker
from core.model_look import LIGHTINGS
from core.settings import get_render_settings, set_render_settings
from ui.archive_jobs import UnpackJob, unpack_in_background
from ui.duotone import set_icon

POLL_MS = 200
LUXCORE, VIEWER = "luxcore", "viewer"


def worker_command(folder: Path) -> tuple[str, list[str]]:
    """How to start a render: the app itself, with --render (tests replace this)."""
    if getattr(sys, "frozen", False):
        return sys.executable, ["--render", str(folder)]
    return sys.executable, [str(Path(__file__).resolve().parent.parent / "main.py"), "--render", str(folder)]


def build_job(viewport, model: Path) -> render_scene.RenderJob:
    """The job for what the viewport shows now, without its size or time."""
    color = (viewport.color.redF(), viewport.color.greenF(), viewport.color.blueF())
    eye, target, up = render_scene.camera(viewport.rot_x, viewport.rot_y, viewport.pan_x, viewport.pan_y, viewport.zoom)
    material, textures = render_scene.surface(viewport.material_id, color, viewport.roughness)
    job = render_scene.RenderJob(str(model), 0, 0, 0.0, True, eye, target, up, material, textures)
    env = viewport.environment
    if env is not None:
        job.hdri = str(environment.local_path(env.key))
        job.sky_matrix = render_scene.sky_matrix(viewport.light_yaw)
        job.exposure = env.exposure * 2.0 ** viewport.exposure_stops
        job.backdrop = viewport.show_backdrop
    else:
        job.lights, job.ambient = render_scene.preset_lights(
            LIGHTINGS[viewport.lighting_id], viewport.rot_x, viewport.rot_y, viewport.light_yaw, viewport.light_pitch,
            metal=viewport.material_id == "metallic",
        )
        job.exposure = render_scene.PRESET_EXPOSURE
    return job


class _Preview(QLabel):
    """The picture, fitted to the room there is."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._pixmap: Optional[QPixmap] = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setMinimumSize(640, 360)
        self.setStyleSheet("background: #14171c; color: #9ca3af;")

    def show_pixmap(self, pixmap: Optional[QPixmap]) -> None:
        self._pixmap = pixmap
        self._fit()

    def _fit(self) -> None:
        if self._pixmap is None or self._pixmap.isNull():
            self.clear()
            return
        self.setPixmap(self._pixmap.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()


class RenderWindow(QDialog):
    def __init__(self, viewport, model: Path, name: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.viewport = viewport
        self.model = model
        self.name = name
        self.setWindowTitle(f"Render - {name}")
        self.resize(980, 720)
        self.process: Optional[QProcess] = None
        self.folder: Optional[Path] = None
        self._download: Optional[UnpackJob] = None
        self._image: Optional[QImage] = None  # the viewer's renderer's result
        self._result: Optional[Path] = None  # LuxCore's
        self._saved_size = ""
        self._preview_seen = 0
        self._stopping = False
        self._seconds = 1.0
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._clean_old_jobs()
        self._build()
        self._refresh()

    # ------------------ the window ------------------
    def _build(self) -> None:
        settings = get_render_settings()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.preview = _Preview(self)
        layout.addWidget(self.preview, 1)

        bar = QWidget(self)
        bar.setObjectName("viewerToolbar")
        rows = QVBoxLayout(bar)
        rows.setContentsMargins(12, 8, 12, 10)
        rows.setSpacing(8)
        layout.addWidget(bar)

        options = QHBoxLayout()
        options.setSpacing(6)
        rows.addLayout(options)

        def heading(text: str) -> None:
            label = QLabel(text, bar)
            label.setObjectName("toolbarLabel")
            options.addWidget(label)

        def choices(values: list[tuple[str, str, str]], current: str) -> QButtonGroup:
            group = QButtonGroup(bar)
            group.setExclusive(True)
            for value, text, tip in values:
                button = QPushButton(text, bar)
                button.setCheckable(True)
                button.setChecked(value == current)
                button.setToolTip(tip)
                button.setProperty("value", value)
                group.addButton(button)
                options.addWidget(button)
            return group

        heading("Renderer")
        self.engine_choice = choices([
            (LUXCORE, "LuxCore", "Real light: cast shadows and light between parts of the model. Downloads once"),
            (VIEWER, "Viewer", "The picture on screen, at this size: instant, needs nothing, no cast shadows"),
        ], settings["engine"])
        options.addSpacing(12)
        heading("Size")
        self.size_choice = choices(
            [(key, key, f"{w} x {h} pixels") for key, (w, h) in render_scene.RESOLUTIONS.items()], settings["resolution"],
        )
        options.addSpacing(12)
        heading("Quality")
        self.quality_choice = choices(
            [(key, text, f"{seconds} seconds of rendering (LuxCore)") for key, (text, seconds) in render_scene.QUALITIES.items()], settings["quality"],
        )
        options.addSpacing(8)
        self.check_denoise = QCheckBox("Denoise", bar)
        self.check_denoise.setChecked(settings["denoise"])
        self.check_denoise.setToolTip("Clean up the last of the grain when LuxCore finishes: takes a few seconds")
        options.addWidget(self.check_denoise)
        options.addStretch(1)
        for group, key in ((self.engine_choice, "engine"), (self.size_choice, "resolution"), (self.quality_choice, "quality")):
            group.buttonClicked.connect(lambda button, k=key: self._option_picked(k, button.property("value")))
        self.check_denoise.toggled.connect(lambda on: set_render_settings(denoise=on))

        self.status = QLabel("", bar)
        self.status.setWordWrap(True)
        self.status.setObjectName("toolbarLabel")
        rows.addWidget(self.status)
        self.progress = QProgressBar(bar)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.hide()
        rows.addWidget(self.progress)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        rows.addLayout(buttons)
        self.btn_render = QPushButton("Render", bar)
        set_icon(self.btn_render, "render")
        self.btn_render.setToolTip("Render what's in the viewer now")
        self.btn_render.clicked.connect(self._render_clicked)
        buttons.addWidget(self.btn_render)
        self.btn_stop = QPushButton("Stop", bar)
        self.btn_stop.setToolTip("Finish now, with what's been rendered so far")
        self.btn_stop.clicked.connect(self._stop_clicked)
        buttons.addWidget(self.btn_stop)
        self.btn_save = QPushButton("Save PNG...", bar)
        set_icon(self.btn_save, "save")
        self.btn_save.setToolTip("Save the picture as a PNG file")
        self.btn_save.clicked.connect(self._save_clicked)
        buttons.addWidget(self.btn_save)
        self.btn_use_viewer = QPushButton("Use the Viewer's Renderer", bar)
        self.btn_use_viewer.setToolTip("Render with the viewer itself: needs nothing extra, but has no cast shadows")
        self.btn_use_viewer.clicked.connect(self._use_viewer)
        self.btn_use_viewer.hide()
        buttons.addWidget(self.btn_use_viewer)
        buttons.addStretch(1)
        self.btn_remove = QPushButton("Remove LuxCore", bar)
        self.btn_remove.setToolTip("Delete the downloaded LuxCore (it can be downloaded again)")
        self.btn_remove.clicked.connect(self._remove_clicked)
        buttons.addWidget(self.btn_remove)
        close = QPushButton("Close", bar)
        close.setToolTip("Close this window (a render still going is stopped)")
        close.clicked.connect(self.reject)
        buttons.addWidget(close)

    def _value(self, group: QButtonGroup) -> str:
        return group.checkedButton().property("value")

    def engine(self) -> str:
        return self._value(self.engine_choice)

    def _option_picked(self, key: str, value: str) -> None:
        set_render_settings(**{key: value})
        self._refresh()

    def _busy(self) -> bool:
        return self.process is not None or self._download is not None

    def _refresh(self) -> None:
        """The buttons, for what can be done now."""
        busy = self._busy()
        lux = self.engine() == LUXCORE
        available = render_engine.supported()
        installed = render_engine.is_installed()
        megabytes = render_engine.DOWNLOAD_BYTES / 1e6
        for group in (self.engine_choice, self.size_choice, self.quality_choice):
            for button in group.buttons():
                button.setEnabled(not busy)
        for button in self.quality_choice.buttons():
            button.setEnabled(not busy and lux)
        self.check_denoise.setEnabled(not busy and lux)
        if lux and not installed:
            self.btn_render.setText(f"Download LuxCore ({megabytes:.0f} MB)")
            self.btn_render.setToolTip("Download LuxCore, once: it's free, and about 300 MB once unpacked")
        else:
            self.btn_render.setText("Render")
            self.btn_render.setToolTip("Render what's in the viewer now")
        self.btn_render.setEnabled(not busy and (available or not lux))
        self.btn_stop.setEnabled(busy)
        self.btn_save.setEnabled(not busy and (self._image is not None or self._result is not None))
        self.btn_remove.setVisible(installed)
        self.btn_remove.setEnabled(not busy)
        if not busy and not self.status.text():
            if lux and not available:
                self.status.setText("LuxCore isn't available for this version of the app. The viewer's renderer works without it.")
            elif lux and not installed:
                self.status.setText(f"LuxCore is free and downloads once: {megabytes:.0f} MB, about 300 MB once unpacked. It's only needed for this.")

    def _say(self, text: str) -> None:
        self.status.setText(text)

    # ------------------ rendering ------------------
    def _render_clicked(self) -> None:
        self.btn_use_viewer.hide()
        self._image = self._result = None
        self._clean_job_folder()  # (the last one's: only one is ever kept)
        if self.engine() == VIEWER:
            self._render_with_viewer()
        elif render_engine.is_installed():
            self._start_luxcore()
        else:
            self._start_download()
        self._refresh()

    def _size(self) -> tuple[int, int]:
        return render_scene.RESOLUTIONS[self._value(self.size_choice)]

    def _render_with_viewer(self) -> None:
        width, height = self._size()
        image = self.viewport.render_image(width, height)
        if image is None or image.isNull():
            self._say("The viewer couldn't draw it. Is its window showing?")
            return
        self._image = image
        self._saved_size = self._value(self.size_choice)
        self.preview.show_pixmap(QPixmap.fromImage(image))
        self._say(f"Done: {width} x {height}.")

    def _use_viewer(self) -> None:
        for button in self.engine_choice.buttons():
            if button.property("value") == VIEWER:
                button.setChecked(True)
        set_render_settings(engine=VIEWER)
        self.btn_use_viewer.hide()
        self._render_clicked()

    def _start_luxcore(self) -> None:
        width, height = self._size()
        self.folder = render_engine.jobs_folder() / uuid.uuid4().hex[:8]
        self.folder.mkdir(parents=True, exist_ok=True)
        job = build_job(self.viewport, self.model)
        job.width, job.height = width, height
        job.seconds = float(render_scene.QUALITIES[self._value(self.quality_choice)][1])
        job.denoise = self.check_denoise.isChecked()
        job.save(self.folder)
        self._seconds = job.seconds
        self._saved_size = self._value(self.size_choice)
        self._preview_seen = 0
        self._stopping = False
        self.process = QProcess(self)
        self.process.finished.connect(self._process_finished)
        self.process.errorOccurred.connect(self._process_error)
        program, arguments = worker_command(self.folder)
        self.progress.setRange(0, 0)
        self.progress.show()
        self._say("Starting...")
        self.process.start(program, arguments)
        self._timer.start()

    def _read_status(self) -> dict:
        if self.folder is None:
            return {}
        try:
            return json.loads((self.folder / render_worker.STATUS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}  # (it isn't there yet, or it's being replaced: next time)

    @Slot()
    def _poll(self) -> None:
        status = self._read_status()
        if not status:
            return
        phase = status.get("phase")
        if phase == "rendering":
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(1000 * min(1.0, float(status.get("rendered", 0.0)) / max(self._seconds, 0.1))))
        elif phase in ("reading", "preparing", "compiling", "denoising", "starting"):
            self.progress.setRange(0, 0)
        if not self._stopping or phase in ("denoising",):
            self._say(str(status.get("message", "")))
        seen = int(status.get("preview", 0) or 0)
        if seen != self._preview_seen:
            self._preview_seen = seen
            pixmap = QPixmap(str(self.folder / render_worker.PREVIEW_FILE))
            if not pixmap.isNull():
                self.preview.show_pixmap(pixmap)

    @Slot(int, QProcess.ExitStatus)
    def _process_finished(self, code: int, _exit_status: QProcess.ExitStatus) -> None:
        self._timer.stop()
        status = self._read_status()
        phase = status.get("phase")
        self.process = None
        self.progress.hide()
        result = self.folder / render_worker.RESULT_FILE if self.folder is not None else None
        if phase == "done" and result is not None and result.is_file():
            self._result = result
            self.preview.show_pixmap(QPixmap(str(result)))
            self._say(f"Done on {status.get('device', 'LuxCore')}: {status.get('passes', 0):,} passes.")
        elif phase == "stopped":
            self._say("Stopped.")
        else:
            why = status.get("message") if phase == "failed" else f"it stopped unexpectedly (code {code})"
            self._say(f"LuxCore couldn't render this: {why}\nThe viewer's renderer needs nothing extra, but has no cast shadows.")
            self.btn_use_viewer.show()
        self._refresh()

    @Slot(QProcess.ProcessError)
    def _process_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart and self.process is not None:
            self._timer.stop()
            self.process = None
            self.progress.hide()
            self._say("The render couldn't be started.\nThe viewer's renderer needs nothing extra, but has no cast shadows.")
            self.btn_use_viewer.show()
            self._refresh()

    def _stop_clicked(self) -> None:
        if self._download is not None:
            self._download.cancel()
            self._download = None
            self.progress.hide()
            self._say("Download stopped.")
            self._refresh()
        elif self.process is not None and self.folder is not None:
            self._stopping = True
            self._say("Finishing...")
            (self.folder / render_worker.STOP_FILE).touch()

    # ------------------ the engine ------------------
    def _start_download(self) -> None:
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.show()
        self._say("Downloading LuxCore...")
        self._download = UnpackJob()
        unpack_in_background(lambda progress, stop: render_engine.install(progress, stop), self._download_done, self._download_failed,
                             self._download_progress, self._download)

    def _download_progress(self, done: int, total: int) -> None:
        if self._download is None:
            return
        self.progress.setValue(int(1000 * done / max(total, 1)))
        if done >= total:
            self.progress.setRange(0, 0)
            self._say("Unpacking LuxCore...")
        else:
            self._say(f"Downloading LuxCore: {done / 1e6:.0f} of {total / 1e6:.0f} MB")

    def _download_done(self, _result: object) -> None:
        self._download = None
        self.progress.hide()
        self._say("LuxCore is ready.")
        self._render_clicked()

    def _download_failed(self, message: str) -> None:
        self._download = None
        self.progress.hide()
        self._say(f"{message}\nThe viewer's renderer needs nothing extra, but has no cast shadows.")
        self.btn_use_viewer.show()
        self._refresh()

    def _remove_clicked(self) -> None:
        self._clean_job_folder()
        render_engine.remove()
        self._result = None
        self._say("")
        self._refresh()

    # ------------------ saving, and clearing up ------------------
    def _save_clicked(self) -> None:
        folder = get_render_settings()["folder"] or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
        suggested = str(Path(folder) / f"{Path(self.name).stem}_render_{self._saved_size}.png")
        path, _filter = QFileDialog.getSaveFileName(self, "Save the Render", suggested, "PNG image (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        try:
            if self._result is not None:
                shutil.copyfile(self._result, path)
            elif self._image is None or not self._image.save(path, "PNG"):
                raise OSError("the image couldn't be written")
        except OSError as exc:
            self._say(f"It couldn't be saved: {getattr(exc, 'strerror', None) or exc}")
            return
        set_render_settings(folder=str(Path(path).parent))
        self._say(f"Saved {Path(path).name}.")

    def _clean_job_folder(self) -> None:
        if self.folder is not None:
            shutil.rmtree(self.folder, ignore_errors=True)
            self.folder = None

    @staticmethod
    def _clean_old_jobs() -> None:
        """What a crash or a closed app may have left."""
        shutil.rmtree(render_engine.jobs_folder(), ignore_errors=True)

    def _shut_down(self) -> None:
        self._timer.stop()
        if self._download is not None:
            self._download.cancel()
            self._download = None
        if self.process is not None:
            process, self.process = self.process, None
            process.finished.disconnect(self._process_finished)
            process.errorOccurred.disconnect(self._process_error)
            process.kill()
            process.waitForFinished(5000)
        self._clean_job_folder()

    def done(self, result: int) -> None:
        self._shut_down()
        super().done(result)

    def closeEvent(self, event) -> None:
        self._shut_down()
        super().closeEvent(event)
