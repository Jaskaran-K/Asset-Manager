"""The Render button: the job (core.render_scene), the download (core.render_engine), the worker process
(core.render_worker, with a stand-in for LuxCore) and the window (ui.render_window, with a stand-in worker)."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import sys
import time
import types
import zipfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest

from core import environment, render_engine, render_scene, render_worker
from core.archives import Cancelled
from core.model_look import LIGHTINGS, MATERIALS, light_directions
from core.render_scene import RenderJob
from core.scanner import AssetItem
from core.settings import get_render_settings, set_model_view, set_render_settings
from tests.test_model_viewer import close, model_asset, open_viewer, pump
from tests.test_thumbnail_cache import CUBE_OBJ


# ----------------------------------------------------------------------
# The job
# ----------------------------------------------------------------------
def test_the_sizes_on_offer():
    assert render_scene.RESOLUTIONS == {"720p": (1280, 720), "1080p": (1920, 1080), "1440p": (2560, 1440)}
    assert {key: seconds for key, (_text, seconds) in render_scene.QUALITIES.items()} == {"draft": 5, "good": 20, "best": 60}


def test_the_camera_is_the_viewers():
    eye, target, up = render_scene.camera(0.0, 0.0, 0.0, 0.0, 1.0)
    assert eye == pytest.approx([0, 0, 3.2]) and target == pytest.approx([0, 0, 0]) and up == pytest.approx([0, 1, 0])
    eye, target, _up = render_scene.camera(0.0, 0.0, 0.0, 0.0, 2.0)  # (zoom: closer)
    assert eye == pytest.approx([0, 0, 1.6])
    eye, target, _up = render_scene.camera(0.0, 0.0, 0.5, 0.25, 1.0)  # (a pan to the right moves the model right: so the camera left)
    assert eye == pytest.approx([-0.5, -0.25, 3.2]) and target == pytest.approx([-0.5, -0.25, 0])
    eye, _target, _up = render_scene.camera(0.0, 90.0, 0.0, 0.0, 1.0)  # (orbited: seen from the side)
    assert eye == pytest.approx([-3.2, 0, 0], abs=1e-9)
    eye, _target, up = render_scene.camera(30.0, 0.0, 0.0, 0.0, 1.0)  # (tilted: looking down from above)
    assert eye[1] > 0 and up[2] < 0


@pytest.mark.parametrize("width, height, angle", [(800, 800, 45.0), (1920, 1080, 72.73), (2560, 1440, 72.73)])
def test_a_wide_picture_sees_what_the_viewer_sees_top_to_bottom(width, height, angle):
    """LuxCore's angle runs along the long side: at 45 degrees, a wide render cut off the head and feet."""
    assert render_scene.lens_angle(width, height) == pytest.approx(angle, abs=0.1)
    half_height = math.tan(math.radians(render_scene.lens_angle(width, height) / 2)) * height / max(width, height)
    assert math.degrees(2 * math.atan(half_height)) == pytest.approx(45.0, abs=0.01)


def test_the_sky_turns_with_the_viewer():
    m = np.array(render_scene.sky_matrix(0.0)).reshape(4, 4).T[:3, :3]
    assert m @ np.array([0, 0, 1]) == pytest.approx([0, 1, 0])  # (LuxCore's up is the panorama's)
    turned = np.array(render_scene.sky_matrix(90.0)).reshape(4, 4).T[:3, :3]
    assert turned @ np.array([1, 0, 0]) == pytest.approx([1, 0, 0], abs=1e-9)  # (what looked along z now looks along x)


def test_materials():
    color = (0.5, 0.5, 0.5)
    assert render_scene.surface("matte", color, 0.5)[0]["type"] == "matte"
    assert render_scene.surface("clay", color, 0.5)[0]["type"] == "roughmatte"
    assert render_scene.surface("phong", color, 0.5)[0]["type"] == "glossy2"
    glossy, phong = render_scene.surface("glossy", color, 0.5)[0], render_scene.surface("phong", color, 0.5)[0]
    assert float(glossy["uroughness"]) < float(phong["uroughness"])
    metal, textures = render_scene.surface("metallic", (1.0, 0.5, 0.0), 0.5)
    assert metal["type"] == "metal2" and float(metal["uroughness"]) == 0.25
    assert textures["metal_colour.kr"].split()[0] == "1.00000" and float(textures["metal_colour.kr"].split()[2]) == 0  # (its colour)
    assert float(render_scene.surface("metallic", color, 0.0)[0]["uroughness"]) < 0.01  # (the slider's mirror)
    assert float(render_scene.surface("metallic", color, 1.0)[0]["uroughness"]) == 1.0


def test_colours_are_decoded_as_the_viewer_decodes_them():
    assert render_scene.linear((0.5, 0.0, 1.0)) == pytest.approx([0.5 ** 2.2, 0.0, 1.0])


def job(**changes):
    values = dict(model="m.obj", width=1920, height=1080, seconds=5.0, denoise=True, eye=[0, 0, 3], target=[0, 0, 0], up=[0, 1, 0])
    values.update(changes)
    return RenderJob(**values)


def test_a_job_survives_its_file(tmp_path):
    original = job(hdri=r"D:\skies\noon.hdr", sky_matrix=render_scene.sky_matrix(30.0), exposure=0.4, backdrop=True, material={"type": "matte"})
    original.save(tmp_path)
    assert RenderJob.load(tmp_path) == original


