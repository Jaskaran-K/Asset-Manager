"""Importing into a Maya or Blender that's already open (core.app_helpers), and apps added by hand in
Settings > Apps. The helper scripts run here against stand-ins for bpy and maya; nothing is started,
and nothing is added to a real Maya or Blender (tests/conftest.py)."""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QImage

from core import app_helpers, app_icons, apps
from core.settings import get_custom_apps, get_app_types, get_file_extensions, set_custom_apps, set_scan_folders
from core.thumbnail_manager import ThumbnailManager
from test_app_files import app_named, asset_named, card_of, color_share, fake_program, is_red, menu_of, open_in_entries, press, viewer_buttons
from test_main_window import open_window


# ----------------------------------------------------------------------
# The helper scripts
# ----------------------------------------------------------------------
@pytest.mark.parametrize("app_id", ["maya", "blender"])
def test_the_helpers_are_valid_python_that_older_apps_can_run(app_id):
    source = app_helpers.helper_source(app_id)
    ast.parse(source, feature_version=(3, 7))  # (Maya and Blender bring their own Python)
    assert "@" not in source
    assert repr(str(app_helpers.mailbox(app_id))) in source


@pytest.fixture
def blender_helper(tmp_path, monkeypatch):
    """The Blender helper, loaded with a stand-in for bpy; imported: what it imported."""
    imported = []
    ops = types.SimpleNamespace(
        wm=types.SimpleNamespace(obj_import=lambda *a, **k: imported.append(("obj", a, k))),
        import_scene=types.SimpleNamespace(
            fbx=lambda *a, **k: imported.append(("fbx", a, k)), gltf=lambda *a, **k: imported.append(("gltf", a, k)),
        ),
    )
    context = types.SimpleNamespace(window_manager=types.SimpleNamespace(windows=[]))
    registered = []
    timers = types.SimpleNamespace(
        register=lambda fn, **k: registered.append(fn), is_registered=lambda fn: fn in registered, unregister=registered.remove,
    )
    bpy = types.SimpleNamespace(ops=ops, context=context, app=types.SimpleNamespace(background=False, timers=timers))
    monkeypatch.setitem(sys.modules, "bpy", bpy)
    module = load_helper("blender", tmp_path)
    module.imported, module.registered, module.bpy_stub = imported, registered, bpy
    return module


