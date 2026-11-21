"""Course View (Ctrl+5, or View > Course View): your courses in a window of their own (core.courses has what a course
is).

- The Courses page: a card per course with its cover, how far you are, and one button to carry on. New Course starts
  an empty one; Open Folder as Course... drafts one from a folder (offering to unzip its archives first:
  ui.course_unzip).
- A course: its chapters and lessons on the left (click to play, the box ticks it done; it ticks itself at 90%), the
  player in the middle (the app's own: it resumes, speeds up, loops A-B, sends frames to the Artbook) with the lesson
  after it starting by itself, and on the right the lesson's files (each opens in its app, Maya first; pictures drag
  onto the Artbook) and the whole course's, or your notes: N notes the moment playing, and each note also lands on
  the course's Artbook page with its frame (ui.course_page).
- Edit Course: drag lessons about, rename them (double-click), drop files on a lesson, videos anywhere; the Course
  files tray holds what the draft couldn't place. A long video is cut into lessons in the cuts bar under the player:
  K cuts at the moment playing.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QByteArray, QEvent, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QPixmap
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPushButton,
    QSplitter, QStackedWidget, QVBoxLayout, QWidget,
)

from core import apps, course_links, course_unzip, courses
from core.scanner import VIDEO_EXTENSIONS
from core.settings import get_course_window, set_course_window
from core.video_tools import extract_poster_frame, probe_video
from ui import course_page, course_web, file_actions, shortcuts
from ui.archive_jobs import UnpackJob, unpack_in_background
from ui.controls import ghost_button, segmented
from ui.course_widgets import CourseCards, CutBar, FileList, LessonList, NotesList, is_video
from ui.duotone import set_icon
from ui.tag_widgets import run_job
from ui.video_player import VideoPlayer, format_time

SAVE_DELAY_MS = 2500  # progress is written this long after it changes (and on leaving a lesson or the window)
NEXT_DELAY_MS = 1200  # a lesson ended: the next one starts after this
MESSAGE_MS = 4500


class CoursePage(QWidget):
    """One course: lessons | player | files / notes (see the module docstring)."""

    def __init__(self, window: "CourseWindow"):
        super().__init__(window)
        self.window_ = window
        self.course: Optional[dict[str, Any]] = None
        self.lesson: Optional[dict[str, Any]] = None
        self.editing = False
        self._note_at: Optional[int] = None
        self._note_frame = None
        self._resume_after_note = False
        self._ending = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        self.split = QSplitter(Qt.Orientation.Horizontal, self)
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(10)
        self.split.setObjectName("courseSplit")
        self.split.setStyleSheet("QSplitter#courseSplit::handle { background: transparent; }")  # (a gap, not a bar)
        layout.addWidget(self.split)

        # the lessons
        left = QWidget(self.split)
        self.left_pane = left
        column = QVBoxLayout(left)
        column.setContentsMargins(0, 0, 0, 0)
        self.course_name = QLabel(left)
        self.course_name.setObjectName("dialogTitle")
        self.course_name.setWordWrap(True)
        column.addWidget(self.course_name)
        self.course_info = QLabel(left)
        self.course_info.setObjectName("pageHint")
        column.addWidget(self.course_info)
        self.lessons = LessonList(left)
        self.lessons.lesson_clicked.connect(self._on_lesson_clicked)
        self.lessons.tick_clicked.connect(self._on_tick)
        self.lessons.order_changed.connect(self._on_order_changed)
        self.lessons.files_dropped.connect(self._on_files_dropped)
        self.lessons.links_dropped.connect(self._on_links_dropped)
        self.lessons.renamed.connect(self._on_renamed)
        self.lessons.itemSelectionChanged.connect(self._on_selection)
        column.addWidget(self.lessons, 1)
        edit_row = QHBoxLayout()
        self.btn_add_videos = ghost_button("add", "Add videos to the course: each becomes a lesson", "Add Videos...")
        self.btn_add_videos.clicked.connect(self.add_videos)
        self.btn_add_chapter = ghost_button("new_folder", "A new chapter at the end (drag lessons under it)", "Add Chapter")
        self.btn_add_chapter.clicked.connect(self.add_chapter)
        self.btn_add_link = ghost_button("web", "Lessons from the web: YouTube plays here, a video file's address in "
                                         "the app's player, anything else in your browser (or drag a link in)", "Add Link...")
        self.btn_add_link.clicked.connect(self.add_link)
        for button in (self.btn_add_videos, self.btn_add_link, self.btn_add_chapter):
            edit_row.addWidget(button)
        edit_row.addStretch(1)
        column.addLayout(edit_row)
        self.split.addWidget(left)

        # the player
        middle = QWidget(self.split)
        centre = QVBoxLayout(middle)
        centre.setContentsMargins(4, 0, 4, 0)
        self.title = QLabel(middle)
        self.title.setObjectName("dialogTitle")
        centre.addWidget(self.title)
        heading = QHBoxLayout()
        self.subtitle = QLabel(middle)
        self.subtitle.setObjectName("pageHint")
        heading.addWidget(self.subtitle, 1)
        self.btn_previous = ghost_button("back", "The lesson before this one", "Previous")
        self.btn_previous.clicked.connect(lambda: self.step_lesson(-1))
        self.btn_next = ghost_button("play", "The next lesson", "Next Lesson")
        self.btn_next.clicked.connect(lambda: self.step_lesson(1))
        heading.addWidget(self.btn_previous)
        heading.addWidget(self.btn_next)
        centre.addLayout(heading)
        self.stage = QStackedWidget(middle)  # (the app's player; YouTube's; or a card for a lesson in the browser)
        self.player = VideoPlayer(self.stage)
        self.player.artbook = window.artbook_sender
        self.player.player.positionChanged.connect(self._on_position)
        self.player.player.durationChanged.connect(self._on_duration)
        self.player.player.mediaStatusChanged.connect(self._on_media_status)
        self.stage.addWidget(self.player)
        self.browser = course_web.BrowserCard(self.stage)
        self.browser.done_toggled.connect(self._on_browser_done)
        self.stage.addWidget(self.browser)
        self.web: Optional[course_web.WebLessonView] = None  # (made the first time a YouTube lesson plays)
        self.mode: Optional[str] = None  # what's playing: "file", "url" (a video file's address), "web", "browser"
        self._loaded_for: Optional[str] = None  # the lesson it's playing for
        centre.addWidget(self.stage, 1)
        self.web_bar = QWidget(middle)
        web_row = QHBoxLayout(self.web_bar)
        web_row.setContentsMargins(0, 4, 0, 0)
        self.web_site = QLabel(self.web_bar)
        self.web_site.setObjectName("pageHint")
        web_row.addWidget(self.web_site, 1)
        self.btn_browser = ghost_button("web", "Watch this lesson in your web browser", "Open in Browser")
        self.btn_browser.clicked.connect(self.browser.open_in_browser)
        self.btn_download = ghost_button("import", "Download it for offline with yt-dlp (an HD copy, kept with the "
                                         "course; the lesson then plays that)", "Download for Offline")
        self.btn_download.clicked.connect(self.download_lesson)
        self.btn_cancel_download = ghost_button("close", "Stop downloading (nothing is kept)", "Cancel")
        self.btn_cancel_download.clicked.connect(self.cancel_download)
        for button in (self.btn_browser, self.btn_download, self.btn_cancel_download):
            web_row.addWidget(button)
        centre.addWidget(self.web_bar)
        self._download: Optional[UnpackJob] = None
        self._downloading: Optional[str] = None  # (the lesson being downloaded)
        self.cuts = CutBar(middle)
        self.cuts.part_clicked.connect(self._on_lesson_clicked)
        self.cuts.part_rename.connect(self.rename_lesson)
        self.cuts.cut_moved.connect(self._on_cut_moved)
        self.cuts.join_requested.connect(self._on_join)
        centre.addWidget(self.cuts)
        self.cuts_hint = QLabel(f"{shortcuts.get('course.cut').keys}: cut the video into lessons here · drag a cut "
                                "to move it · double-click a part to name it · Delete joins a part with the next", middle)
        self.cuts_hint.setObjectName("pageHint")
        centre.addWidget(self.cuts_hint)
        self.split.addWidget(middle)

        # files / notes
        right = QWidget(self.split)
        self.right_pane = right
        side = QVBoxLayout(right)
        side.setContentsMargins(0, 0, 0, 0)
        self.tabs, self.tab_group = segmented(
            [("files", "Lesson files", "This lesson's files, and the whole course's"),
             ("notes", "Notes", f"Your notes on this lesson ({shortcuts.get('course.note').keys} adds one)")], "files", right)
        self.tab_group.buttonClicked.connect(self._on_tab)
        side.addWidget(self.tabs)
        self.side_stack = QStackedWidget(right)
        self.files = FileList(right)
        self.files.open_requested.connect(self.open_file)
        self.files.reveal_requested.connect(self._reveal)
        self.files.remove_requested.connect(self._remove_file)
        self.side_stack.addWidget(self.files)
        notes = QWidget(right)
        notes_column = QVBoxLayout(notes)
        notes_column.setContentsMargins(0, 0, 0, 0)
        self.notes = NotesList(notes)
        self.notes.note_clicked.connect(self._on_note_clicked)
        self.notes.delete_requested.connect(self._on_delete_note)
        notes_column.addWidget(self.notes, 1)
        self.note_box = QLineEdit(notes)
        self.note_box.setToolTip(f"Type a note, Enter to keep it ({shortcuts.get('course.note').keys} while watching "
                                 "starts one at that moment)")
        self.note_box.returnPressed.connect(self._keep_note)
        self.note_box.installEventFilter(self)
        notes_column.addWidget(self.note_box)
        self.btn_page = ghost_button("view_artbook", "Open the course's Artbook page: your notes there, each with its "
                                     "frame", "Open the Artbook Page")
        self.btn_page.clicked.connect(window.open_artbook_page)
        notes_column.addWidget(self.btn_page)
        self.side_stack.addWidget(notes)
        self.tray = FileList(right)  # (editing: the Course files tray)
        self.tray.open_requested.connect(self.open_file)
        self.tray.reveal_requested.connect(self._reveal)
        self.tray.remove_requested.connect(self._remove_file)
        self.side_stack.addWidget(self.tray)
        side.addWidget(self.side_stack, 1)
        tray_row = QHBoxLayout()
        self.btn_add_files = ghost_button("import", "Add files to the course: drag them onto a lesson", "Add Files...")
        self.btn_add_files.clicked.connect(self.add_files)
        self.btn_relink = ghost_button("search", "Find files that moved: pick the folder they're in now", "Relink...")
        self.btn_relink.clicked.connect(self.relink)
        tray_row.addWidget(self.btn_add_files)
        tray_row.addWidget(self.btn_relink)
        tray_row.addStretch(1)
        side.addLayout(tray_row)
        self.split.addWidget(right)
        self.split.setStretchFactor(0, 0)
        self.split.setStretchFactor(1, 1)
        self.split.setStretchFactor(2, 0)
        self.split.setSizes([330, 660, 360])

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SAVE_DELAY_MS)
        self._save_timer.timeout.connect(self.save)
        self._next_timer = QTimer(self)
        self._next_timer.setSingleShot(True)
        self._next_timer.setInterval(NEXT_DELAY_MS)
        self._next_timer.timeout.connect(self._play_next)
        self.set_editing(False)

    # ------------------ the course ------------------
    def show_course(self, course: dict[str, Any], play: bool = False) -> None:
        self.save()
        self.course = course
        course["opened"] = time.time()
        self.lesson = None
        self.lessons.fill(course)
        self._show_header()
        start = courses.next_up(course) or next(courses.lessons(course), None)
        if start is not None:
            self.open_lesson(start["id"], play)
        else:
            self.player.stop()
            self._show_lesson()
        if self.editing:
            self._refresh_files()  # (the Course files tray)
        self.save()
        self._probe_lengths()

    def _show_header(self) -> None:
        course = self.course
        self.course_name.setText(course["name"])
        done, total, left = courses.progress(course)
        bits = [f"{done} of {total} lesson{'s' if total != 1 else ''}"]
        if left:
            bits.append(f"{courses.format_length(left)} left")
        self.course_info.setText(" · ".join(bits))

    def save(self) -> None:
        self._save_timer.stop()
        if self.course is not None:
            try:
                courses.save_course(self.course)
            except courses.CourseError as exc:
                self.window_.message(str(exc))

    def _changed(self) -> None:
        self._save_timer.start()

    # ------------------ lessons ------------------
    def open_lesson(self, lesson_id: str, play: bool = True) -> None:
        found = courses.find_lesson(self.course, lesson_id)
        if found is None:
            return
        if self.lesson is not None:
            self._record(self.position())
        self._next_timer.stop()
        self._ending = False
        self.lesson = found[1]
        self.course["last_lesson"] = lesson_id
        self.lessons.current_id = lesson_id
        self.lessons.select(lesson_id)
        self.lessons.viewport().update()
        self._load_media(play)
        self._show_lesson()
        self._changed()

    def _load_media(self, play: bool) -> None:
        """The lesson's video into the player (a file; or a web lesson: core.course_links says which way it plays)."""
        lesson = self.lesson
        video, url = lesson.get("video"), lesson.get("url")
        self._loaded_for = lesson["id"]
        if url and not (video and Path(video).is_file()):
            self._load_web(url, play)
            return
        self._stop_web()
        self.stage.setCurrentWidget(self.player)
        self.mode = "file" if video else None
        if video and Path(video).is_file():
            same = self.player.source() is not None and str(self.player.source()) == video
            start = self.lesson.get("start")
            if start is not None or self.lesson.get("end") is not None:  # (a part: where you were in it, or its start)
                position = self.lesson.get("position")
                end = self.lesson.get("end") or 1 << 62
                at = position if isinstance(position, int) and (start or 0) <= position < end - 3000 else (start or 0)
                if same:
                    self.player.player.setPosition(at)
                    if play:
                        self.player.player.play()
                else:
                    self.player.load(video, autoplay=play, position=at)
            elif not same:
                self.player.load(video, autoplay=play)
            elif play:
                self.player.player.play()
        else:
            self.player.stop()
            if video:
                self.window_.message(f"{Path(video).name} isn't there any more: Edit Course > Relink... finds it.")

    def _resume_at(self) -> int:
        position = self.lesson.get("position")
        return position if isinstance(position, int) and position > 0 else 0

    def _load_web(self, url: str, play: bool) -> None:
        kind = course_links.classify(url)
        if kind["kind"] == "file":  # (a link straight to a video file: the app's own player)
            self._stop_web()
            self.stage.setCurrentWidget(self.player)
            self.mode = "url"
            self.player.load_url(url, autoplay=play, position=self._resume_at())
            return
        self.player.stop()
        if kind["kind"] == "youtube" and course_web.web_view_available():
            view = self._web_view()
            start = self._resume_at() or course_links.start_seconds(url) * 1000
            self.stage.setCurrentWidget(view)
            self.mode = "web"
            view.load_youtube(kind["id"], start, play)
            return
        why = "Vimeo's player can't play inside the app." if kind["kind"] == "vimeo" else ""
        self._show_browser(kind["site"], why)

    def _show_browser(self, site: str, why: str = "") -> None:
        self._stop_web()
        self.mode = "browser"
        self.browser.show_lesson(self.lesson, site, why)
        self.stage.setCurrentWidget(self.browser)

    def _web_view(self) -> course_web.WebLessonView:
        if self.web is None:
            self.web = course_web.WebLessonView(self.stage)
            self.web.position_changed.connect(self._on_web_position)
            self.web.duration_changed.connect(self._on_web_duration)
            self.web.ended.connect(self._on_web_ended)
            self.web.failed.connect(self._on_web_failed)
            self.web.fullscreen_changed.connect(self.window_._on_video_fullscreen)
            self.stage.addWidget(self.web)
        return self.web

    def _stop_web(self) -> None:
        if self.web is not None and self.mode == "web":
            self.web.stop()

    def position(self) -> int:
        """Where the lesson playing is (ms)."""
        if self.mode == "web" and self.web is not None:
            return self.web.position
        return self.player.player.position() if self.mode in ("file", "url") else 0

    def is_playing(self) -> bool:
        if self.mode == "web" and self.web is not None:
            return self.web.playing
        return self.mode in ("file", "url") and self.player.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def pause(self) -> None:
        if self.mode == "web" and self.web is not None:
            self.web.pause()
        elif self.mode in ("file", "url"):
            self.player.player.pause()

    def resume(self) -> None:
        if self.mode == "web" and self.web is not None:
            self.web.play()
        elif self.mode in ("file", "url"):
            self.player.player.play()

    def seek(self, at: int) -> None:
        if self.mode == "web" and self.web is not None:
            self.web.seek(at)
        elif self.mode in ("file", "url"):
            self.player.player.setPosition(at)

    def frame(self):
        if self.mode == "web" and self.web is not None:
            return self.web.frame()
        return self.player.current_frame() if self.mode in ("file", "url") else None

    # (YouTube's player reports where it is)
    def _on_web_position(self, position: int) -> None:
        if self.mode == "web":
            self._on_position(position, web=True)

    def _on_web_duration(self, duration: int) -> None:
        if self.mode == "web" and self.lesson is not None and self.lesson.get("duration") != duration:
            self.lesson["duration"] = duration
            self.lessons.viewport().update()
            self._show_header()
            self._changed()

    def _on_web_ended(self) -> None:
        if self.mode == "web" and self.lesson is not None and not self._ending:
            self._ending = True
            courses.record_watch(self.lesson, self.lesson.get("duration") or self.position())
            self._lesson_ended()

    def _on_web_failed(self, message: str) -> None:
        if self.lesson is not None and self.mode == "web":
            self._show_browser("YouTube", message)

    def _on_browser_done(self) -> None:
        if self.lesson is not None:
            self._on_tick(self.lesson["id"])
            self.browser.show_lesson(self.lesson, course_links.classify(self.lesson.get("url") or "")["site"])

    def _show_lesson(self) -> None:
        lesson = self.lesson
        if lesson is None:
            self.title.setText("No lessons yet" if self.course and not any(courses.lessons(self.course)) else "")
            self.subtitle.setText("Edit Course: drag videos in, or Add Videos..." if self.course else "")
            self.files.fill(self._file_sections(), "")
            self.notes.fill([])
            self.web_bar.hide()
            self._sync_cuts()
            return
        number = f"{lesson['number']} · " if lesson.get("number") else ""
        self.title.setText(f"{number}{lesson['name']}")
        flat = list(courses.lessons(self.course))
        at = flat.index(lesson) if lesson in flat else 0
        chapter = courses.find_lesson(self.course, lesson["id"])[0]["name"]
        after = courses.lesson_after(self.course, lesson["id"])
        bits = [chapter, f"lesson {at + 1} of {len(flat)}"] + ([f"next: {after['name']}"] if after else [])
        self.subtitle.setText(" · ".join(bits))
        self.btn_previous.setEnabled(at > 0)
        self.btn_next.setEnabled(after is not None)
        self._refresh_files()
        self.notes.fill(lesson["notes"])
        self._sync_web_bar()
        self.player.slider_seek.set_marks([n["at"] for n in lesson["notes"] if n.get("at") is not None])
        if lesson.get("start") is not None or lesson.get("end") is not None:  # (a part of a long video)
            self.player.slider_seek.set_part(lesson.get("start"), lesson.get("end"))
        else:
            self.player.slider_seek.set_part(None, None)
        self.note_box.setPlaceholderText(
            "A note on this lesson..." if self.mode == "browser" else
            f"Note at {format_time(self.position())}... ({shortcuts.get('course.note').keys} while watching)")
        self._sync_cuts()

    def _sync_web_bar(self) -> None:
        lesson = self.lesson
        url = lesson.get("url") if lesson else None
        self.web_bar.setVisible(bool(url))
        if not url:
            return
        self.browser.url = url
        site = course_links.classify(url)["site"]
        downloaded = bool(lesson.get("video")) and Path(lesson["video"]).is_file()
        busy = self._downloading == lesson["id"]
        if not busy:
            self.web_site.setText(f"From {site}" + (" \u00b7 downloaded: playing the copy kept with the course" if downloaded else ""))
        has_ytdlp = course_links.find_ytdlp() is not None
        self.btn_download.setVisible(has_ytdlp and not downloaded and not busy and self._downloading is None)
        self.btn_cancel_download.setVisible(busy)

    def add_link(self) -> None:
        """Add Link...: web addresses (one a line) become lessons, after the lesson picked."""
        text, ok = QInputDialog.getMultiLineText(
            self, "Add Link", "Web addresses of lessons, one a line. YouTube plays here; a link to a video file plays in "
            "the app's player; anything else opens in your browser.")
        if ok:
            links = [line.strip() for line in text.splitlines() if course_links.is_web(line)]
            if links:
                self.add_links(links)
            elif text.strip():
                self.window_.message("Those aren't web addresses (they start with https://).")

    def add_links(self, links: list[str], kind: str = "lesson", target: Optional[str] = None) -> list[dict]:
        found = courses.find_lesson(self.course, target or (self.lesson["id"] if self.lesson else None)) if kind == "lesson" else None
        if found is not None:
            chapter, after = found
            at = chapter["lessons"].index(after) + 1
        else:
            chapter = next((c for c in self.course["chapters"] if c["id"] == target), self.course["chapters"][-1])
            at = len(chapter["lessons"])
        made = [courses.new_web_lesson(url, course_links.name_from(url)) for url in links]
        chapter["lessons"][at:at] = made
        self._after_structure_change()
        course = self.course
        run_job(lambda: [course_links.details(lesson["url"]) for lesson in made],
                lambda found_details: self._on_link_details(course, made, found_details), lambda _message: None)
        if self.lesson is None and made:
            self.open_lesson(made[0]["id"], play=False)
        self.window_.message(f"Added {len(made)} web lesson{'s' if len(made) != 1 else ''}; their titles follow.")
        return made

    def _on_link_details(self, course: dict, made: list, found: list) -> None:
        """The sites' titles (and Vimeo's lengths) for the lessons just added (unless you've renamed one meanwhile)."""
        for lesson, info in zip(made, found):
            if info.get("title") and lesson["name"] == course_links.name_from(lesson["url"]):
                lesson["name"] = info["title"][:courses.MAX_NAME]
            if info.get("duration") and not lesson.get("duration"):
                lesson["duration"] = info["duration"]
        if course is self.course:
            self._after_structure_change()
        else:
            courses.save_course(course)

    def _on_links_dropped(self, kind: str, target: str, links: list) -> None:
        self.add_links(links, kind, target)

    def download_lesson(self) -> None:
        """Download for Offline (yt-dlp, if it's installed): an HD copy kept with the course; the lesson plays that."""
        lesson = self.lesson
        if lesson is None or not lesson.get("url") or self._download is not None:
            return
        folder = self.course.get("folder") or self.course.get("download_folder")
        if not folder or not Path(folder).is_dir():
            folder = QFileDialog.getExistingDirectory(self, "Where should downloaded lessons go?")
            if not folder:
                return
            self.course["download_folder"] = folder
        name = f"{lesson['number']} {lesson['name']}" if lesson.get("number") else lesson["name"]
        url, lesson_id, course = lesson["url"], lesson["id"], self.course
        self._downloading = lesson_id
        self.web_site.setText("Downloading\u2026")
        self._sync_web_bar()
        self._download = unpack_in_background(
            lambda progress, stop: course_links.download(url, Path(folder), name, progress, stop),
            lambda path: self._on_downloaded(course, lesson_id, path), self._on_download_failed, self._on_download_progress)

    def _on_download_progress(self, done: int, total: int) -> None:
        if self._downloading is not None and self.lesson is not None and self.lesson["id"] == self._downloading:
            self.web_site.setText(f"Downloading\u2026 {done * 100 // max(1, total)}%")

    def _on_downloaded(self, course: dict, lesson_id: str, path) -> None:
        self._download, self._downloading = None, None
        found = courses.find_lesson(course, lesson_id)
        if found is not None:
            found[1]["video"] = str(path)
        if course is self.course:
            self.window_.message(f"Downloaded \u201c{Path(path).name}\u201d: the lesson plays it from now on.")
            if self.lesson is not None and self.lesson["id"] == lesson_id:
                playing = self.is_playing()
                at = self.position()
                self.lesson["position"] = at or self.lesson.get("position")
                self._load_media(playing)
            self.save()
            self._show_lesson()
        else:
            courses.save_course(course)

    def _on_download_failed(self, message: str) -> None:
        self._download, self._downloading = None, None
        self.window_.message(f"Couldn't download it: {message}")
        self._sync_web_bar()

    def cancel_download(self) -> None:
        if self._download is not None:
            self._download.cancel()
        self._download, self._downloading = None, None
        self._sync_web_bar()

    def _file_sections(self) -> list:
        sections = []
        if self.lesson is not None:
            sections.append(("lesson", "This lesson", self.lesson["files"]))
        if self.course is not None:
            sections.append(("course", "The whole course", self.course["files"]))
        return sections

    def step_lesson(self, step: int) -> None:
        if self.lesson is None:
            return
        flat = list(courses.lessons(self.course))
        at = flat.index(self.lesson) + step
        if 0 <= at < len(flat):
            self.open_lesson(flat[at]["id"])

    def _on_lesson_clicked(self, lesson_id: str) -> None:
        self.open_lesson(lesson_id)

    def _on_selection(self) -> None:
        """Editing: the lesson picked is loaded (paused), so the cuts bar and K work on it."""
        if not self.editing or self.course is None:
            return
        lesson_id = self.lessons.selected_id()
        if lesson_id and courses.find_lesson(self.course, lesson_id) and (self.lesson is None or self.lesson["id"] != lesson_id):
            self.open_lesson(lesson_id, play=False)

    def _on_tick(self, lesson_id: str) -> None:
        found = courses.find_lesson(self.course, lesson_id)
        if found is not None:
            lesson = found[1]
            lesson["done"] = not lesson["done"]
            if not lesson["done"]:
                lesson["watched"] = 0.0
            self.lessons.refresh_lesson(lesson)
            self.lessons.viewport().update()
            self._show_header()
            self._changed()

    # ------------------ watching ------------------
    def _record(self, position: int) -> None:
        lesson = self.lesson
        if lesson is None or self._loaded_for != lesson["id"] or self.mode not in ("file", "url", "web"):
            return
        start, end = lesson.get("start") or 0, lesson.get("end")
        if position < start or (end and position > end + 500):
            return
        if courses.record_watch(lesson, position):
            self._show_header()
        self.lessons.refresh_lesson(lesson)
        self.lessons.viewport().update()
        self._changed()

    def _on_position(self, position: int, web: bool = False) -> None:
        lesson = self.lesson
        if lesson is None or (not web and self.mode not in ("file", "url")):
            return
        self._record(position)
        self.cuts.set_position(position)
        if not self.note_box.hasFocus():
            self.note_box.setPlaceholderText(f"Note at {format_time(position)}... ({shortcuts.get('course.note').keys} while watching)")
        end = lesson.get("end")
        if end and position >= end and not self._ending and not self.editing:  # (the part is over: on to the next)
            self._ending = True
            self.pause()
            courses.record_watch(lesson, end)
            self._lesson_ended()

    def _on_duration(self, duration: int) -> None:
        lesson = self.lesson
        if lesson is not None and duration > 0 and self.mode == "url" and lesson.get("duration") != duration:
            lesson["duration"] = duration  # (a video file's address: its length, now it's known)
            self.lessons.viewport().update()
            self._show_header()
            self._changed()
            return
        if lesson is None or duration <= 0 or self.player.source() is None:
            return
        for each in courses.lessons(self.course):  # (every lesson of this video learns its length)
            if each.get("video") == str(self.player.source()) and each.get("duration") != duration:
                each["duration"] = duration
        self.lessons.viewport().update()
        self._show_header()
        self._sync_cuts()
        self._changed()

    def _on_media_status(self, status) -> None:
        if (status == QMediaPlayer.MediaStatus.EndOfMedia and self.lesson is not None and not self._ending
                and self.mode in ("file", "url")):
            self._ending = True
            courses.record_watch(self.lesson, self.player.player.duration())
            self._lesson_ended()

    def _lesson_ended(self) -> None:
        self.lessons.refresh_lesson(self.lesson)
        self.lessons.viewport().update()
        self._show_header()
        self.save()
        after = courses.lesson_after(self.course, self.lesson["id"])
        if after is not None and not self.editing:
            self.window_.message(f"Up next: {after['name']}")
            self._next_timer.start()

    def _play_next(self) -> None:
        if self.lesson is not None:
            after = courses.lesson_after(self.course, self.lesson["id"])
            if after is not None:
                self.open_lesson(after["id"])

    # ------------------ files ------------------
    def open_file(self, path: str, app_id: str) -> None:
        """A file in its app (Maya first, or app_id's), a folder in Explorer."""
        target = Path(path)
        if not target.exists():
            self.window_.message(f"{target.name} isn't there any more: Edit Course > Relink... finds it.")
            return
        if target.is_dir():
            file_actions.open_file(target)
            return
        app = next((a for a in apps.apps_for(target.suffix.lower()) if a.id == app_id), None) if app_id else None
        if app is None:
            if not file_actions.open_file(target):
                self.window_.message(f"Couldn't open {target.name}.")
            return
        try:
            if not apps.launch(app, target):
                self.window_.message(f"Couldn't open {target.name} in {app.name}.")
        except apps.AppError as exc:
            self.window_.message(str(exc))

    def _reveal(self, path: str) -> None:
        file_actions.reveal(Path(path))

    def _remove_file(self, section: str, path: str) -> None:
        """A file out of a lesson (into the Course files tray, to put elsewhere), or out of the course."""
        if section == "lesson" and self.lesson is not None and path in self.lesson["files"]:
            self.lesson["files"].remove(path)
            self.course["loose"].append(path)
        elif section == "course" and path in self.course["files"]:
            self.course["files"].remove(path)
            self.course["loose"].append(path)
        elif section == "loose" and path in self.course["loose"]:
            self.course["loose"].remove(path)
        self._refresh_files()
        self._changed()

    def _refresh_files(self) -> None:
        if self.editing:  # (the lesson picked: what it has; then what's left to place; then the whole course's)
            lesson = [("lesson", f"This lesson: {self.lesson['name']}", self.lesson["files"])] if self.lesson else []
            self.tray.fill(lesson + [("loose", "Course files: drag each onto its lesson", self.course["loose"]),
                                     ("course", "The whole course", self.course["files"])],
                           "Every file has a place. Add Files... (or drop them on a lesson) adds more.")
        else:
            self.files.fill(self._file_sections(), "No files for this lesson. Edit Course: drop files on it.")

    # ------------------ notes ------------------
    def start_note(self) -> None:
        """N: a note at this moment (playback pauses while you type; Enter keeps it, Esc drops it)."""
        if self.lesson is None:
            return
        timed = self.mode in ("file", "url", "web")
        self._note_at = self.position() if timed else None
        self._note_frame = self.frame() if timed else None
        self._resume_after_note = self.is_playing()
        self.pause()
        self._show_tab("notes")
        self.note_box.setPlaceholderText(f"Note at {format_time(self._note_at)}..." if timed else "A note on this lesson...")
        self.note_box.setFocus()

    def _keep_note(self) -> None:
        text = self.note_box.text().strip()
        if not text or self.lesson is None:
            return
        timed = self.mode in ("file", "url", "web")
        at = self._note_at if self._note_at is not None else (self.position() if timed else None)
        frame = self._note_frame if self._note_at is not None else (self.frame() if timed else None)
        note = courses.add_note(self.lesson, at, text)
        self.note_box.clear()
        self._note_at, self._note_frame = None, None
        try:
            course_page.add_note(self.course, self.lesson, note, frame, self.window_.panel)
        except Exception as exc:  # (the note is kept in the course either way: a page that can't be written...)
            self.window_.message(f"Kept the note; couldn't put it on the Artbook page: {exc}")
            self.save()
        self._show_lesson()
        if self._resume_after_note:
            self._resume_after_note = False
            self.resume()
        self.player.setFocus()

    def _drop_note(self) -> None:
        self.note_box.clear()
        self._note_at, self._note_frame = None, None
        if self._resume_after_note:
            self._resume_after_note = False
            self.resume()
        self.player.setFocus()

    def _on_note_clicked(self, at: int) -> None:
        self.seek(at)

    def _on_delete_note(self, note_id: str) -> None:
        if self.lesson is not None:
            courses.remove_note(self.lesson, note_id)
            self._show_lesson()
            self._changed()

    def eventFilter(self, watched, event) -> bool:
        if watched is self.note_box and event.type() == QEvent.Type.KeyPress and shortcuts.matches("course.note_cancel", event):
            self._drop_note()  # (Esc: no note after all)
            return True
        return super().eventFilter(watched, event)

    def _on_tab(self, button) -> None:
        self._show_tab(button.property("value"))

    def _show_tab(self, tab: str) -> None:
        if self.editing:
            return
        for button in self.tab_group.buttons():
            button.setChecked(button.property("value") == tab)
        self.side_stack.setCurrentIndex(1 if tab == "notes" else 0)

    # ------------------ editing ------------------
    def set_editing(self, on: bool) -> None:
        self.editing = on
        self.lessons.set_editing(on)
        if self.course is not None:
            self.lessons.fill(self.course)
        for widget in (self.btn_add_videos, self.btn_add_link, self.btn_add_chapter, self.btn_add_files, self.btn_relink):
            widget.setVisible(on)
        self.tabs.setVisible(not on)
        if on:
            self.side_stack.setCurrentWidget(self.tray)
        else:
            self._show_tab("files")
        if self.course is not None:
            self._refresh_files()
        self._sync_cuts()

    def _sync_cuts(self) -> None:
        lesson = self.lesson
        video = lesson.get("video") if lesson else None
        parts = courses.parts_of(self.course, video) if self.course and video else []
        show = self.editing and lesson is not None and bool(video)
        self.cuts.setVisible(show)
        self.cuts_hint.setVisible(show)
        if not show:
            return
        length = lesson.get("duration") or self.player.player.duration() or 0
        self.cuts.set_parts([(p["id"], p.get("start") or 0, p.get("end") or length or 1, p["name"]) for p in parts],
                            length, lesson["id"])

    def cut_here(self) -> None:
        """K (editing): cut the video playing into two lessons here."""
        if not self.editing or self.lesson is None:
            return
        position = self.player.player.position()
        if not self.lesson.get("duration"):
            self.lesson["duration"] = self.player.player.duration() or None
        part = courses.cut_lesson(self.course, self.lesson["id"], position)
        if part is None:
            self.window_.message("Cut inside the lesson, at least a second from its ends.")
            return
        self.lessons.fill(self.course)
        self.lessons.current_id = self.lesson["id"]
        self._show_lesson()
        self._show_header()
        self.window_.message(f"Cut at {format_time(position)}: “{part['name']}” starts there. Double-click it to name it.")
        self._changed()

    def _on_cut_moved(self, lesson_id: str, at: int) -> None:
        if courses.move_cut(self.course, lesson_id, at):
            self._after_structure_change()

    def _on_join(self, lesson_id: str) -> None:
        if courses.join_with_next(self.course, lesson_id):
            self._after_structure_change()

    def rename_lesson(self, lesson_id: str) -> None:
        found = courses.find_lesson(self.course, lesson_id)
        if found is None:
            return
        name, ok = QInputDialog.getText(self, "Name the Lesson", "Name:", text=found[1]["name"])
        if ok and name.strip():
            found[1]["name"] = name.strip()[:courses.MAX_NAME]
            self._after_structure_change()

    def _on_renamed(self, kind: str, item_id: str, name: str) -> None:
        if kind == "chapter":
            for chapter in self.course["chapters"]:
                if chapter["id"] == item_id:
                    chapter["name"] = name[:courses.MAX_NAME]
        else:
            found = courses.find_lesson(self.course, item_id)
            if found is not None:
                found[1]["name"] = name[:courses.MAX_NAME]
        self._show_lesson()
        self._changed()

    def _on_order_changed(self) -> None:
        """Lessons dragged about: the course follows the list (a lesson goes in the chapter above it)."""
        by_id = {lesson["id"]: lesson for lesson in courses.lessons(self.course)}
        chapters = {chapter["id"]: chapter for chapter in self.course["chapters"]}
        made, current = [], None
        for kind, item_id, text in self.lessons.order():
            if kind == "chapter":
                current = dict(chapters[item_id], lessons=[])
                made.append(current)
            elif item_id in by_id:
                if current is None:
                    current = courses.new_chapter("Lessons")
                    made.append(current)
                current["lessons"].append(by_id[item_id])
        self.course["chapters"] = made
        self._after_structure_change()

    def _after_structure_change(self) -> None:
        self.lessons.fill(self.course)
        self._show_lesson()
        self._show_header()
        self._changed()

    def _on_files_dropped(self, kind: str, target: str, paths: list) -> None:
        """Dropped while editing: videos become lessons (after the lesson dropped on, or in the chapter), other files a
        lesson's (dropped on it), else the tray's."""
        videos = [p for p in paths if is_video(p)]
        others = [p for p in paths if not is_video(p)]
        for path in others:  # (out of the tray or another lesson, if that's where it came from)
            if path in self.course["loose"]:
                self.course["loose"].remove(path)
        if kind == "lesson":
            found = courses.find_lesson(self.course, target)
            chapter, lesson = found
            lesson["files"] += [p for p in others if p not in lesson["files"]]
            at = chapter["lessons"].index(lesson) + 1
            for video in reversed(courses.add_videos({"chapters": [courses.new_chapter("x")]}, [Path(v) for v in videos])):
                chapter["lessons"].insert(at, video)
            self.window_.message(f"Added {len(others)} file{'s' if len(others) != 1 else ''} to “{lesson['name']}”."
                                 if others and not videos else f"Added {len(videos) + len(others)} to the course.")
        else:
            courses.add_videos(self.course, [Path(v) for v in videos], target if kind == "chapter" else None)
            self.course["loose"] += [p for p in others if p not in self.course["loose"]]
        self._after_structure_change()
        self._refresh_files()
        self._probe_lengths()

    def add_videos(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(self, "Add Videos", self.course.get("folder") or "",
                                                "Videos (" + " ".join(f"*{e}" for e in sorted(VIDEO_EXTENSIONS)) + ")")
        if names:
            courses.add_videos(self.course, [Path(n) for n in names])
            self._after_structure_change()
            self._probe_lengths()
            if self.lesson is None:
                first = next(courses.lessons(self.course), None)
                if first is not None:
                    self.open_lesson(first["id"], play=False)

    def add_files(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(self, "Add Files", self.course.get("folder") or "")
        if names:
            self.course["loose"] += [n for n in names if n not in self.course["loose"]]
            self._refresh_files()
            self._changed()

    def add_chapter(self) -> None:
        name, ok = QInputDialog.getText(self, "Add Chapter", "Name:", text=f"Chapter {len(self.course['chapters']) + 1}")
        if ok and name.strip():
            self.course["chapters"].append(courses.new_chapter(name.strip()))
            self._after_structure_change()

    def remove_selected(self) -> None:
        """Delete (editing): the selected lesson out of the course (its video and files into the tray), or an empty
        chapter."""
        if not self.editing:
            return
        if self.cuts.hasFocus() and self.cuts.current:  # (in the cuts bar: the cut after the part picked)
            self._on_join(self.cuts.current)
            return
        items = self.lessons.selectedItems()
        if not items:
            return
        kind, item_id = items[0].data(Qt.ItemDataRole.UserRole), items[0].data(Qt.ItemDataRole.UserRole + 1)
        if kind == "chapter":
            chapter = next(c for c in self.course["chapters"] if c["id"] == item_id)
            index = self.course["chapters"].index(chapter)
            if chapter["lessons"] and index == 0 and len(self.course["chapters"]) == 1:
                return
            if chapter["lessons"]:  # (its lessons join the chapter before it)
                into = self.course["chapters"][index - 1] if index > 0 else self.course["chapters"][1]
                into["lessons"] += chapter["lessons"]
            self.course["chapters"].remove(chapter)
        else:
            found = courses.find_lesson(self.course, item_id)
            if found is None:
                return
            chapter, lesson = found
            others = [l for l in courses.lessons(self.course) if l is not lesson and l.get("video") == lesson.get("video")]
            if lesson.get("video") and not others:
                self.course["loose"].append(lesson["video"])
            self.course["loose"] += [p for p in lesson["files"] if p not in self.course["loose"]]
            chapter["lessons"].remove(lesson)
            if self.lesson is lesson:
                self.lesson = None
                self.player.stop()
        self._after_structure_change()
        self._refresh_files()

    def relink(self) -> None:
        missing = courses.missing_paths(self.course)
        if not missing:
            self.window_.message("Every file is where the course expects it.")
            return
        folder = QFileDialog.getExistingDirectory(self, f"Where are the {len(missing)} missing files now?",
                                                  self.course.get("folder") or "")
        if not folder:
            return
        course = self.course
        run_job(lambda: courses.relink(course, Path(folder)), self._on_relinked,
                lambda message: self.window_.message(f"Couldn't look there: {message}"))

    def _on_relinked(self, count: int) -> None:
        left = len(courses.missing_paths(self.course))
        self.window_.message(f"Found {count} file{'s' if count != 1 else ''}" + (f"; {left} still missing." if left else "."))
        self._after_structure_change()
        self._refresh_files()

    # ------------------ lengths (ffmpeg) ------------------
    def _probe_lengths(self) -> None:
        """Lessons whose videos' lengths aren't known yet learn them in the background (ffmpeg, if it's there)."""
        videos = sorted({l["video"] for l in courses.lessons(self.course) if l.get("video") and not l.get("duration")})
        if not videos:
            return
        course = self.course

        def work() -> dict[str, int]:
            found = {}
            for video in videos:
                details = probe_video(Path(video)) if Path(video).is_file() else None
                if details is not None:
                    found[video] = details[0]
            return found

        run_job(work, lambda found: self._on_lengths(course, found), lambda _message: None)

    def _on_lengths(self, course: dict, found: dict) -> None:
        if course is not self.course or not found:
            return
        for lesson in courses.lessons(course):
            if lesson.get("video") in found and not lesson.get("duration"):
                lesson["duration"] = found[lesson["video"]]
        self.lessons.viewport().update()
        self._show_header()
        self._changed()

    def leave(self) -> None:
        """Leaving the course (another one, the Courses page, or closing): where you were is kept, and the course is let
        go (so a course deleted from the Courses page afterwards isn't saved back)."""
        if self.lesson is not None:
            self._record(self.position())
        self.player.set_fullscreen(False)
        self.player.stop()
        if self.web is not None:
            self.web.stop()
        self.mode = None
        self.save()
        self.course = None
        self.lesson = None
        self.lessons.current_id = None
        self.lessons.clear()

    def set_video_only(self, on: bool) -> None:
        """The player's full screen: only the video and its controls."""
        for widget in (self.left_pane, self.right_pane, self.title, self.subtitle, self.btn_previous, self.btn_next):
            widget.setVisible(not on)
        self.layout().setContentsMargins(*((0, 0, 0, 0) if on else (12, 8, 12, 12)))
        if on:
            self.cuts.hide()
            self.cuts_hint.hide()
            self.web_bar.hide()
        else:
            self._sync_cuts()
            self._sync_web_bar()


class CourseWindow(QMainWindow):
    """See the module docstring. panel: the Artbook (ui.artbook.ArtbookPanel); sender: frames to the Artbook
    (ui.asset_menu.ArtbookSender); show_artbook(): brings the Artbook into view in the main window."""

    closed = Signal()

    def __init__(self, panel=None, sender=None, show_artbook=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.panel = panel
        self.artbook_sender = sender
        self.show_artbook = show_artbook
        self.setWindowTitle("Courses · Asset Manager")
        self.resize(1400, 860)
        state = get_course_window()
        if state["geometry"]:
            self.restoreGeometry(QByteArray(base64.b64decode(state["geometry"])))
        root = QWidget(self)
        column = QVBoxLayout(root)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        bar = QWidget(root)
        self.top_bar = bar
        bar.setObjectName("courseBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 8, 12, 8)
        self.btn_back = ghost_button("back", "Back to every course", "Courses")
        self.btn_back.clicked.connect(self.show_home)
        row.addWidget(self.btn_back)
        self.heading = QLabel("Courses", bar)
        self.heading.setObjectName("dialogTitle")
        row.addWidget(self.heading, 1)
        self.btn_new = QPushButton("New Course", bar)
        self.btn_new.setObjectName("primaryBtn")
        self.btn_new.setToolTip("An empty course: drag videos and files into it")
        self.btn_new.clicked.connect(self.new_course)
        self.btn_open_folder = ghost_button("folder", "Draft a course from a folder of lesson videos (archives in it are "
                                            "unzipped first, if you like)", "Open Folder as Course...")
        self.btn_open_folder.clicked.connect(self.open_folder)
        self.btn_edit = ghost_button("edit", "Edit the course: order and rename lessons, give them their files, cut a "
                                     "long video into lessons", "Edit Course", checkable=True)
        self.btn_edit.toggled.connect(self._on_edit_toggled)
        self.btn_page = ghost_button("view_artbook", "Open the course's Artbook page (your notes, with their frames)",
                                     "Open the Artbook Page")
        self.btn_page.clicked.connect(self.open_artbook_page)
        for button in (self.btn_new, self.btn_open_folder, self.btn_edit, self.btn_page):
            row.addWidget(button)
        column.addWidget(bar)
        self.stack = QStackedWidget(root)
        home = QWidget(self.stack)
        home_column = QVBoxLayout(home)
        home_column.setContentsMargins(20, 12, 20, 12)
        self.home_hint = QLabel(home)
        self.home_hint.setObjectName("pageHint")
        self.home_hint.setWordWrap(True)
        home_column.addWidget(self.home_hint)
        self.cards = CourseCards(home)
        self.cards.open_course.connect(self._on_card)
        self.cards.menu_requested.connect(self._card_menu)
        home_column.addWidget(self.cards, 1)
        self.stack.addWidget(home)
        self.page = CoursePage(self)
        self.page.player.fullscreen_changed.connect(self._on_video_fullscreen)
        self._was_maximized = False
        self.stack.addWidget(self.page)
        column.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.toast = QLabel(self)
        self.toast.setObjectName("boardToast")
        self.toast.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.toast.setWordWrap(True)
        self.toast.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.toast.hide()
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.setInterval(MESSAGE_MS)
        self._toast_timer.timeout.connect(self.toast.hide)
        for shortcut_id, slot in (("course.note", self.page.start_note), ("course.cut", self.page.cut_here),
                                  ("course.remove", self.page.remove_selected)):
            action = QAction(self)
            action.setShortcut(shortcuts.sequence(shortcut_id))
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            action.triggered.connect(slot)
            self.addAction(action)
        self.show_home()

    # ------------------ the Courses page ------------------
    def show_home(self) -> None:
        if self.stack.currentWidget() is self.page:
            self.page.leave()
        self.btn_edit.setChecked(False)
        items = courses.list_courses()
        self.cards.fill(items)
        self.home_hint.setText("Your courses, the one you watched last first. Click a course's button to carry on where "
                               "you left off." if items else
                               "No courses yet. Open Folder as Course... drafts one from a folder of lesson videos (zips "
                               "in it get unzipped first, if you like); New Course starts an empty one to drag videos into.")
        self.stack.setCurrentIndex(0)
        self.heading.setText("Courses")
        self.setWindowTitle("Courses · Asset Manager")
        for widget, on in ((self.btn_back, False), (self.btn_new, True), (self.btn_open_folder, True),
                           (self.btn_edit, False), (self.btn_page, False)):
            widget.setVisible(on)
        for course in items:
            if course["id"] not in self.cards.covers:
                self._load_cover(course)

    def _load_cover(self, course: dict) -> None:
        first = next((l["video"] for l in courses.lessons(course) if l.get("video") and Path(l["video"]).is_file()), None)
        if first is None:
            return
        course_id = course["id"]
        run_job(lambda: extract_poster_frame(Path(first)), lambda path: self._on_cover(course_id, path), lambda _m: None)

    def _on_cover(self, course_id: str, path) -> None:
        if path is not None:
            picture = QPixmap(str(path))
            if not picture.isNull():
                self.cards.set_cover(course_id, picture)

    def _on_card(self, course_id: str, carry_on: bool) -> None:
        self.open_course(course_id, play=carry_on)

    def _card_menu(self, course_id: str, at: QPoint) -> None:
        menu = QMenu(self)
        menu.addAction("Open", lambda: self.open_course(course_id))
        menu.addAction("Rename...", lambda: self.rename_course(course_id))
        try:
            course = courses.load_course(course_id)
        except courses.CourseError:
            course = None
        if course is not None and course.get("folder"):
            menu.addAction("Show Folder", lambda: file_actions.open_file(Path(course["folder"])))
        menu.addSeparator()
        menu.addAction("Delete Course...", lambda: self.delete_course(course_id))
        menu.exec(at)
        menu.deleteLater()

    def rename_course(self, course_id: str) -> None:
        course = courses.load_course(course_id)
        name, ok = QInputDialog.getText(self, "Rename Course", "Name:", text=course["name"])
        if ok and name.strip():
            course["name"] = name.strip()[:courses.MAX_NAME]
            courses.save_course(course)
            self.show_home()

    def delete_course(self, course_id: str) -> None:
        course = courses.load_course(course_id)
        answer = QMessageBox.question(self, "Delete Course",
                                      f"Move “{course['name']}” to the Recycle Bin? Its videos and files stay "
                                      "where they are; its lesson list, progress and notes go.")
        if answer == QMessageBox.StandardButton.Yes:
            if self.page.course is not None and self.page.course["id"] == course_id:
                self.page.leave()  # (let go of it first: nothing saves it back)
            try:
                courses.trash_course(course_id)
            except courses.CourseError as exc:
                self.message(str(exc))
            self.show_home()

    def new_course(self) -> None:
        name, ok = QInputDialog.getText(self, "New Course", "Name:", text="New course")
        if not ok:
            return
        course = courses.new_course(name)
        courses.save_course(course)
        self.open_course(course["id"], edit=True)
        self.message("Drag videos onto the lesson list (or Add Videos...); each becomes a lesson.")

    def open_folder(self, folder: Optional[str] = None) -> None:
        """Open Folder as Course...: its archives unzipped first (if you like), then a draft of it, in Edit."""
        folder = folder or QFileDialog.getExistingDirectory(self, "Open Folder as Course")
        if not folder:
            return
        path = Path(folder)
        existing = next((c for c in courses.list_courses() if c.get("folder") and Path(c["folder"]) == path), None)
        if existing is not None:
            answer = QMessageBox.question(self, "Open Folder as Course",
                                          f"“{existing['name']}” is already a course made from that folder. Open it?"
                                          " (No makes another.)")
            if answer == QMessageBox.StandardButton.Yes:
                self.open_course(existing["id"])
                return
        found = course_unzip.find_archives(path)
        if found:
            answer = QMessageBox.question(self, "Open Folder as Course",
                                          f"That folder has {len(found)} archive{'s' if len(found) != 1 else ''} (zips). "
                                          "Unzip them first, where they are? Every file is checked afterwards.")
            if answer == QMessageBox.StandardButton.Yes:
                from ui.course_unzip import UnzipDialog

                dialog = UnzipDialog(path, found, self)
                dialog.exec()
                dialog.deleteLater()
        self.message("Drafting the course...")
        run_job(lambda: courses.draft_from_folder(path), self._on_drafted, self._on_draft_failed)

    def _on_drafted(self, course: dict) -> None:
        courses.save_course(course)
        self.open_course(course["id"], edit=bool(course["loose"]))
        flat = list(courses.lessons(course))
        placed = sum(len(lesson["files"]) for lesson in flat)
        with_files = sum(1 for lesson in flat if lesson["files"])
        loose = len(course["loose"])
        text = f"{len(flat)} lesson{'s' if len(flat) != 1 else ''}"
        if placed:
            text += f"; {placed} file{'s' if placed != 1 else ''} went with {with_files} of them (each lesson's show beside it)"
        text += (f"; {loose} {'is' if loose == 1 else 'are'} left to place: drag {'it' if loose == 1 else 'each'} onto its lesson."
                 if loose else ". Edit Course to change anything.")
        self.message(text)

    def _on_draft_failed(self, message: str) -> None:
        self.message(f"Couldn't make a course of it: {message}")

    # ------------------ a course ------------------
    def open_course(self, course_id: str, play: bool = False, edit: bool = False) -> None:
        try:
            course = courses.load_course(course_id)
        except courses.CourseError as exc:
            self.message(str(exc))
            return
        self.stack.setCurrentWidget(self.page)
        for widget, on in ((self.btn_back, True), (self.btn_new, False), (self.btn_open_folder, False),
                           (self.btn_edit, True), (self.btn_page, True)):
            widget.setVisible(on)
        self.heading.setText(course["name"])
        self.setWindowTitle(f"{course['name']} · Courses")
        self.btn_edit.blockSignals(True)
        self.btn_edit.setChecked(edit)
        self.btn_edit.blockSignals(False)
        self.page.set_editing(edit)
        self.btn_edit.setText("Done" if edit else "Edit Course")
        self.page.show_course(course, play)
        set_course_window(open=True)

    def _on_video_fullscreen(self, on: bool) -> None:
        """The player's full-screen button (F, double-click; YouTube's own button too): the window full screen, showing
        only the video."""
        self.top_bar.setVisible(not on)
        self.page.set_video_only(on)
        if on:
            self._was_maximized = self.isMaximized()
            self.showFullScreen()
        elif self._was_maximized:
            self.showMaximized()
        else:
            self.showNormal()

    def _on_edit_toggled(self, on: bool) -> None:
        self.page.set_editing(on)
        self.btn_edit.setText("Done" if on else "Edit Course")
        set_icon(self.btn_edit, "check" if on else "edit")
        self.page.save()

    def open_artbook_page(self) -> None:
        course = self.page.course
        if course is None:
            return
        try:
            page_id = course_page.ensure_page(course, self.panel)
        except Exception as exc:  # (a page folder that can't be written...)
            self.message(f"Couldn't make the Artbook page: {exc}")
            return
        if self.panel is not None:
            self.panel.open_page(page_id)
            if self.show_artbook is not None:
                self.show_artbook()

    # ------------------ messages and closing ------------------
    def message(self, text: str) -> None:
        self.toast.setText(text)
        width = min(620, self.width() - 40)
        self.toast.setFixedWidth(width)
        self.toast.adjustSize()
        self.toast.move((self.width() - width) // 2, self.height() - self.toast.height() - 24)
        self.toast.show()
        self.toast.raise_()
        self._toast_timer.start()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.page.leave()
        set_course_window(geometry=base64.b64encode(bytes(self.saveGeometry().data())).decode("ascii"), open=False)
        super().closeEvent(event)
        self.closed.emit()

    def shutdown(self) -> None:
        """The app is quitting: keep where you were, and whether the window was open (it reopens then)."""
        was_open = self.isVisible()
        self.close()
        set_course_window(open=was_open)
