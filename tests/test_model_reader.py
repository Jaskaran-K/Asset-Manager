"""3D models read in a helper program of its own (core.model_reader): the real helper is started here (in
other tests models are read in the test's own process, see conftest)."""

from __future__ import annotations

import ctypes
import time
from pathlib import Path

import numpy as np
import pytest

from core import crash_log, metadata, model_reader
from core.obj_fast import LoadCancelled
from tests.test_model_viewer import model_asset, open_viewer
from tests.test_thumbnail_cache import CUBE_OBJ


def pump(qapp, condition, seconds=20.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


@pytest.fixture
def helper(monkeypatch):
    """The real helper program (started on first use)."""
    monkeypatch.setenv(model_reader.READER_ENV, "testing")
    monkeypatch.setattr(model_reader, "_unavailable", False)
    monkeypatch.setattr(model_reader, "_readers", {})
    yield
    model_reader.close_all()


@pytest.fixture
def cube(tmp_path) -> Path:
    path = tmp_path / "crate.obj"
    path.write_text(CUBE_OBJ, encoding="utf-8")
    return path


def test_the_helper_reads_pictures_details_meshes_and_uvs(helper, cube):
    picture = model_reader.read_picture(cube, 110)
    assert picture is not None and not picture.isNull() and picture.width() > 10
    assert model_reader.read_faces(cube) == 12
    mesh = model_reader.read_mesh(cube)
    assert mesh.face_count == 12 and isinstance(mesh.vertices, np.ndarray)
    model_reader.read_uvs(cube)  # (the cube has none: only that the request goes through)
    first = model_reader.reader("library").request({"op": "ping", "path": ""}, 10)["pong"]
    assert model_reader.reader("library").request({"op": "ping", "path": ""}, 10)["pong"] == first  # (kept running)
    viewer_pid = model_reader.reader("viewer").request({"op": "ping", "path": ""}, 10)["pong"]
    assert viewer_pid != first  # (the viewer has its own: it never waits behind thumbnails)
    assert metadata.read_details(str(cube), ".obj").poly_count == 12


def test_the_helpers_are_capped_and_end_with_the_app(helper, cube):
    model_reader.read_faces(cube)
    process = model_reader.reader("details")._process
    in_job = ctypes.c_int()
    ctypes.windll.kernel32.IsProcessInJob(int(process._handle), None, ctypes.byref(in_job))
    assert in_job.value and model_reader.memory_limit() > 1024 ** 3


def test_a_crash_or_a_hang_ends_only_the_helper(helper):
    library = model_reader.reader("library")
    with pytest.raises(model_reader.ReadFailed, match="crashed the model reader"):
        library.request({"op": "crash", "path": ""}, 10)
    assert library.request({"op": "ping", "path": ""}, 10)["pong"]  # (a new one, for the next file)
    with pytest.raises(model_reader.ReadFailed, match="took over"):
        library.request({"op": "sleep", "path": "", "seconds": 30}, 0.5)
    started = time.monotonic()
    with pytest.raises(LoadCancelled):
        library.request({"op": "sleep", "path": "", "seconds": 30}, 60, should_stop=lambda: time.monotonic() - started > 0.3)
    assert time.monotonic() - started < 5
    assert library.request({"op": "ping", "path": ""}, 10)["pong"]


def test_a_model_that_failed_is_not_read_again_until_it_changes(helper, cube, monkeypatch):
    calls = []

    def crashing(name, request, should_stop=None):
        calls.append(request["op"])
        raise model_reader.ReadFailed("reading it crashed the model reader")

    monkeypatch.setattr(model_reader, "_ask", crashing)
    for _ in range(2):
        with pytest.raises(model_reader.ReadFailed):
            model_reader.read_picture(cube, 110)
    assert calls == ["picture"]  # (the second time: remembered)
    assert "crashed" in model_reader._failures_path().read_text(encoding="utf-8")
    monkeypatch.setattr(model_reader, "_failures", None)  # (as at the next start: read from the file)
    with pytest.raises(model_reader.ReadFailed):
        model_reader.read_faces(cube)
    assert calls == ["picture"]
    cube.write_text(CUBE_OBJ + "\n# changed\n", encoding="utf-8")
    with pytest.raises(model_reader.ReadFailed):
        model_reader.read_faces(cube)
    assert calls == ["picture", "faces"]  # (changed: read again)


def test_a_thumbnail_says_when_its_model_couldnt_be_read(qapp, cube, monkeypatch):
    from core import thumbnail_manager
    from core.scanner import AssetItem

    def failed(path, size):
        raise model_reader.ReadFailed("reading it crashed the model reader")

    monkeypatch.setattr(model_reader, "read_picture", failed)
    asset = AssetItem(cube, cube.name, ".obj", cube.stat().st_size, cube.stat().st_mtime, "3D Models", "OBJ")
    job = thumbnail_manager._ThumbnailRunnable(None, asset, 110, None, {}, render_model=True)
    assert job._render() == (None, "Couldn't read")


def test_without_a_helper_models_are_read_in_the_app(helper, cube, monkeypatch):
    logged = []
    monkeypatch.setattr(model_reader, "helper_command", lambda address: [str(cube.parent / "no-such-program.exe")])
    monkeypatch.setattr(crash_log, "log_error", lambda title, details="": logged.append(title))
    assert model_reader.read_faces(cube) == 12
    assert model_reader.in_process() and len(logged) == 1


def test_a_big_mesh_comes_back_through_files(tmp_path, monkeypatch):
    from core.model_loader import Mesh3D

    monkeypatch.setattr(model_reader, "SPILL_BYTES", 10)
    mesh = Mesh3D("big", np.ones((100, 3), np.float32), np.zeros((50, 3), np.uint32), np.ones((100, 3), np.float32))
    sent = model_reader._spill_mesh(mesh, tmp_path / "spill")
    assert isinstance(sent.vertices, str) and len(list((tmp_path / "spill").iterdir())) == 3
    taken = model_reader._take_mesh(sent)
    assert taken.vertices.shape == (100, 3) and taken.faces.dtype == np.uint32
    assert not (tmp_path / "spill").exists()


def test_the_viewer_opens_a_model_through_the_helper(qapp, tmp_path, helper):
    dialog = open_viewer(qapp, model_asset(tmp_path))
    try:
        assert pump(qapp, lambda: dialog.viewport.face_count > 0)
        assert model_reader.reader("viewer")._process is not None
        assert not any(model_reader.jobs_folder().glob("*")) if model_reader.jobs_folder().exists() else True
    finally:
        dialog.close()
        dialog.deleteLater()