def load_helper(app_id: str, folder: Path):
    path = folder / f"helper_{app_id}.py"
    path.write_text(app_helpers.helper_source(app_id), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(f"helper_{app_id}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def requests_left(app_id: str) -> list[str]:
    return [n for n in os.listdir(app_helpers.mailbox(app_id)) if not n.startswith(app_helpers.ALIVE_PREFIX)]


def model(folder: Path, name: str = "a.obj") -> Path:
    path = folder / name
    path.write_text("v 0 0 0\n", encoding="utf-8")
    return path


def test_blender_imports_whats_asked_for_once(blender_helper, tmp_path):
    awkward = model(tmp_path, "it's a & test (1).FBX")
    app_helpers.send("blender", model(tmp_path))
    app_helpers.send("blender", awkward)
    blender_helper.check()
    kinds = sorted((kind, kw["filepath"]) for kind, _args, kw in blender_helper.imported)
    assert kinds == [("fbx", str(awkward)), ("obj", str(tmp_path / "a.obj"))]
    assert requests_left("blender") == []
    blender_helper.check()
    assert len(blender_helper.imported) == 2


def test_blender_gives_the_importers_a_window(blender_helper, tmp_path):
    entered = []

    class Override:
        def __enter__(self):
            entered.append(True)

        def __exit__(self, *_exc):
            return False

    blender_helper.bpy_stub.context.window_manager.windows = ["window"]
    blender_helper.bpy_stub.context.temp_override = lambda window: Override()
    app_helpers.send("blender", model(tmp_path))
    blender_helper.check()
    assert entered == [True] and blender_helper.imported[0][1] == ("EXEC_DEFAULT", True)  # (undoable)


@pytest.mark.parametrize("name, data", [
    ("old", None),  # (asked for long ago)
    ("notes.txt", None),  # (only model files, whatever's asked)
    ("script.py", None),
    ("gone", "missing"),
    ("damaged", "not json"),
])
def test_a_request_that_cant_be_met_is_dropped(blender_helper, tmp_path, name, data):
    folder = app_helpers.mailbox("blender")
    folder.mkdir(parents=True)
    if data is None:
        file = tmp_path / ("a.obj" if name == "old" else name)
        file.write_text("x", encoding="utf-8")
        app_helpers.send("blender", file)
    elif data == "missing":
        app_helpers.send("blender", tmp_path / "gone.obj")
    else:
        (folder / f"x{app_helpers.REQUEST_SUFFIX}").write_text(data, encoding="utf-8")
    if name == "old":
        long_ago = time.time() - app_helpers.MAX_REQUEST_SECONDS - 5
        for entry in folder.iterdir():
            os.utime(entry, (long_ago, long_ago))
    blender_helper.check()
    assert blender_helper.imported == [] and requests_left("blender") == []


def test_a_failed_import_doesnt_stop_the_helper(blender_helper, tmp_path, capsys):
    def fail(*_a, **_k):
        raise RuntimeError("no good")

    blender_helper.bpy_stub.ops.wm.obj_import = fail
    app_helpers.send("blender", model(tmp_path))
    blender_helper.check()
    assert "no good" in capsys.readouterr().out and requests_left("blender") == []


def test_blender_helper_starts_with_a_window_only(blender_helper):
    blender_helper.bpy_stub.app.background = True
    blender_helper.register()
    assert blender_helper.registered == [] and not app_helpers.mailbox("blender").exists()
    blender_helper.bpy_stub.app.background = False
    blender_helper.register()
    blender_helper.register()  # (twice: one timer)
    assert len(blender_helper.registered) == 1
    assert f"{app_helpers.ALIVE_PREFIX}{os.getpid()}" in os.listdir(app_helpers.mailbox("blender"))
    assert blender_helper.registered[0]() == 1.0  # (keeps running)
    blender_helper.unregister()
    assert blender_helper.registered == []


@pytest.fixture
def maya_helper(tmp_path, monkeypatch):
    calls = []
    cmds = types.SimpleNamespace(
        about=lambda batch=False: cmds.batch, batch=False,
        loadPlugin=lambda name, quiet=False: calls.append(("plugin", name)),
        file=lambda path, **k: calls.append(("file", path, k)),
    )
    deferred = []
    utils = types.SimpleNamespace(executeDeferred=deferred.append)
    maya = types.ModuleType("maya")
    maya.cmds, maya.utils = cmds, utils
    monkeypatch.setitem(sys.modules, "maya", maya)
    monkeypatch.setitem(sys.modules, "maya.cmds", cmds)
    monkeypatch.setitem(sys.modules, "maya.utils", utils)
    module = load_helper("maya", tmp_path)
    module.calls, module.cmds_stub, module.deferred = calls, cmds, deferred
    return module


def test_maya_imports_whats_asked_for(maya_helper, tmp_path):
    fbx = model(tmp_path, "a.fbx")
    app_helpers.send("maya", fbx)
    maya_helper.check()
    assert maya_helper.calls == [("plugin", "fbxmaya"), ("file", str(fbx), {"i": True, "type": "FBX", "ignoreVersion": True})]
    assert requests_left("maya") == []


def test_maya_doesnt_import_scenes_or_other_files(maya_helper, tmp_path):
    for name in ("a.mb", "a.ma", "a.py"):
        app_helpers.send("maya", model(tmp_path, name))
    maya_helper.check()
    assert maya_helper.calls == [] and requests_left("maya") == []


def test_maya_helper_skips_batch_jobs_and_acts_on_mayas_own_thread(maya_helper, tmp_path, monkeypatch):
    maya_helper.cmds_stub.batch = True
    maya_helper.start()
    assert not app_helpers.mailbox("maya").exists()

    maya_helper.cmds_stub.batch = False
    started = []
    monkeypatch.setattr(maya_helper.threading, "Thread", lambda **k: types.SimpleNamespace(start=lambda: started.append(k)))
    maya_helper.start()
    assert started[0]["daemon"] is True
    assert f"{app_helpers.ALIVE_PREFIX}{os.getpid()}" in os.listdir(app_helpers.mailbox("maya"))

    # The thread only looks; once there's a request, one job is queued for Maya until that's run
    app_helpers.send("maya", model(tmp_path))
    ticks = []

    def sleep(_seconds):
        ticks.append(True)
        if len(ticks) > 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(maya_helper.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        maya_helper._watch()
    assert len(maya_helper.deferred) == 1 and maya_helper.calls == []
    maya_helper.deferred[0]()
    assert [c[0] for c in maya_helper.calls] == ["plugin", "file"]


# ----------------------------------------------------------------------
# Is there an open window, and what goes to it
# ----------------------------------------------------------------------
def announce(app_id: str, pid: int) -> None:
    folder = app_helpers.mailbox(app_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{app_helpers.ALIVE_PREFIX}{pid}").touch()


def test_a_window_counts_only_if_its_process_is_that_app(monkeypatch):
    assert not app_helpers.window_is_open("maya")  # (no folder yet)
    announce("maya", 111)
    announce("maya", 222)
    programs = {111: None, 222: r"C:\Windows\notepad.exe"}  # (gone; its id reused by something else)
    monkeypatch.setattr(app_helpers, "_process_program", programs.get)
    assert not app_helpers.window_is_open("maya")
    assert os.listdir(app_helpers.mailbox("maya")) == []  # (what they left is cleared up)

    announce("maya", 333)
    programs[333] = r"C:\Program Files\Autodesk\Maya2027\bin\Maya.exe"
    assert app_helpers.window_is_open("maya")
    assert not app_helpers.window_is_open("blender") and not app_helpers.window_is_open("zbrush")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' process list")
def test_reading_a_real_process():
    assert os.path.basename(app_helpers._process_program(os.getpid())).lower().startswith("python")
    assert app_helpers._process_program(2 ** 31 - 2) is None


@pytest.fixture
def started(tmp_path, monkeypatch):
    """Maya and Blender are on this computer; what's been started instead of sent."""
    calls = []
    apps.configure({
        "maya": [fake_program(tmp_path / "Maya2027" / "bin", "maya.exe")],
        "blender": [fake_program(tmp_path / "Blender 4.0", "blender-launcher.exe")],
        "zbrush": [fake_program(tmp_path / "ZBrush", "ZBrush.exe")],
    }, {})
    monkeypatch.setattr(subprocess, "Popen", lambda args, **k: calls.append(args))
    return calls


def test_a_model_goes_to_the_open_window(started, monkeypatch):
    monkeypatch.setattr(app_helpers, "window_is_open", lambda app_id: app_id == "maya")
    assert apps.launch(app_named("maya"), "D:/a.FBX") is True
    assert started == [] and len(requests_left("maya")) == 1


@pytest.mark.parametrize("app_id, name", [
    ("maya", "scene.mb"),  # (a scene would replace what's in the window)
    ("maya", "scene.ma"),
    ("blender", "scene.blend"),
    ("zbrush", "tool.ztl"),  # (it has no helper)
])
def test_scenes_and_apps_without_a_helper_still_start_a_new_window(started, monkeypatch, app_id, name):
    monkeypatch.setattr(app_helpers, "window_is_open", lambda _app_id: True)
    assert apps.launch(app_named(app_id), f"D:/{name}") is False
    assert len(started) == 1


def test_with_no_open_window_a_new_one_starts(started):
    assert apps.launch(app_named("blender"), "D:/a.obj") is False
    assert len(started) == 1 and "wm.obj_import" in started[0][2]


def test_if_a_request_cant_be_written_a_new_window_starts(started, monkeypatch):
    monkeypatch.setattr(app_helpers, "window_is_open", lambda _app_id: True)

    def refuse(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(app_helpers, "send", refuse)
    assert apps.launch(app_named("maya"), "D:/a.obj") is False and len(started) == 1


def test_an_old_request_nobody_took_is_cleared_when_the_next_is_sent(tmp_path):
    app_helpers.send("maya", tmp_path / "old.obj")
    long_ago = time.time() - app_helpers.MAX_REQUEST_SECONDS - 5
    first = app_helpers.mailbox("maya") / requests_left("maya")[0]
    os.utime(first, (long_ago, long_ago))
    app_helpers.send("maya", tmp_path / "new.obj")
    assert len(requests_left("maya")) == 1 and not first.exists()


# ----------------------------------------------------------------------
# Adding and removing them
# ----------------------------------------------------------------------
def maya_scripts() -> Path:
    return Path(os.environ["MAYA_APP_DIR"]) / "scripts"


def test_maya_helper_is_added_beside_whats_there_and_taken_out_again():
    user_setup = maya_scripts() / "userSetup.py"
    user_setup.parent.mkdir(parents=True)
    mine = "import my_tools\nmy_tools.start()"  # (no newline at its end)
    user_setup.write_text(mine, encoding="utf-8")
    assert not app_helpers.is_installed("maya", None)

    app_helpers.install("maya", None)
    app_helpers.install("maya", None)  # (twice: one block)
    text = user_setup.read_text(encoding="utf-8")
    assert app_helpers.is_installed("maya", None) and text.startswith(mine + "\n")
    assert text.count("asset_manager_helper.start()") == 1
    compile(text, "userSetup", "exec")
    assert (maya_scripts() / app_helpers.HELPER_FILE).is_file()

    app_helpers.remove("maya", None)
    assert user_setup.read_text(encoding="utf-8") == mine + "\n"  # (only our lines are gone)
    assert not app_helpers.is_installed("maya", None)


def test_a_startup_script_that_held_only_our_lines_goes_too():
    app_helpers.install("maya", None)
    assert (maya_scripts() / "userSetup.py").exists()
    app_helpers.remove("maya", None)
    assert not (maya_scripts() / "userSetup.py").exists()
    app_helpers.remove("maya", None)  # (nothing to remove: fine)


def test_a_startup_script_that_cant_be_read_is_left_alone():
    user_setup = maya_scripts() / "userSetup.py"
    user_setup.parent.mkdir(parents=True)
    user_setup.write_bytes(b"# caf\xe9\n")  # (not UTF-8)
    with pytest.raises(app_helpers.HelperError):
        app_helpers.install("maya", None)
    assert user_setup.read_bytes() == b"# caf\xe9\n"


def test_blender_helper_goes_in_the_startup_folder_of_its_version():
    program = r"C:\Program Files\Blender Foundation\Blender 4.0\blender-launcher.exe"
    app_helpers.install("blender", program)
    expected = Path(os.environ["APPDATA"]) / "Blender Foundation" / "Blender" / "4.0" / "scripts" / "startup" / app_helpers.HELPER_FILE
    assert expected.is_file() and app_helpers.is_installed("blender", program)
    assert not app_helpers.is_installed("blender", r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe")
    app_helpers.remove("blender", program)
    assert not expected.exists()


def test_a_blender_of_unknown_version_says_so(monkeypatch, tmp_path):
    with pytest.raises(app_helpers.HelperError, match="version"):
        app_helpers.install("blender", r"D:\Apps\Blender\blender.exe")
    assert not app_helpers.is_installed("blender", r"D:\Apps\Blender\blender.exe")
    monkeypatch.setenv("BLENDER_USER_SCRIPTS", str(tmp_path / "scripts"))
    app_helpers.install("blender", r"D:\Apps\Blender\blender.exe")
    assert (tmp_path / "scripts" / "startup" / app_helpers.HELPER_FILE).is_file()


def test_settings_adds_and_removes_a_helper(qapp, tmp_path):
    from ui.settings_dialog import AppsPage

    exe = fake_program(tmp_path / "Blender 4.0", "blender-launcher.exe")
    page = AppsPage({"blender": [exe]}, {})
    status, button = page._helpers["blender"]
    assert button.text() == "Install Helper" and "Not installed" in status.text()
    assert not page._helpers["maya"][1].isEnabled()  # (Maya isn't on this computer)
    button.click()
    assert button.text() == "Remove Helper" and "next time you start Blender" in status.text()
    assert app_helpers.is_installed("blender", exe)
    button.click()
    assert button.text() == "Install Helper" and not app_helpers.is_installed("blender", exe)
    page.deleteLater()


# ----------------------------------------------------------------------
# Apps added by hand
# ----------------------------------------------------------------------
def krita(tmp_path, types_=(".kra", ".png")) -> dict:
    return {"id": "custom-1", "name": "Krita", "program": fake_program(tmp_path / "Krita", "krita.exe"), "types": list(types_)}


def test_an_added_app_opens_its_types_like_any_other(tmp_path, monkeypatch):
    entry = krita(tmp_path)
    apps.configure({"photoshop": [fake_program(tmp_path / "ps", "Photoshop.exe")]}, {}, [entry])
    assert [a.name for a in apps.apps_for(".png")] == ["Photoshop", "Krita"]
    assert [a.name for a in apps.apps_for(".KRA")] == ["Krita"]
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **k: started.append((args, k)))
    assert apps.launch(apps.apps_for(".kra")[0], r"D:\my files\it's a & b.kra") is False
    assert started[0][0] == [entry["program"], r"D:\my files\it's a & b.kra"] and not started[0][1].get("shell")


def test_an_added_app_thats_gone_isnt_offered(tmp_path):
    entry = krita(tmp_path)
    os.remove(entry["program"])
    apps.configure({}, {}, [entry])
    assert apps.apps_for(".kra") == []


def test_setting_an_apps_file_types_by_hand(tmp_path):
    apps.configure({"zbrush": [fake_program(tmp_path / "z", "ZBrush.exe")]}, {}, [], {"zbrush": [".ztl", ".psd"]})
    assert [a.id for a in apps.apps_for(".psd")] == ["zbrush"]
    assert apps.apps_for(".obj") == []  # (no longer among them)
    assert apps.icon_program(".ztl") is not None  # (its cards keep its icon)


def test_a_new_type_shows_its_apps_icon_and_a_viewer_page(library_with_kra, qapp, monkeypatch, no_starting):
    from ui.viewer import AssetViewerDialog

    set_custom_apps([krita(library_with_kra.parent)])
    icon = QImage(64, 64, QImage.Format.Format_ARGB32_Premultiplied)
    icon.fill(QColor(255, 0, 0))
    monkeypatch.setattr(app_icons, "read_icon", lambda program: icon)
    window = open_window(qapp)
    asset = asset_named(window, "painting.kra")
    assert asset.asset_group == "Other" and asset.opens_in_app
    manager = ThumbnailManager()
    assert color_share(card_of(qapp, manager, asset), is_red) > 0.2
    manager.shutdown()

    dialog = AssetViewerDialog(asset, window)
    assert viewer_buttons(dialog) == ["Open in Krita"]
    from PySide6.QtCore import Qt

    press(dialog, Qt.Key.Key_O)
    assert no_starting == [("custom-1", str(library_with_kra / "painting.kra"))]
    dialog.close()
    dialog.deleteLater()
    window.close()


def test_a_type_with_a_viewer_keeps_its_picture_and_cards(tmp_path):
    entry = krita(tmp_path, [".png"])
    apps.configure({}, {}, [entry])
    assert apps.icon_program(".png") is None


def test_the_menu_offers_an_added_app_and_says_when_a_model_goes_to_an_open_window(library_with_kra, qapp, monkeypatch):
    from ui.asset_menu import AssetMenuActions

    set_custom_apps([krita(library_with_kra.parent)])
    window = open_window(qapp)
    apps.configure({"maya": [fake_program(library_with_kra.parent / "m", "maya.exe")]}, {}, get_custom_apps())
    noop = lambda *a: None  # noqa: E731
    actions = AssetMenuActions(noop, noop, noop, noop, noop, open_in=noop)
    menu = menu_of(window, asset_named(window, "painting.kra"), actions)
    assert open_in_entries(menu) == ["Krita"]
    menu.deleteLater()

    monkeypatch.setattr(app_helpers, "window_is_open", lambda app_id: app_id == "maya")
    menu = menu_of(window, asset_named(window, "model.obj"), actions)
    open_in = next(a for a in menu.actions() if a.text() == "Open In").menu()
    assert open_in.actions()[0].toolTip() == "Imports it into the Maya that's open"
    menu.deleteLater()
    window.close()


@pytest.fixture
def library_with_kra(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    (root / "painting.kra").write_bytes(b"PK")
    model(root, "model.obj")
    set_scan_folders([root])
    settings_extensions = [*get_file_extensions(), ".kra"]
    from core.settings import set_file_extensions

    set_file_extensions(settings_extensions)
    return root


@pytest.fixture
def no_starting(monkeypatch):
    started = []
    monkeypatch.setattr(apps, "launch", lambda app, file: started.append((app.id, str(file))))
    return started


def test_adding_an_app_in_settings(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QInputDialog

    from ui.settings_dialog import SettingsDialog

    exe = fake_program(tmp_path / "Krita", "krita-app.exe")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (exe, "")))
    answers = iter([("Krita Studio", True), (".kra, .PNG", True)])
    asked = []

    def answer(_parent, _title, label, text=""):
        asked.append((label, text))
        return next(answers)

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(answer))
    dialog = SettingsDialog()
    assert ".kra" not in get_file_extensions()
    dialog.page_apps._on_add()
    assert asked[0][1] == "Krita App"  # (a name suggested from the program's)
    dialog._on_save()
    saved = get_custom_apps()
    assert [(c["name"], c["program"], c["types"]) for c in saved] == [("Krita Studio", str(Path(exe)), [".kra", ".png"])]
    assert ".kra" in get_file_extensions() and dialog.extensions_changed and dialog.apps_changed  # (the library picks them up)
    assert [a.name for a in apps.apps_for(".kra")] == ["Krita Studio"]
    dialog.deleteLater()

    # Shown next time, and can be taken out (its file type stays in the library's list)
    dialog = SettingsDialog()
    page = dialog.page_apps
    assert len(page.custom()) == 1 and len(page._custom_rows) == 5
    page._on_remove_custom(page.custom()[0])
    dialog._on_save()
    assert get_custom_apps() == [] and apps.apps_for(".kra") == []
    assert ".kra" in get_file_extensions()
    dialog.deleteLater()


@pytest.mark.parametrize("answers", [
    [("", False)],  # (cancelled at the name)
    [("   ", True)],  # (no name)
    [("Krita", True), ("", False)],  # (cancelled at the types)
    [("Krita", True), ("", True)],  # (no types)
    [("Krita", True), ("kra", True)],  # (not a file type: it says so)
])
def test_adding_an_app_can_be_cancelled_or_refused(qapp, tmp_path, monkeypatch, answers):
    from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

    from ui.settings_dialog import AppsPage

    exe = fake_program(tmp_path, "krita.exe")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (exe, "")))
    replies = iter(answers)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: next(replies)))
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a: warned.append(a[2])))
    page = AppsPage({}, {})
    page._on_add()
    assert page.custom() == []
    assert bool(warned) == (answers[-1][0] == "kra")
    page.deleteLater()