def test_an_hdri_in_the_scene():
    props = render_scene.scene_properties(job(hdri="noon.hdr", sky_matrix=render_scene.sky_matrix(0.0)), "mesh.ply")
    assert props["scene.lights.sky.type"] == "infinite" and props["scene.lights.sky.file"] == "noon.hdr"
    assert props["scene.lights.sky.gamma"] == 1.0  # (the default of 2.2 blew a model out to white)
    assert props["scene.shapes.model.ply"] == "mesh.ply" and not any(".key0" in name for name in props)


def test_a_presets_lights_in_the_scene():
    lighting = LIGHTINGS["studio"]
    lights, ambient = render_scene.preset_lights(lighting, 0.0, 0.0, 0.0, 0.0)
    assert len(lights) == len(lighting.lights) and ambient == pytest.approx(render_scene.linear(tuple((s + g) / 2 for s, g in zip(lighting.sky, lighting.ground))))
    assert lights[0]["direction"] == pytest.approx(light_directions(lighting)[0].tolist())
    props = render_scene.scene_properties(job(lights=lights, ambient=ambient), "mesh.ply")
    assert props["scene.lights.key0.type"] == "sharpdistant" and props["scene.lights.soft.type"] == "constantinfinite"
    # (LuxCore's direction is the way the light goes: away from where it's coming from)
    assert props["scene.lights.key0.direction"] == pytest.approx((-light_directions(lighting)[0]).tolist())


def test_a_presets_lights_stay_with_the_camera():
    """In the viewer they turn with the view: orbiting leaves them where they were on screen."""
    lighting = LIGHTINGS["top"]
    still, _soft = render_scene.preset_lights(lighting, 0.0, 0.0, 0.0, 0.0)
    orbited, _soft = render_scene.preset_lights(LIGHTINGS["headlight"], 0.0, 90.0, 0.0, 0.0)
    assert still[0]["direction"][1] > 0.9
    eye, _target, _up = render_scene.camera(0.0, 90.0, 0.0, 0.0, 1.0)
    assert np.dot(orbited[0]["direction"], np.array(eye) / np.linalg.norm(eye)) > 0.99  # (the headlight's still at the camera)


def test_metal_has_something_to_reflect_under_a_preset():
    dark = LIGHTINGS["dramatic"]
    _lights, plain = render_scene.preset_lights(dark, 0.0, 0.0, 0.0, 0.0)
    _lights, metal = render_scene.preset_lights(dark, 0.0, 0.0, 0.0, 0.0, metal=True)
    assert min(metal) > max(plain) * 5


def test_the_film():
    props = render_scene.film_properties(job(backdrop=False), "PATHOCL")
    assert props["path.forceblackbackground.enable"] == 1 and props["film.width"] == 1920 and props["renderengine.type"] == "PATHOCL"
    assert props["film.imagepipelines.1.0.type"] == "INTEL_OIDN"  # (only the second version is denoised: not the live preview)
    assert not any("OIDN" in str(value) for name, value in props.items() if name.startswith("film.imagepipelines.0"))
    assert render_scene.film_properties(job(backdrop=True), "PATHCPU")["path.forceblackbackground.enable"] == 0


# ----------------------------------------------------------------------
# The download
# ----------------------------------------------------------------------
def wheel_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buffer.getvalue()


FAKE_WHEELS = {
    "lux.whl": wheel_bytes({
        "pyluxcore/__init__.py": b"# lux", "pyluxcore.libs/a.dll": b"dll", "bin/luxcoreui.exe": b"left out",
        "pyluxcoretest/t.py": b"left out", "pyluxcoretools/t.py": b"left out",
    }),
    "nvidia.whl": wheel_bytes({"nvidia/cuda_nvrtc/bin/nvrtc64.dll": b"kept: its own bin"}),
}


class FakeReply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def serve(monkeypatch, wheels=None, hashes=None):
    """Downloads come from here, not the internet."""
    wheels = wheels or FAKE_WHEELS
    hashes = hashes or {}
    monkeypatch.setattr(render_engine, "DOWNLOADS", tuple(
        render_engine.Download(name, f"https://example.invalid/{name}", len(data), hashes.get(name, hashlib.sha256(data).hexdigest()))
        for name, data in wheels.items()
    ))
    monkeypatch.setattr(render_engine, "DOWNLOAD_BYTES", sum(len(d) for d in wheels.values()))
    monkeypatch.setattr(render_engine, "supported", lambda: True)
    asked = []

    def urlopen(request, timeout=None):
        asked.append(request.full_url)
        return FakeReply(wheels[request.full_url.rsplit("/", 1)[1]])

    monkeypatch.setattr(render_engine.urllib.request, "urlopen", urlopen)
    return asked


def test_the_pinned_downloads_are_for_this_python():
    """A new Python needs new LuxCore files: until then, the Render button's main renderer wouldn't load."""
    assert sys.version_info[:2] == render_engine.PYTHON
    tag = f"cp{render_engine.PYTHON[0]}{render_engine.PYTHON[1]}"
    assert tag in render_engine.DOWNLOADS[0].name and "win_amd64" in render_engine.DOWNLOADS[0].name
    for item in render_engine.DOWNLOADS:
        assert item.url.startswith("https://files.pythonhosted.org/") and item.url.endswith(item.name)
        assert len(item.sha256) == 64 and item.size > 0


