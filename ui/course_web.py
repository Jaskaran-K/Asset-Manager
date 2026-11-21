"""Web lessons in the course window (core.course_links): YouTube's embedded player in a web view (WebLessonView), and
a card for a lesson that opens in your browser (BrowserCard).

The web view is Qt's own browser (QtWebEngine). It's made the first time a YouTube lesson plays, and the app sets
Qt.AA_ShareOpenGLContexts before it starts, as Qt asks. Where it isn't available, YouTube lessons open in the browser
like any other web lesson.
"""

from __future__ import annotations

import json
from typing import Optional

from PySide6.QtCore import QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QImage
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from core import course_links
from ui.duotone import DuotoneGlyph

POLL_MS = 400
BASE_URL = "https://assetmanager.local/"  # (the page's own address: YouTube's player wants one to come from)


def web_view_available() -> bool:
    try:
        import PySide6.QtWebEngineWidgets  # noqa: F401  (Qt's browser: part of PySide6's add-ons)
    except ImportError:
        return False
    return True


def trim_black(image: QImage, dark: int = 18) -> QImage:
    """The image without black bars at its edges (a video letterboxed in the player)."""

    def blank_row(y: int) -> bool:
        return all(max(image.pixelColor(x, y).getRgb()[:3]) <= dark for x in range(0, image.width(), max(1, image.width() // 40)))

    def blank_column(x: int) -> bool:
        return all(max(image.pixelColor(x, y).getRgb()[:3]) <= dark for y in range(top, bottom + 1, max(1, (bottom - top) // 40 or 1)))

    top, bottom = 0, image.height() - 1
    while top < bottom and blank_row(top):
        top += 1
    while bottom > top and blank_row(bottom):
        bottom -= 1
    left, right = 0, image.width() - 1
    while left < right and blank_column(left):
        left += 1
    while right > left and blank_column(right):
        right -= 1
    if right - left < 16 or bottom - top < 16:
        return image  # (all dark: keep it as it is)
    return image.copy(left, top, right - left + 1, bottom - top + 1)


class WebLessonView(QWidget):
    """YouTube's embedded player, with where it's at reported (position / duration in ms, ended, failed)."""

    position_changed = Signal(int)
    duration_changed = Signal(int)
    ended = Signal()
    failed = Signal(str)
    fullscreen_changed = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        from PySide6.QtWebEngineCore import QWebEngineSettings
        from PySide6.QtWebEngineWidgets import QWebEngineView

        self.setObjectName("courseWeb")
        self.view = QWebEngineView(self)
        settings = self.view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, True)
        self.view.page().fullScreenRequested.connect(self._on_fullscreen_request)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)
        self.playing = False
        self.position = 0
        self.duration = 0
        self._ended = False
        self._error_told = False
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)

    def load_youtube(self, video_id: str, start_ms: int = 0, autoplay: bool = True) -> None:
        self.playing, self.position, self.duration, self._ended, self._error_told = False, start_ms, 0, False, False
        self.view.setHtml(course_links.embed_page(video_id, start_ms // 1000, autoplay), QUrl(BASE_URL))
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self.view.setHtml("<html><body style='background:#000'></body></html>", QUrl(BASE_URL))
        self.playing = False

    def play(self) -> None:
        self.view.page().runJavaScript("am_play()")

    def pause(self) -> None:
        self.view.page().runJavaScript("am_pause()")

    def seek(self, ms: int) -> None:
        self.view.page().runJavaScript(f"am_seek({max(0, int(ms)) / 1000.0})")

    def frame(self) -> Optional[QImage]:
        """The picture showing (for a note's card on the Artbook page), without the black bars round the video."""
        image = self.view.grab().toImage()
        return None if image.isNull() else trim_black(image)

    def _poll(self) -> None:
        self.view.page().runJavaScript("JSON.stringify(window.am || null)", 0, self._on_state)

    def _on_state(self, text) -> None:
        try:
            state = json.loads(text) if isinstance(text, str) else None
        except ValueError:
            state = None
        if not isinstance(state, dict):
            return
        if state.get("error") is not None and not self._error_told:
            self._error_told = True
            self.failed.emit(course_links.YOUTUBE_ERRORS.get(state["error"], "YouTube's player couldn't play it here."))
            return
        self.playing = state.get("state") == 1
        duration = int(float(state.get("d") or 0) * 1000)
        if duration and duration != self.duration:
            self.duration = duration
            self.duration_changed.emit(duration)
        position = int(float(state.get("t") or 0) * 1000)
        if state.get("state") in (1, 2) and position != self.position:
            self.position = position
            self.position_changed.emit(position)
        if state.get("state") == 0 and not self._ended:  # (YouTube's "ended")
            self._ended = True
            self.ended.emit()

    def _on_fullscreen_request(self, request) -> None:
        request.accept()
        self.fullscreen_changed.emit(request.toggleOn())

    def leave_fullscreen(self) -> None:
        from PySide6.QtWebEngineCore import QWebEnginePage

        self.view.page().triggerAction(QWebEnginePage.WebAction.ExitFullScreen)


class BrowserCard(QWidget):
    """A web lesson that plays in your browser: what it is, Open in Browser, and Mark as Done."""

    done_toggled = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("courseBrowserCard")
        self.url = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.addStretch(1)
        glyph = DuotoneGlyph("web", size=48)
        layout.addWidget(glyph, 0, Qt.AlignmentFlag.AlignHCenter)
        self.heading = QLabel(self)
        self.heading.setObjectName("dialogTitle")
        self.heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        self.about = QLabel(self)
        self.about.setObjectName("pageHint")
        self.about.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.about.setWordWrap(True)
        layout.addWidget(self.about)
        self.btn_open = QPushButton("Open in Browser", self)
        self.btn_open.setObjectName("primaryBtn")
        self.btn_open.setToolTip("Watch it in your web browser (where you're logged in)")
        self.btn_open.clicked.connect(self.open_in_browser)
        layout.addWidget(self.btn_open, 0, Qt.AlignmentFlag.AlignHCenter)
        self.btn_done = QPushButton("Mark as Done", self)
        self.btn_done.setToolTip("The app can't see how far you've watched in your browser: tick it done here")
        self.btn_done.clicked.connect(self._on_done)
        layout.addWidget(self.btn_done, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)

    def _on_done(self) -> None:
        self.done_toggled.emit()

    def show_lesson(self, lesson: dict, site: str, why: str = "") -> None:
        self.url = lesson.get("url") or ""
        self.heading.setText(lesson.get("name") or "Web lesson")
        self.about.setText((why + " " if why else "") + f"It plays on {site or 'its site'}, in your browser. Notes still "
                           "work here (they're kept without a time).")
        self.btn_done.setText("Mark as Not Done" if lesson.get("done") else "Mark as Done")

    def open_in_browser(self) -> None:
        if self.url:
            QDesktopServices.openUrl(QUrl(self.url))