def test_editing_a_built_in_apps_file_types(qapp, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    from ui.settings_dialog import SettingsDialog

    replies = iter([(".ztl, .tga", True), ("", True)])
    shown = []

    def answer(_parent, _title, _label, text=""):
        shown.append(text)
        return next(replies)

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(answer))
    dialog = SettingsDialog()
    zbrush = app_named("zbrush")
    dialog.page_apps._on_edit_types(zbrush)
    assert ".ztl" in shown[0]  # (starts from what it has)
    dialog._on_save()
    assert get_app_types() == {"zbrush": [".ztl", ".tga"]}
    dialog.deleteLater()

    # Left empty: back to its usual types
    dialog = SettingsDialog()
    dialog.page_apps._on_edit_types(zbrush)
    dialog._on_save()
    assert get_app_types() == {}
    dialog.deleteLater()


def test_a_damaged_list_of_added_apps_is_ignored():
    import json

    from core import settings

    settings.CONFIG_FILE_PATH.write_text(json.dumps({
        "custom_apps": [{"id": "a"}, "nonsense", {"id": "b", "name": "B", "program": "b.exe", "types": [".b", 3]}],
        "app_types": {"zbrush": "nope", "maya": [".ma", 1]},
    }), encoding="utf-8")
    assert get_custom_apps() == [{"id": "b", "name": "B", "program": "b.exe", "types": [".b"]}]
    assert get_app_types() == {"maya": [".ma"]}