def test_installing(monkeypatch):
    serve(monkeypatch)
    assert not render_engine.is_installed()
    seen = []
    render_engine.install(lambda done, total: seen.append((done, total)))
    assert render_engine.is_installed()
    folder = render_engine.engine_folder()
    assert (folder / "pyluxcore" / "__init__.py").read_bytes() == b"# lux" and (folder / "pyluxcore.libs" / "a.dll").is_file()
    assert (folder / "nvidia" / "cuda_nvrtc" / "bin" / "nvrtc64.dll").is_file()  # (NVIDIA's bin, which LuxCore needs)
    assert not (folder / "bin").exists() and not (folder / "pyluxcoretest").exists() and not (folder / "pyluxcoretools").exists()
    total = render_engine.DOWNLOAD_BYTES
    assert seen[-1] == (total, total) and all(b >= a for (a, _t), (b, _u) in zip(seen, seen[1:]))
    assert not (render_engine._root() / "unpacking").exists()
    assert render_engine.installed_bytes() > 0


def test_installing_twice_downloads_once(monkeypatch):
    asked = serve(monkeypatch)
    render_engine.install()
    count = len(asked)
    render_engine.install()
    assert len(asked) == count


def test_a_file_that_doesnt_match_its_hash_is_never_used(monkeypatch):
    serve(monkeypatch, hashes={"nvidia.whl": "0" * 64})
    with pytest.raises(render_engine.EngineError, match="didn't download correctly"):
        render_engine.install()
    assert not render_engine.is_installed() and not render_engine.engine_folder().exists()
    assert not (render_engine._root() / "unpacking").exists()


def test_a_file_that_would_unpack_elsewhere_is_refused(monkeypatch):
    serve(monkeypatch, wheels={"evil.whl": wheel_bytes({"../../outside.dll": b"no"})})
    with pytest.raises(render_engine.EngineError, match="shouldn't"):
        render_engine.install()
    assert not render_engine.is_installed()
    assert not (render_engine._root().parent / "outside.dll").exists()


def test_a_failed_download_says_so(monkeypatch):
    serve(monkeypatch)

    def offline(_request, timeout=None):
        raise render_engine.urllib.error.URLError("no connection")

    monkeypatch.setattr(render_engine.urllib.request, "urlopen", offline)
    with pytest.raises(render_engine.EngineError, match="no connection"):
        render_engine.install()
    assert not render_engine.is_installed()


def test_a_download_can_be_stopped(monkeypatch):
    serve(monkeypatch)
    with pytest.raises(Cancelled):
        render_engine.install(should_stop=lambda: True)
    assert not render_engine.is_installed() and not (render_engine._root() / "unpacking").exists()


def test_removing(monkeypatch):
    serve(monkeypatch)
    render_engine.install()
    render_engine.remove()
    assert not render_engine.is_installed() and not render_engine._root().exists()


def test_the_engines_path_is_short_enough_for_windows():
    """Its DLLs have names of up to 61 characters, and Windows won't load one past 260: a long folder name failed to load."""
    assert len(render_engine.engine_folder().name) <= 12 and render_engine.engine_folder().parent.name == "render"


# ----------------------------------------------------------------------
# The worker, with a stand-in for LuxCore
# ----------------------------------------------------------------------
def test_tone_mapping_is_the_viewers():
    assert render_worker.tone_map(np.zeros(3))[0] == 0
    assert render_worker.tone_map(np.full(3, 1000.0))[0] == pytest.approx(1.0)
    x = 0.5
    expected = ((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14)) ** (1 / 2.2)
    assert render_worker.tone_map(np.full(3, x))[0] == pytest.approx(expected)
    assert render_worker.tone_map(np.full(3, -1.0))[0] == 0


def test_the_mesh_file_reads_back(tmp_path):
    import trimesh

    points = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float32)
    faces = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.uint32)
    normals = np.array([[0, 0, 1]] * 4, dtype=np.float32)
    path = tmp_path / "mesh.ply"
    render_worker.write_ply(path, points, faces, normals)
    mesh = trimesh.load(str(path), process=False)
    assert np.array_equal(np.asarray(mesh.vertices, dtype=np.float32), points) and np.array_equal(mesh.faces, faces)
    assert np.allclose(mesh.vertex_normals, normals)


def test_a_file_held_open_by_the_window_doesnt_stop_a_render(tmp_path, monkeypatch):
    """On Windows, replacing a file fails while something's reading it: it did, and failed a whole render."""
    status = render_worker._Status(tmp_path)
    real, tries = os.replace, []

    def refusing(source, target):
        tries.append(1)
        if len(tries) < 3:
            raise PermissionError("in use")
        real(source, target)

    monkeypatch.setattr(render_worker.os, "replace", refusing)
    monkeypatch.setattr(render_worker.time, "sleep", lambda _s: None)
    status.update(phase="rendering")
    assert json.loads((tmp_path / render_worker.STATUS_FILE).read_text(encoding="utf-8"))["phase"] == "rendering" and len(tries) == 3

    def always(_source, _target):
        raise PermissionError("in use")

    monkeypatch.setattr(render_worker.os, "replace", always)
    status.update(phase="denoising")  # (it can't land: nothing's raised, and the next one will)


