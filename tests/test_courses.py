"""Courses (Course View): drafting a course from a folder (lesson numbers, files with their lessons, the Course files
tray), progress, notes, cutting a long video into lessons, relinking, unzipping a course's archives and checking every
file, and the window itself (notes onto the course's Artbook page, editing, Ctrl+5)."""

from __future__ import annotations

import os
import time
import zipfile
from pathlib import Path

import pytest

from core import courses
from tests.test_artbook_cards import panel, wait_for  # noqa: F401  (shared fixtures)


def touch(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def lesson_names(course) -> list[str]:
    return [lesson["name"] for lesson in courses.lessons(course)]


# ----------------------------------------------------------------------
# Names and numbers
# ----------------------------------------------------------------------
@pytest.mark.parametrize("name, number", [
    ("05_Head_Blockout.mp4", 5), ("Lesson 3 - Wings.mp4", 3), ("Part 12.mkv", 12), ("02_SettingUpZBrush", 2),
    ("Creating the head.mp4", None), ("02-19", None), ("videos_01-17.zip", None), ("Intro (part 2).mp4", 2),
])
def test_lesson_numbers(name, number):
    assert courses.lesson_number(name) == number


def test_lesson_titles_from_file_names():
    assert courses.clean_title("05_HeadBlockout") == "Head Blockout"
    assert courses.clean_title("Lesson 3 - Wings") == "Wings"
    assert courses.clean_title("20_SculptingProduction01") == "Sculpting Production 01"
    assert courses.clean_title("07") == "07"  # (nothing else to go by)


# ----------------------------------------------------------------------
# A draft from a folder
# ----------------------------------------------------------------------
def test_a_numbered_course_folder_gets_its_lessons_and_files(tmp_path):
    root = tmp_path / "Stylized Sculpting"
    for name in ("10_Wrap up.mp4", "01_Welcome.mp4", "02_SettingUpZBrush.mp4"):
        touch(root / "videos_01-17" / name)
    front = touch(root / "02-19" / "02_SettingUpZBrush" / "concept" / "front.png")
    tool = touch(root / "10_final.ztl")
    notes = touch(root / "Course notes.pdf")
    stray = touch(root / "hands_v2.ztl")
    for n in range(5):
        touch(root / "Alphas" / f"crack_{n}.png")
    course = courses.draft_from_folder(root)
    assert course["name"] == "Stylized Sculpting" and len(course["chapters"]) == 1  # (a range folder isn't a chapter)
    assert lesson_names(course) == ["Welcome", "Setting Up ZBrush", "Wrap up"]  # (by number: 10 after 2)
    flat = list(courses.lessons(course))
    assert [lesson["number"] for lesson in flat] == ["01", "02", "10"]
    assert flat[1]["files"] == [str(front)] and flat[2]["files"] == [str(tool)]  # (by their folder's / their own number)
    assert course["files"] == [str(notes)]  # (a document with no number: the whole course's)
    assert str(stray) in course["loose"] and str(root / "Alphas") in course["loose"]  # (a folder of alphas: one entry)


def test_unnumbered_videos_go_in_name_order_or_by_date(tmp_path):
    named = tmp_path / "named"
    for name in ("Part 10.mp4", "Part 2.mp4", "Intro.mp4"):
        touch(named / name)
    assert lesson_names(courses.draft_from_folder(named))[0] == "Intro"
    dated = tmp_path / "dated"
    for n, name in enumerate(("zebra.mp4", "apple.mp4", "mango.mp4")):
        os.utime(touch(dated / name), (time.time() - 1000 + n * 100,) * 2)
    assert lesson_names(courses.draft_from_folder(dated)) == ["zebra", "apple", "mango"]  # (names say nothing: dates)


def test_folders_of_videos_become_chapters(tmp_path):
    root = tmp_path / "course"
    touch(root / "Basics" / "intro.mp4")
    touch(root / "Basics" / "tools.mp4")
    touch(root / "Advanced" / "hair.mp4")
    course = courses.draft_from_folder(root)
    assert [c["name"] for c in course["chapters"]] == ["Advanced", "Basics"]


def test_a_folder_with_no_videos_isnt_a_course(tmp_path):
    touch(tmp_path / "a.zip")
    with pytest.raises(courses.CourseError, match="no videos"):
        courses.draft_from_folder(tmp_path)


# ----------------------------------------------------------------------
# Progress, notes, saving
# ----------------------------------------------------------------------
def test_a_lesson_is_done_at_90_percent_and_next_up_follows(tmp_path):
    course = courses.new_course("Hair")
    course["chapters"][0]["lessons"] = [courses.new_lesson(n, str(tmp_path / f"{n}.mp4")) for n in ("a", "b", "c")]
    a, b, c = courses.lessons(course)
    for lesson in (a, b, c):
        lesson["duration"] = 100_000
    assert courses.next_up(course) is a
    assert not courses.record_watch(a, 50_000) and a["watched"] == 0.5
    assert courses.record_watch(a, 91_000) and a["done"]
    course["last_lesson"] = a["id"]
    assert courses.next_up(course) is b
    assert courses.progress(course) == (1, 3, 200_000)


def test_courses_save_and_load_and_a_damaged_one_is_left_out(tmp_path):
    course = courses.new_course("Portrait")
    lesson = courses.new_lesson("Eyes", "eyes.mp4")
    course["chapters"][0]["lessons"].append(lesson)
    courses.add_note(lesson, 61_000, "Upper lid overlaps the lower one")
    courses.save_course(course)
    again = courses.load_course(course["id"])
    assert again["chapters"][0]["lessons"][0]["notes"][0]["text"] == "Upper lid overlaps the lower one"
    (courses.COURSES_DIR / f"{'0' * 32}.json").write_text("{not json", encoding="utf-8")
    assert [c["id"] for c in courses.list_courses()] == [course["id"]]
    courses.trash_course(course["id"])
    assert courses.list_courses() == []


def test_a_long_video_is_cut_into_lessons_and_joined_again():
    course = courses.new_course("Stream")
    stream = courses.new_lesson("Livestream", "stream.mp4")
    stream["duration"] = 3_600_000
    course["chapters"][0]["lessons"].append(stream)
    courses.add_note(stream, 600_000, "early")
    courses.add_note(stream, 2_000_000, "late")
    part = courses.cut_lesson(course, stream["id"], 1_200_000)
    assert part is not None and (stream["start"], stream["end"]) == (0, 1_200_000)
    assert (part["start"], part["end"]) == (1_200_000, None) and courses.lesson_length(part) == 2_400_000
    assert [n["text"] for n in stream["notes"]] == ["early"] and [n["text"] for n in part["notes"]] == ["late"]
    assert courses.cut_lesson(course, stream["id"], 500) is None  # (too near its start)
    assert courses.move_cut(course, stream["id"], 2_100_000)
    assert [n["text"] for n in stream["notes"]] == ["early", "late"] and part["start"] == 2_100_000
    assert courses.join_with_next(course, stream["id"])
    assert stream["end"] is None and len(list(courses.lessons(course))) == 1


def test_moved_files_are_found_again_by_name(tmp_path):
    course = courses.new_course("Moved")
    lesson = courses.new_lesson("A", str(tmp_path / "old" / "a.mp4"))
    lesson["files"] = [str(tmp_path / "old" / "a.ztl")]
    course["chapters"][0]["lessons"].append(lesson)
    video, tool = touch(tmp_path / "new" / "deep" / "a.mp4"), touch(tmp_path / "new" / "a.ztl")
    assert len(courses.missing_paths(course)) == 2
    assert courses.relink(course, tmp_path / "new") == 2
    assert lesson["video"] == str(video) and lesson["files"] == [str(tool)] and not courses.missing_paths(course)


# ----------------------------------------------------------------------
# Unzipping a course and checking every file
# ----------------------------------------------------------------------
def make_zip(path: Path, files: dict[str, bytes], method=zipfile.ZIP_DEFLATED) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", method) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


def test_archives_unzip_where_they_are_and_every_file_is_checked(tmp_path):
    from core import course_unzip as unzip

    nested = make_zip(tmp_path / "course_videos_01-02.zip", {"videos/01_Intro.mp4": b"a" * 5000, "videos/02_Tools.mp4": b"b" * 7000})
    loose = make_zip(tmp_path / "course_scenes.zip", {"05_head.ztl": b"z" * 3000, "refs/front.png": b"p" * 100})
    assert unzip.find_archives(tmp_path) == [loose, nested]
    plans = unzip.plan([nested, loose])
    assert plans[0].target == tmp_path and plans[1].target == tmp_path / "course_scenes"  # (loose files: a folder of its own)
    assert unzip.space_needed(plans) == 15100
    seen = []
    results = [unzip.unzip(p, progress=lambda done, total: seen.append((done, total))) for p in plans]
    assert all(r.verified for r in results) and [r.checked for r in results] == [2, 2]
    assert (tmp_path / "videos" / "02_Tools.mp4").read_bytes() == b"b" * 7000
    assert (tmp_path / "course_scenes" / "05_head.ztl").is_file()
    assert seen and seen[-1][0] == seen[-1][1]
    again = unzip.unzip(unzip.plan([nested])[0])  # (unzipped already, the same files: still checked)
    assert again.verified
    moved, freed, failed = unzip.trash_archives([nested, loose])
    assert moved == 2 and freed > 0 and not failed and not nested.exists() and not loose.exists()


def test_a_different_file_in_the_way_is_kept_and_the_archive_isnt_offered_for_deleting(tmp_path):
    from core import course_unzip as unzip

    archive = make_zip(tmp_path / "scenes.zip", {"scenes/a.ztl": b"from the zip", "scenes/b.ztl": b"b"})
    mine = touch(tmp_path / "scenes" / "a.ztl", b"my own edits")
    result = unzip.unzip(unzip.plan([archive])[0])
    assert result.kept == ["scenes/a.ztl"] and result.checked == 1 and not result.verified
    assert mine.read_bytes() == b"my own edits"


def test_a_damaged_file_in_an_archive_isnt_checked(tmp_path):
    from core import course_unzip as unzip

    archive = make_zip(tmp_path / "videos.zip", {"v/01.mp4": b"a" * 4000, "v/02.mp4": b"b" * 4000}, zipfile.ZIP_STORED)
    data = bytearray(archive.read_bytes())
    at = data.find(b"b" * 4000)
    data[at + 100] = ord("X")  # (one byte of the second file damaged: its CRC-32 no longer matches)
    archive.write_bytes(bytes(data))
    result = unzip.unzip(unzip.plan([archive])[0])
    assert not result.verified and result.failed and result.checked == 1


def test_the_unzip_window_unzips_checks_and_offers_the_recycle_bin(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from ui.course_unzip import UnzipDialog

    archive = make_zip(tmp_path / "course.zip", {"course/01_Intro.mp4": b"v" * 2000, "course/notes.pdf": b"%PDF"})
    dialog = UnzipDialog(tmp_path, [archive])
    assert wait_for(qapp, lambda: dialog.btn_unzip.isEnabled())
    assert "needed" in dialog.space.text()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    dialog.delete_after.setChecked(True)
    dialog.btn_unzip.click()
    assert wait_for(qapp, lambda: dialog.btn_unzip.text() == "Done")
    assert dialog.results[0].verified and dialog.trashed == 1 and not archive.exists()
    assert (tmp_path / "course" / "01_Intro.mp4").is_file()
    assert "Unzipped" in dialog.rows.topLevelItem(0).text(1)
    dialog.deleteLater()


# ----------------------------------------------------------------------
# The window
# ----------------------------------------------------------------------
@pytest.fixture
def course_window(qapp, panel):
    from ui.course_window import CourseWindow

    window = CourseWindow(panel, None, None)
    window.resize(1300, 800)
    window.show()
    qapp.processEvents()
    yield window
    window.close()
    window.deleteLater()


def made_course(tmp_path) -> dict:
    root = tmp_path / "Hair for Games"
    for name in ("01_Intro.mp4", "02_Clumps.mp4"):
        touch(root / name)
    touch(root / "02_clumps.ztl")
    touch(root / "stray.ma")
    return root


def test_open_folder_as_course_drafts_it_and_puts_leftovers_in_the_tray(qapp, course_window, tmp_path):
    root = made_course(tmp_path)
    course_window.open_folder(str(root))
    page = course_window.page
    assert wait_for(qapp, lambda: page.course is not None)
    assert page.editing and course_window.btn_edit.isChecked()  # (files to place: it opens in Edit)
    assert page.tray.paths() == [str(root / "stray.ma")]
    lessons = list(courses.lessons(page.course))
    page.lessons.select(lessons[1]["id"])  # (editing, a lesson picked: its files show above the tray)
    assert page.tray.paths() == [str(root / "02_clumps.ztl"), str(root / "stray.ma")]
    page.lessons.select(lessons[0]["id"])
    # editing: drop the stray file on lesson 01, rename it, take lesson 02 out (into the tray)
    page._on_files_dropped("lesson", lessons[0]["id"], [str(root / "stray.ma")])
    assert lessons[0]["files"] == [str(root / "stray.ma")] and page.course["loose"] == []
    page._on_renamed("lesson", lessons[0]["id"], "Welcome")
    page.lessons.select(lessons[1]["id"])
    page.remove_selected()
    assert lesson_names(page.course) == ["Welcome"] and str(root / "02_Clumps.mp4") in page.course["loose"]
    course_window.btn_edit.click()  # (Done)
    assert not page.editing
    page.save()
    assert courses.load_course(page.course["id"])["chapters"][0]["lessons"][0]["name"] == "Welcome"
    course_window.show_home()
    assert course_window.cards.count() == 1


def test_a_note_goes_in_the_course_and_on_its_artbook_page(qapp, course_window, panel, tmp_path):
    from core import artbook_store as store

    course = courses.draft_from_folder(made_course(tmp_path))
    courses.save_course(course)
    course_window.open_course(course["id"])
    page = course_window.page
    assert page.lesson is not None
    page.start_note()
    assert page.side_stack.currentIndex() == 1 and page.note_box.hasFocus() or page._note_at is not None
    page.note_box.setText("Clumps first, then strands")
    page._keep_note()
    assert [n["text"] for n in page.lesson["notes"]] == ["Clumps first, then strands"]
    page_id = page.course["artbook_page"]
    saved = store.load_page(page_id)
    texts = [d.get("text") for d in saved["items"] if d.get("kind") == "note"]
    assert saved["name"] == "Course: Hair for Games" and any("Clumps first" in (t or "") for t in texts)
    assert any(t == "01 · Intro" for t in texts)  # (the lesson's heading starts its row)
    # with the course's page open in the Artbook, the next note goes on the board
    panel.open_page(page_id)
    before = len(panel.board.items_list())
    page.start_note()
    page.note_box.setText("Second pass")
    page._keep_note()
    assert len(panel.board.items_list()) == before + 1
    assert courses.load_course(course["id"])["artbook_page"] == page_id


def test_clicking_a_lesson_s_box_ticks_it(qapp, course_window, tmp_path):
    course = courses.draft_from_folder(made_course(tmp_path))
    courses.save_course(course)
    course_window.open_course(course["id"])
    page = course_window.page
    first = next(courses.lessons(page.course))
    page._on_tick(first["id"])
    assert first["done"] and "1 of 2" in page.course_info.text()


def test_ctrl_5_opens_course_view(qapp, tmp_path):
    from PySide6.QtTest import QTest

    from core.settings import set_scan_folders
    from tests.test_main_window import open_window
    from ui import shortcuts

    touch(tmp_path / "library" / "lesson.mp4")
    set_scan_folders([tmp_path / "library"])

    window = open_window(qapp)
    try:
        QTest.keySequence(window, shortcuts.sequence("view.course"))
        assert wait_for(qapp, lambda: window.course_window is not None and window.course_window.isVisible())
        assert window.view_modes.course_action in window.btn_view.menu().actions()
    finally:
        if window.course_window is not None:
            window.course_window.close()
        window.close()


# ----------------------------------------------------------------------
# No numbers: the names' steps give the order, and place the files
# ----------------------------------------------------------------------
def test_unnumbered_lessons_follow_the_steps_their_names_speak_of(tmp_path):
    root = tmp_path / "Eyes"
    for name in ("BakingAndTexturing.mp4", "LightingAndRendering.mp4", "ModelingAndSculpting.mp4", "Promo.mp4", "Zoom1.mp4"):
        touch(root / "Videos" / name)
    sculpt = touch(root / "Zbrush" / "Eye.ZTL")
    scene = touch(root / "Maya" / "Eye_Basemodel.ma")
    bake_scene = touch(root / "Marmoset" / "Eye_Bake.tbscene")
    presentation = touch(root / "Marmoset" / "Eye_Presentation.tbscene")
    for n in range(6):
        touch(root / "Textures" / "Bakes" / f"eye_map_{n}.png")
    stray = touch(root / "FBX files" / "Eye_Mesh.fbx")
    course = courses.draft_from_folder(root)
    assert lesson_names(course) == ["Promo", "Modeling And Sculpting", "Baking And Texturing", "Lighting And Rendering", "Zoom 1"]
    promo, modeling, baking, lighting, zoom = courses.lessons(course)
    assert sorted(modeling["files"]) == sorted([str(scene), str(sculpt)])
    assert str(bake_scene) in baking["files"] and str(root / "Textures" / "Bakes") in baking["files"]  # (fifteen bakes: one entry)
    assert lighting["files"] == [str(presentation)]  # (a Marmoset scene with nothing in its name: rendering)
    assert course["loose"] == [str(stray)]


def test_steps_read_from_names():
    assert courses.stages_of("BakingAndTexturing") == [5, 6]
    assert courses.stages_of("MarmosetMaterialValues") == [9, 6]
    assert courses.stages_of("Highlights") == []  # (whole words only: not "light")


def test_unzipped_files_keep_their_dates_from_the_archive(tmp_path):
    from core import course_unzip as unzip

    archive = tmp_path / "videos.zip"
    with zipfile.ZipFile(archive, "w") as z:
        info = zipfile.ZipInfo("videos/01_intro.mp4", date_time=(2021, 3, 4, 5, 6, 8))
        z.writestr(info, b"v" * 100)
    assert unzip.unzip(unzip.plan([archive])[0]).verified
    when = time.localtime((tmp_path / "videos" / "01_intro.mp4").stat().st_mtime)
    assert (when.tm_year, when.tm_mon, when.tm_mday) == (2021, 3, 4)


def test_lists_paint_without_font_warnings_when_text_is_sized_in_pixels(qapp, tmp_path):
    from PySide6.QtCore import qInstallMessageHandler

    from ui.course_widgets import FileList, LessonList, NotesList

    course = courses.draft_from_folder(made_course(tmp_path))
    warnings = []
    previous = qInstallMessageHandler(lambda _kind, _context, text: warnings.append(text))
    try:
        for view in (LessonList(), FileList(), NotesList()):
            font = view.font()
            font.setPixelSize(13)  # (as the app's stylesheet sizes it: its point size reads -1)
            view.setFont(font)
            view.resize(360, 400)
            if isinstance(view, LessonList):
                view.fill(course)
            elif isinstance(view, FileList):
                view.fill([("course", "The whole course", [str(p) for p in made_course(tmp_path).iterdir()])])
            else:
                view.fill([{"id": "a", "at": 61_000, "text": "Upper lid overlaps the lower one"}])
            view.grab()
            view.deleteLater()
    finally:
        qInstallMessageHandler(previous)
    assert not [w for w in warnings if "QFont" in w]


def test_a_course_deleted_after_watching_it_stays_deleted(qapp, course_window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    course = courses.draft_from_folder(made_course(tmp_path))
    courses.save_course(course)
    course_window.open_course(course["id"])
    course_window.show_home()  # (Back to the Courses page)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    course_window.delete_course(course["id"])
    course_window.close()  # (closing used to save the course it had held back)
    assert courses.list_courses() == []
    # and deleting the one that's open lets go of it too
    again = courses.draft_from_folder(made_course(tmp_path))
    courses.save_course(again)
    course_window.show()
    course_window.open_course(again["id"])
    course_window.delete_course(again["id"])
    course_window.page.save()
    assert courses.list_courses() == []


def test_full_screen_shows_only_the_video(qapp, course_window, tmp_path):
    course = courses.draft_from_folder(made_course(tmp_path))
    courses.save_course(course)
    course_window.open_course(course["id"])
    page = course_window.page
    page.player.set_fullscreen(True)  # (its button, F or a double-click)
    qapp.processEvents()
    assert course_window.isFullScreen() and not page.left_pane.isVisible() and not course_window.top_bar.isVisible()
    assert page.player.isVisible()
    page.player.set_fullscreen(False)
    qapp.processEvents()
    assert not course_window.isFullScreen() and page.left_pane.isVisible() and course_window.top_bar.isVisible()


# ----------------------------------------------------------------------
# Web lessons: YouTube in the window, a video file's address in the player, the rest in the browser; yt-dlp
# ----------------------------------------------------------------------
@pytest.mark.parametrize("url, kind, video_id", [
    ("https://www.youtube.com/watch?v=aqz-KE-bpKQ&t=90", "youtube", "aqz-KE-bpKQ"),
    ("https://youtu.be/aqz-KE-bpKQ?si=x", "youtube", "aqz-KE-bpKQ"),
    ("https://www.youtube.com/shorts/aqz-KE-bpKQ", "youtube", "aqz-KE-bpKQ"),
    ("https://vimeo.com/1084537", "vimeo", "1084537"),
    ("https://example.com/lessons/03_wings.mp4", "file", None),
    ("https://www.artstation.com/learning/courses/abc/lesson-3", "page", None),
])
def test_web_addresses_are_recognised(url, kind, video_id):
    from core import course_links

    found = course_links.classify(url)
    assert found["kind"] == kind and found.get("id") == video_id


def test_youtube_start_times_and_the_embedded_player_page():
    from core import course_links

    assert course_links.start_seconds("https://youtu.be/aqz-KE-bpKQ?t=1m30s") == 90
    assert course_links.start_seconds("https://www.youtube.com/watch?v=aqz-KE-bpKQ&t=75") == 75
    page = course_links.embed_page("aqz-KE-bpKQ", 90)
    assert "videoId: 'aqz-KE-bpKQ'" in page and "start: 90" in page and "function am_seek" in page
    with pytest.raises(ValueError):
        course_links.embed_page("x'); alert(1); ('")


def test_titles_and_lengths_come_from_the_sites_oembed(monkeypatch):
    from core import course_links

    asked = []
    monkeypatch.setattr(course_links, "_get_json", lambda url: asked.append(url) or {"title": " Eye modeling ", "duration": 600})
    found = course_links.details("https://vimeo.com/1084537")
    assert found == {"title": "Eye modeling", "duration": 600_000} and asked[0].startswith("https://vimeo.com/api/oembed.json?url=")


class FakeProcess:
    """yt-dlp, as far as download() can tell."""

    def __init__(self, command, lines, made=None, code=0):
        import io

        self.command, self.returncode = command, None
        self.stdout = io.BytesIO("".join(f"{line}\n" for line in lines).encode())
        self._made, self._code = made, code

    def poll(self):
        return self.returncode

    def wait(self):
        if self._made is not None:
            touch(self._made, b"mp4")
        self.returncode = self._code
        return self._code

    def kill(self):
        self.returncode = -9


def test_yt_dlp_downloads_a_lesson_for_offline(tmp_path, monkeypatch):
    from core import course_links

    made = {}

    def popen(command, **_options):
        made["command"] = command
        return FakeProcess(command, ["download:  12.5%", "download: 100.0%", "[Merger] Merging formats"],
                           made=tmp_path / "05 Head blockout.mp4")

    monkeypatch.setattr(course_links.subprocess, "Popen", popen)
    seen = []
    path = course_links.download("https://youtu.be/aqz-KE-bpKQ", tmp_path, "05 Head blockout",
                                 progress=lambda done, total: seen.append(done), exe="yt-dlp")
    assert path == tmp_path / "05 Head blockout.mp4" and seen == [125, 1000]
    command = made["command"]
    assert command[0] == "yt-dlp" and "--no-playlist" in command and command[-1] == "https://youtu.be/aqz-KE-bpKQ"
    assert command[command.index("--merge-output-format") + 1] == "mp4"
    monkeypatch.setattr(course_links.subprocess, "Popen",
                        lambda command, **_o: FakeProcess(command, ["ERROR: [youtube] Sign in to confirm your age"], code=1))
    with pytest.raises(course_links.DownloadError, match="Sign in to confirm your age"):
        course_links.download("https://youtu.be/aqz-KE-bpKQ", tmp_path, "x", exe="yt-dlp")


@pytest.fixture
def web_course(course_window, monkeypatch):
    from core import course_links
    from ui import course_web

    monkeypatch.setattr(course_web, "web_view_available", lambda: False)  # (no web view in tests: the browser card)
    monkeypatch.setattr(course_links, "details", lambda url: {"title": f"Title of {course_links.classify(url)['kind']}"})
    monkeypatch.setattr(course_links, "find_ytdlp", lambda: None)
    course = courses.new_course("Online")
    courses.save_course(course)
    course_window.open_course(course["id"], edit=True)
    return course_window


def test_links_become_web_lessons_with_their_titles(qapp, web_course):
    page = web_course.page
    made = page.add_links(["https://vimeo.com/1084537", "https://example.com/v/03.mp4", "https://youtu.be/aqz-KE-bpKQ"])
    assert [lesson["url"] for lesson in made] == ["https://vimeo.com/1084537", "https://example.com/v/03.mp4", "https://youtu.be/aqz-KE-bpKQ"]
    assert wait_for(qapp, lambda: lesson_names(page.course) == ["Title of vimeo", "Title of file", "Title of youtube"])
    page.save()
    assert courses.load_course(page.course["id"])["chapters"][0]["lessons"][0]["url"] == "https://vimeo.com/1084537"


def test_a_vimeo_lesson_plays_in_the_browser_and_is_ticked_by_hand(qapp, web_course, monkeypatch):
    from PySide6.QtGui import QDesktopServices

    page = web_course.page
    vimeo, = page.add_links(["https://vimeo.com/1084537"])
    web_course.btn_edit.click()  # (Done)
    page.open_lesson(vimeo["id"], play=True)
    assert page.mode == "browser" and page.stage.currentWidget() is page.browser
    assert "Vimeo" in page.browser.about.text() and page.web_bar.isVisible()
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    page.browser.btn_open.click()
    assert opened == ["https://vimeo.com/1084537"]
    page.browser.btn_done.click()
    assert vimeo["done"] and page.browser.btn_done.text() == "Mark as Not Done"
    page.start_note()  # (no moment to stamp: a note on the lesson)
    page.note_box.setText("Watch the iris part twice")
    page._keep_note()
    assert vimeo["notes"][0]["at"] is None and vimeo["notes"][0]["text"] == "Watch the iris part twice"


def test_a_video_file_address_plays_in_the_apps_player(qapp, web_course, monkeypatch):
    page = web_course.page
    lesson, = page.add_links(["https://example.com/v/03.mp4"])
    played = []
    monkeypatch.setattr(page.player, "load_url", lambda url, autoplay=False, position=0: played.append((url, position)))
    page.open_lesson(lesson["id"])
    assert page.mode == "url" and played == [("https://example.com/v/03.mp4", 0)]


def test_download_for_offline_needs_yt_dlp_and_then_plays_the_copy(qapp, web_course, tmp_path, monkeypatch):
    from core import course_links

    page = web_course.page
    lesson, = page.add_links(["https://vimeo.com/1084537"])
    assert wait_for(qapp, lambda: lesson["name"] == "Title of vimeo")
    page.open_lesson(lesson["id"], play=False)
    assert page.btn_download.isHidden()  # (no yt-dlp: no download)
    monkeypatch.setattr(course_links, "find_ytdlp", lambda: "yt-dlp")
    page.course["download_folder"] = str(tmp_path)
    page._sync_web_bar()
    assert not page.btn_download.isHidden()

    def fake_download(url, folder, name, progress=None, should_stop=None):
        progress(500, 1000)
        return touch(folder / f"{name}.mp4", b"mp4")

    monkeypatch.setattr(course_links, "download", fake_download)
    page.btn_download.click()
    assert wait_for(qapp, lambda: lesson.get("video"))
    assert Path(lesson["video"]).name == "Title of vimeo.mp4" and page.mode == "file"
    assert courses.load_course(page.course["id"])["chapters"][0]["lessons"][0]["video"] == lesson["video"]


def test_a_notes_frame_loses_the_black_bars_round_the_video(qapp):
    from PySide6.QtGui import QColor, QImage

    from ui.course_web import trim_black

    image = QImage(400, 300, QImage.Format.Format_RGB32)
    image.fill(QColor("black"))
    for y in range(75, 225):  # (a 16:9 picture letterboxed in a 4:3 player)
        for x in range(400):
            image.setPixelColor(x, y, QColor(120, 160, 90))
    trimmed = trim_black(image)
    assert (trimmed.width(), trimmed.height()) == (400, 150)