def fake_luxcore(*, passes=lambda polls: polls, gpu=True, device_error=None, log=None):
    """As much of pyluxcore as the worker uses: it renders a white model on the left half."""
    module = types.ModuleType("pyluxcore")
    log = log if log is not None else {}
    log.update(parsed={}, config={}, stopped=False, outputs=[])

    class Property:
        def __init__(self, name, value):
            self.name, self.value = name, value

    class Properties:
        def __init__(self):
            self.values = {}

        def Set(self, prop):
            self.values[prop.name] = prop.value
            return self

    class Value:
        def __init__(self, value):
            self.value = value

        def GetInt(self):
            return self.value

        def GetString(self):
            return self.value

    class Devices:
        def GetAllUniqueSubNames(self, _prefix):
            return ["opencl.device.0"]

        def Get(self, name):
            return Value("OPENCL_GPU" if gpu and name.endswith("type") else "OPENCL_CPU" if name.endswith("type") else "Test Card")

    def devices():
        if device_error is not None:
            raise RuntimeError(device_error)
        return Devices()

    class Scene:
        def Parse(self, props):
            log["parsed"] = props.values
            log["ply_exists"] = Path(props.values["scene.shapes.model.ply"][0]).is_file()

    class Film:
        def __init__(self, session):
            self.session = session

        def GetOutputFloat(self, kind, buffer, index=0):
            log["outputs"].append((kind, index))
            height, width = buffer.shape[:2]
            if kind == "RGB":
                buffer[:] = 0.0
                buffer[:height // 2] = 1000.0  # (LuxCore's rows run from the bottom: this is the picture's lower half)
            else:
                buffer[:] = 1.0

    class Stats:
        def __init__(self, count):
            self.count = count

        def Get(self, _name):
            return Value(self.count)

    class Session:
        def __init__(self, _config):
            self.polls = 0

        def Start(self):
            log["started"] = True

        def Stop(self):
            log["stopped"] = True

        def UpdateStats(self):
            self.polls += 1

        def GetStats(self):
            return Stats(passes(self.polls))

        def GetFilm(self):
            return Film(self)

    class Config:
        def __init__(self, props, _scene):
            log["config"] = props.values

    module.Init = lambda _log=None: log.update(initialised=True)
    module.Property, module.Properties, module.Scene = Property, Properties, Scene
    module.RenderConfig, module.RenderSession = Config, Session
    module.GetOpenCLDeviceDescs = devices
    module.FilmOutputType = types.SimpleNamespace(RGB_IMAGEPIPELINE="RGB", ALPHA="ALPHA")
    return module, log


def worker_folder(tmp_path, seconds=0.0, denoise=True, backdrop=False):
    model = tmp_path / "crate.obj"
    model.write_text(CUBE_OBJ, encoding="utf-8")
    folder = tmp_path / "job"
    folder.mkdir()
    RenderJob(str(model), 64, 36, seconds, denoise, [0, 0, 3], [0, 0, 0], [0, 1, 0], {"type": "matte", "kd": "0.5 0.5 0.5"}, backdrop=backdrop).save(folder)
    return folder


def state_of(folder):
    return json.loads((folder / render_worker.STATUS_FILE).read_text(encoding="utf-8"))


def install_stand_in(monkeypatch, module):
    monkeypatch.setitem(sys.modules, "pyluxcore", module)
    monkeypatch.setattr(render_engine, "is_installed", lambda: True)
    monkeypatch.setattr(render_engine, "add_to_path", lambda: None)
    monkeypatch.setattr(render_worker, "POLL_EVERY", 0.0)


def test_a_render_from_start_to_finish(tmp_path, monkeypatch):
    module, log = fake_luxcore(gpu=True)
    install_stand_in(monkeypatch, module)
    folder = worker_folder(tmp_path)
    assert render_worker.run(folder) == 0
    state = state_of(folder)
    assert state["phase"] == "done" and state["device"] == "Test Card"
    assert log["config"]["renderengine.type"] == ["PATHOCL"] and log["ply_exists"] and log["stopped"]
    assert ("RGB", 1) in log["outputs"]  # (the denoised version: the result)
    with Image.open(folder / render_worker.RESULT_FILE) as result:
        assert result.size == (64, 36)
        assert result.getpixel((30, 34))[0] > 240  # (LuxCore's first rows are the bottom of the picture)
        assert result.getpixel((30, 2))[0] < 40
    assert not (folder / "mesh.ply").exists()  # (hundreds of MB, for a sculpt)


def test_with_no_graphics_card_it_uses_the_processor(tmp_path, monkeypatch):
    module, log = fake_luxcore(gpu=False)
    install_stand_in(monkeypatch, module)
    folder = worker_folder(tmp_path)
    assert render_worker.run(folder) == 0
    assert log["config"]["renderengine.type"] == ["PATHCPU"] and "processor" in state_of(folder)["device"]


def test_without_denoising_it_isnt_denoised(tmp_path, monkeypatch):
    module, log = fake_luxcore()
    install_stand_in(monkeypatch, module)
    assert render_worker.run(worker_folder(tmp_path, denoise=False)) == 0
    assert ("RGB", 1) not in log["outputs"] and ("RGB", 0) in log["outputs"]


def test_without_a_backdrop_the_rest_is_the_viewers_background(tmp_path, monkeypatch):
    module, _log = fake_luxcore()

    def only_left(self_film_buffer):
        return self_film_buffer

    install_stand_in(monkeypatch, module)
    real = module.RenderSession.GetFilm

    def film_with_alpha(session):
        film = real(session)
        original = film.GetOutputFloat

        def output(kind, buffer, index=0):
            original(kind, buffer, index)
            if kind == "ALPHA":
                buffer[:] = 0.0  # (nothing of the model anywhere)

        film.GetOutputFloat = output
        return film

    module.RenderSession.GetFilm = film_with_alpha
    folder = worker_folder(tmp_path)
    assert render_worker.run(folder) == 0
    with Image.open(folder / render_worker.RESULT_FILE) as result:
        expected = tuple(round(c * 255) for c in render_scene.BACKGROUND)
        assert result.getpixel((5, 5)) == expected and result.getpixel((5, 34)) == expected


def test_the_time_only_counts_once_it_has_started_rendering(tmp_path, monkeypatch):
    """The first render on a computer compiles for its card for a couple of minutes: that isn't the render's time."""
    phases = []
    module, _log = fake_luxcore(passes=lambda polls: 0 if polls < 5 else polls)
    install_stand_in(monkeypatch, module)
    real = render_worker._Status.update

    def watched(self, **changes):
        if "phase" in changes:
            phases.append(changes["phase"])
        real(self, **changes)

    monkeypatch.setattr(render_worker._Status, "update", watched)
    monkeypatch.setattr(render_worker, "SLOW_START_SECONDS", -1.0)
    assert render_worker.run(worker_folder(tmp_path)) == 0
    assert "compiling" in phases and phases.index("compiling") < phases.index("rendering") < phases.index("done")


def test_a_first_renders_long_wait_is_warned_of_before_it_starts(tmp_path, monkeypatch):
    """LuxCore compiles for the card inside its start-up, for about 100 s, and reports nothing meanwhile."""
    said = {}
    module, _log = fake_luxcore()
    install_stand_in(monkeypatch, module)
    real_session = module.RenderSession

    def session_watching_status(config):
        said["while starting"] = state_of(folder)["message"]
        return real_session(config)

    module.RenderSession = session_watching_status
    folder = worker_folder(tmp_path)
    assert render_worker.run(folder) == 0
    assert "couple of minutes" in said["while starting"]


def test_stop_finishes_with_what_there_is(tmp_path, monkeypatch):
    module, log = fake_luxcore()
    install_stand_in(monkeypatch, module)
    folder = worker_folder(tmp_path, seconds=3600.0)
    (folder / render_worker.STOP_FILE).touch()
    start = time.monotonic()
    assert render_worker.run(folder) == 0
    assert time.monotonic() - start < 30 and state_of(folder)["phase"] == "done" and (folder / render_worker.RESULT_FILE).is_file()


def test_stop_before_any_rendering_leaves_nothing(tmp_path, monkeypatch):
    module, _log = fake_luxcore(passes=lambda _polls: 0)
    install_stand_in(monkeypatch, module)
    folder = worker_folder(tmp_path, seconds=3600.0)
    (folder / render_worker.STOP_FILE).touch()
    assert render_worker.run(folder) == 0
    assert state_of(folder)["phase"] == "stopped" and not (folder / render_worker.RESULT_FILE).exists()


def test_a_live_preview_appears(tmp_path, monkeypatch):
    module, _log = fake_luxcore()
    install_stand_in(monkeypatch, module)
    monkeypatch.setattr(render_worker, "PREVIEW_EVERY", 0.0)
    folder = worker_folder(tmp_path, seconds=0.2)
    assert render_worker.run(folder) == 0
    assert state_of(folder)["preview"] > 0 and (folder / render_worker.PREVIEW_FILE).is_file()


def test_when_luxcore_wont_start_it_says_why(tmp_path, monkeypatch):
    """As when no NVIDIA card can be used: CUDA_ERROR_NOT_INITIALIZED, even for the processor."""
    module, _log = fake_luxcore(device_error="CUDA driver API error CUDA_ERROR_NOT_INITIALIZED")
    install_stand_in(monkeypatch, module)
    folder = worker_folder(tmp_path)
    assert render_worker.run(folder) == 1
    state = state_of(folder)
    assert state["phase"] == "failed" and state["message"] == "it can't use this computer's graphics card (CUDA_ERROR_NOT_INITIALIZED)"


def test_with_no_engine_it_says_so(tmp_path):
    folder = worker_folder(tmp_path)
    assert render_worker.run(folder) == 1
    assert state_of(folder)["phase"] == "failed" and state_of(folder)["message"] == "it isn't installed"


def test_a_model_that_cant_be_read_says_so(tmp_path, monkeypatch):
    module, _log = fake_luxcore()
    install_stand_in(monkeypatch, module)
    folder = worker_folder(tmp_path)
    (tmp_path / "crate.obj").write_text("not a model", encoding="utf-8")
    assert render_worker.run(folder) == 1
    assert "couldn't be read" in state_of(folder)["message"]


# ----------------------------------------------------------------------
# The window, with a stand-in for the worker
# ----------------------------------------------------------------------
STAND_IN_WORKER = r"""
import json, os, sys, time
from pathlib import Path
from PIL import Image

folder = Path(sys.argv[1])
behaviour = sys.argv[2]
def say(**values):
    temporary = folder / "status.json.tmp"
    temporary.write_text(json.dumps(values), encoding="utf-8")
    for _ in range(50):
        try:
            os.replace(temporary, folder / "status.json")
            return
        except OSError:
            time.sleep(0.02)
if behaviour == "crash":
    os._exit(7)
if behaviour == "fail":
    say(phase="failed", message="CUDA_ERROR_NOT_INITIALIZED")
    sys.exit(1)
say(phase="rendering", message="Rendering...", rendered=0.0, preview=0, passes=1, device="Test Card")
if behaviour == "slow":
    end = time.time() + 60
    while time.time() < end and not (folder / "stop").exists():
        time.sleep(0.02)
    Image.new("RGB", (64, 36), (10, 200, 10)).save(folder / "preview.jpg")
    say(phase="rendering", message="Rendering...", rendered=1.0, preview=1, passes=2, device="Test Card")
    time.sleep(0.4)
if behaviour == "stopped":
    say(phase="stopped", message="Stopped.")
    sys.exit(0)
Image.new("RGB", (64, 36), (200, 10, 10)).save(folder / "result.png")
say(phase="done", message="Done.", passes=42, device="Test Card")
os._exit(0)
"""


@pytest.fixture
def stand_in_worker(tmp_path, monkeypatch):
    """The window starts this instead of the app: it behaves as asked. (It's a real process.)"""
    from ui import render_window

    script = tmp_path / "stand_in_worker.py"
    script.write_text(STAND_IN_WORKER, encoding="utf-8")
    behaviour = {"value": "ok"}

    def command(folder):
        return sys.executable, [str(script), str(folder), behaviour["value"]]

    monkeypatch.setattr(render_window, "worker_command", command)
    monkeypatch.setattr(render_engine, "supported", lambda: True)
    return behaviour


def fake_installed(monkeypatch):
    monkeypatch.setattr(render_engine, "supported", lambda: True)
    folder = render_engine.engine_folder()
    folder.mkdir(parents=True)
    (folder / "installed.json").write_text("{}", encoding="utf-8")


def choose(group, value):
    next(b for b in group.buttons() if b.property("value") == value).click()


def viewer_with_window(qapp, tmp_path):
    from ui.render_window import RenderWindow

    dialog = open_viewer(qapp, model_asset(tmp_path))
    assert pump(qapp, lambda: dialog.viewport.face_count > 0)
    window = RenderWindow(dialog.viewport, dialog._file_path(), dialog.asset.name, dialog)
    window.show()
    return dialog, window


def finish(qapp, dialog, window):
    window.reject()
    window.deleteLater()
    close(qapp, dialog)


def test_it_offers_to_download_first(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(render_engine, "supported", lambda: True)
    dialog, window = viewer_with_window(qapp, tmp_path)
    assert "Download LuxCore (157 MB)" in window.btn_render.text() and "free" in window.status.text()
    assert window.btn_remove.isHidden() and not window.btn_save.isEnabled() and not window.btn_stop.isEnabled()
    finish(qapp, dialog, window)


def test_the_download_then_renders(qapp, tmp_path, monkeypatch, stand_in_worker):
    serve(monkeypatch)
    dialog, window = viewer_with_window(qapp, tmp_path)
    window.btn_render.click()
    assert pump(qapp, lambda: window._result is not None)
    assert render_engine.is_installed() and window.btn_render.text() == "Render" and not window.btn_remove.isHidden()
    finish(qapp, dialog, window)


def test_a_failed_download_offers_the_viewers_renderer(qapp, tmp_path, monkeypatch):
    serve(monkeypatch, hashes={"lux.whl": "0" * 64})
    dialog, window = viewer_with_window(qapp, tmp_path)
    window.btn_render.click()
    assert pump(qapp, lambda: not window.btn_use_viewer.isHidden())
    assert "didn't download correctly" in window.status.text() and window.btn_render.isEnabled()
    finish(qapp, dialog, window)


def test_a_render_shows_its_result_and_it_can_be_saved(qapp, tmp_path, monkeypatch, stand_in_worker):
    from PySide6.QtWidgets import QFileDialog

    fake_installed(monkeypatch)
    dialog, window = viewer_with_window(qapp, tmp_path)
    window.btn_render.click()
    assert not window.btn_render.isEnabled() and window.btn_stop.isEnabled()  # (one at a time)
    assert pump(qapp, lambda: window.process is None)
    assert "Test Card" in window.status.text() and "42" in window.status.text() and window.btn_save.isEnabled()
    target = tmp_path / "picture"
    asked = []

    def ask(_parent, _title, suggested, _filter):
        asked.append(suggested)
        return str(target), ""

    monkeypatch.setattr(QFileDialog, "getSaveFileName", ask)
    window.btn_save.click()
    assert Image.open(str(target) + ".png").getpixel((1, 1))[0] > 150  # (.png added)
    assert asked[0].endswith("crate_render_1080p.png")
    assert get_render_settings()["folder"] == str(tmp_path)
    finish(qapp, dialog, window)


@pytest.mark.parametrize("behaviour, words", [("fail", "NOT_INITIALIZED"), ("crash", "unexpectedly")])
def test_when_luxcore_cant_render_it_offers_the_viewers_renderer(qapp, tmp_path, monkeypatch, stand_in_worker, behaviour, words):
    fake_installed(monkeypatch)
    stand_in_worker["value"] = behaviour
    dialog, window = viewer_with_window(qapp, tmp_path)
    window.btn_render.click()
    assert pump(qapp, lambda: not window.btn_use_viewer.isHidden())
    assert words in window.status.text() and "no cast shadows" in window.status.text() and not window.btn_save.isEnabled()
    monkeypatch.setattr(dialog.viewport, "render_image", lambda w, h: QImage(w, h, QImage.Format.Format_RGB32))
    window.btn_use_viewer.click()  # (one click, and there's a picture)
    assert window.engine() == "viewer" and window._image is not None and window.btn_save.isEnabled() and window.btn_use_viewer.isHidden()
    assert get_render_settings()["engine"] == "viewer"
    finish(qapp, dialog, window)


def test_a_worker_that_wont_start(qapp, tmp_path, monkeypatch):
    from ui import render_window

    fake_installed(monkeypatch)
    monkeypatch.setattr(render_window, "worker_command", lambda folder: (str(tmp_path / "no_such_program.exe"), []))
    dialog, window = viewer_with_window(qapp, tmp_path)
    window.btn_render.click()
    assert pump(qapp, lambda: not window.btn_use_viewer.isHidden())
    assert window.process is None and window.btn_render.isEnabled()
    finish(qapp, dialog, window)


def test_stop_keeps_what_was_rendered(qapp, tmp_path, monkeypatch, stand_in_worker):
    fake_installed(monkeypatch)
    stand_in_worker["value"] = "slow"
    dialog, window = viewer_with_window(qapp, tmp_path)
    window.btn_render.click()
    assert pump(qapp, lambda: window._read_status().get("phase") == "rendering")
    window.btn_stop.click()
    assert window.status.text() == "Finishing..."
    assert pump(qapp, lambda: window._preview_seen > 0, seconds=30)  # (a live preview on the way)
    assert pump(qapp, lambda: window.process is None, seconds=30)
    assert window._result is not None and window.btn_save.isEnabled()
    finish(qapp, dialog, window)


def test_closing_stops_a_render_and_clears_up(qapp, tmp_path, monkeypatch, stand_in_worker):
    fake_installed(monkeypatch)
    stand_in_worker["value"] = "slow"
    dialog, window = viewer_with_window(qapp, tmp_path)
    window.btn_render.click()
    process, folder = window.process, window.folder
    assert pump(qapp, lambda: window._read_status().get("phase") == "rendering")
    pid = process.processId()
    assert pid
    window.reject()
    assert window.process is None and not folder.exists()
    close(qapp, dialog)


def test_a_new_render_clears_the_last_ones_folder(qapp, tmp_path, monkeypatch, stand_in_worker):
    fake_installed(monkeypatch)
    dialog, window = viewer_with_window(qapp, tmp_path)
    window.btn_render.click()
    assert pump(qapp, lambda: window.process is None)
    first = window.folder
    window.btn_render.click()
    assert not first.exists()
    assert pump(qapp, lambda: window.process is None)
    finish(qapp, dialog, window)


def test_old_folders_left_by_a_crash_are_cleared(qapp, tmp_path, monkeypatch):
    fake_installed(monkeypatch)
    left = render_engine.jobs_folder() / "abc"
    left.mkdir(parents=True)
    dialog, window = viewer_with_window(qapp, tmp_path)
    assert not left.exists() and render_engine.is_installed()
    finish(qapp, dialog, window)


def test_luxcore_can_be_removed(qapp, tmp_path, monkeypatch):
    fake_installed(monkeypatch)
    dialog, window = viewer_with_window(qapp, tmp_path)
    assert not window.btn_remove.isHidden()
    window.btn_remove.click()
    assert not render_engine.is_installed() and window.btn_remove.isHidden() and "Download" in window.btn_render.text()
    finish(qapp, dialog, window)


def test_the_viewers_renderer_needs_no_download(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(render_engine, "supported", lambda: True)
    asked = []

    def draw(width, height):
        asked.append((width, height))
        return QImage(width, height, QImage.Format.Format_RGB32)

    dialog, window = viewer_with_window(qapp, tmp_path)
    monkeypatch.setattr(dialog.viewport, "render_image", draw)
    choose(window.engine_choice, "viewer")
    assert window.btn_render.text() == "Render" and not any(b.isEnabled() for b in window.quality_choice.buttons())
    assert not window.check_denoise.isEnabled()
    for size, (width, height) in render_scene.RESOLUTIONS.items():
        choose(window.size_choice, size)
        window.btn_render.click()
        assert asked[-1] == (width, height) and window._image is not None and window.btn_save.isEnabled()
    assert not render_engine.is_installed()
    finish(qapp, dialog, window)


def test_the_viewer_not_being_ready_is_said(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(render_engine, "supported", lambda: True)
    dialog, window = viewer_with_window(qapp, tmp_path)
    monkeypatch.setattr(dialog.viewport, "render_image", lambda _w, _h: None)
    choose(window.engine_choice, "viewer")
    window.btn_render.click()
    assert "couldn't draw" in window.status.text() and not window.btn_save.isEnabled()
    finish(qapp, dialog, window)


def test_the_choices_are_kept(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(render_engine, "supported", lambda: True)
    dialog, window = viewer_with_window(qapp, tmp_path)
    assert (window.engine(), window._value(window.size_choice), window._value(window.quality_choice)) == ("luxcore", "1080p", "good")
    choose(window.size_choice, "1440p")
    choose(window.quality_choice, "best")
    window.check_denoise.setChecked(False)
    finish(qapp, dialog, window)
    settings = get_render_settings()
    assert (settings["resolution"], settings["quality"], settings["denoise"]) == ("1440p", "best", False)
    dialog, window = viewer_with_window(qapp, tmp_path)
    assert window._value(window.size_choice) == "1440p" and not window.check_denoise.isChecked()
    finish(qapp, dialog, window)


def test_damaged_choices_fall_back():
    set_render_settings(engine="x", resolution="9k", quality=3, denoise="yes", folder=7)
    assert get_render_settings() == {"engine": "luxcore", "resolution": "1080p", "quality": "good", "denoise": True, "folder": ""}


def test_luxcore_isnt_offered_on_a_python_it_doesnt_fit(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(render_engine, "supported", lambda: False)
    dialog, window = viewer_with_window(qapp, tmp_path)
    assert not window.btn_render.isEnabled() and "isn't available" in window.status.text()
    finish(qapp, dialog, window)


# ----------------------------------------------------------------------
# The job, from what's on screen; and the button
# ----------------------------------------------------------------------
def test_the_job_is_what_the_viewport_shows(qapp, tmp_path):
    from ui.render_window import build_job

    dialog = open_viewer(qapp, model_asset(tmp_path))
    viewport = dialog.viewport
    viewport.rot_x, viewport.rot_y, viewport.pan_x, viewport.zoom = 10.0, 40.0, 0.2, 2.0
    viewport.set_color(QColor("#ff8000"), remember=False)
    viewport.material_id, viewport.roughness = "metallic", 0.7
    viewport.lighting_id = "dramatic"
    job_now = build_job(viewport, Path("m.obj"))
    assert job_now.eye == pytest.approx(render_scene.camera(10.0, 40.0, 0.2, 0.0, 2.0)[0])
    assert job_now.material["type"] == "metal2" and float(job_now.material["uroughness"]) == pytest.approx(0.49)
    assert job_now.hdri is None and len(job_now.lights) == len(LIGHTINGS["dramatic"].lights) and job_now.exposure == render_scene.PRESET_EXPOSURE
    close(qapp, dialog)


def test_the_job_under_an_hdri(qapp, tmp_path):
    from tests.test_model_hdri import write_panorama
    from ui.render_window import build_job

    path = write_panorama(tmp_path)
    set_model_view(hdri=str(path), hdri_exposure=1.0, hdri_backdrop=True)
    dialog = open_viewer(qapp, model_asset(tmp_path))
    viewport = dialog.viewport
    assert pump(qapp, lambda: viewport.environment is not None)
    viewport.light_yaw = 33.0
    job_now = build_job(viewport, Path("m.obj"))
    assert job_now.hdri == str(path) and job_now.backdrop and job_now.lights == [] and job_now.ambient is None
    assert job_now.sky_matrix == pytest.approx(render_scene.sky_matrix(33.0))
    assert job_now.exposure == pytest.approx(viewport.environment.exposure * 2.0)
    close(qapp, dialog)


def test_an_hdri_inside_an_archive_is_given_as_its_unpacked_copy(tmp_path):
    from core import archives
    from tests.test_model_hdri import write_panorama

    hdr = write_panorama(tmp_path)
    pack = tmp_path / "pack.zip"
    with zipfile.ZipFile(pack, "w") as z:
        z.write(hdr, "noon.hdr")
    local = environment.local_path(str(archives.virtual_path(pack, "noon.hdr")))
    assert local.is_file() and archives.SEPARATOR not in str(local)


def test_the_button_waits_for_the_model_and_a_real_material(qapp, tmp_path):
    dialog = open_viewer(qapp, model_asset(tmp_path))
    button = dialog.btn_render
    assert not button.isEnabled() and "loaded" in button.toolTip()
    assert pump(qapp, lambda: button.isEnabled())
    for material_id in ("toon", "normals"):
        dialog.cmb_material.setCurrentIndex(dialog.cmb_material.findData(material_id))
        assert not button.isEnabled() and MATERIALS[material_id].label in button.toolTip()
    dialog.cmb_material.setCurrentIndex(dialog.cmb_material.findData("clay"))
    assert button.isEnabled() and "F12" in button.toolTip()
    dialog.btn_uv.click()  # (a flat UV layout isn't what it renders)
    assert not button.isEnabled()
    dialog.btn_uv.click()
    assert button.isEnabled()
    close(qapp, dialog)


def test_f12_opens_it(qapp, tmp_path, monkeypatch):
    from ui import render_window

    opened = []
    monkeypatch.setattr(render_window.RenderWindow, "exec", lambda self: opened.append((self.model, self.name)))
    dialog = open_viewer(qapp, model_asset(tmp_path))
    assert pump(qapp, lambda: dialog.btn_render.isEnabled())
    dialog.viewport.setFocus()
    QTest.keyClick(dialog.viewport, Qt.Key.Key_F12)
    assert opened == [(dialog._file_path(), "crate.obj")]
    dialog.cmb_material.setCurrentIndex(dialog.cmb_material.findData("toon"))
    QTest.keyClick(dialog.viewport, Qt.Key.Key_F12)  # (greyed out: nothing happens)
    assert len(opened) == 1
    close(qapp, dialog)


def test_a_model_that_cant_be_read_cant_be_rendered(qapp, tmp_path):
    path = tmp_path / "broken.fbx"
    path.write_bytes(b"not a model")
    asset = AssetItem(path, path.name, ".fbx", os.path.getsize(path), os.path.getmtime(path), "3D Models", "FBX")
    dialog = open_viewer(qapp, asset)
    assert pump(qapp, lambda: dialog.viewport.mesh is not None)
    assert not dialog.btn_render.isEnabled() and "couldn't be read" in dialog.btn_render.toolTip()
    close(qapp, dialog)


def test_an_alphas_3d_view_has_no_render_button(qapp, tmp_path):
    arrays = np.tile(np.linspace(0, 255, 64, dtype=np.uint8), (64, 1))
    path = tmp_path / "alpha_brush.png"
    Image.fromarray(arrays, "L").save(path)
    asset = AssetItem(path, path.name, ".png", os.path.getsize(path), os.path.getmtime(path), "Textures", "PNG")
    dialog = open_viewer(qapp, asset)
    assert pump(qapp, lambda: not dialog.btn_3d.isHidden())
    dialog.btn_3d.click()
    assert dialog.btn_render is None
    close(qapp, dialog)
