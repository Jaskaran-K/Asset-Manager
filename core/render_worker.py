"""A render, run in a process of its own (AssetManager.exe --render FOLDER), so that a graphics driver
crash or running out of memory can't take the library with it.

The window talks to it through files in the job's folder, as the windowed executable has no console:
it writes status.json, preview.jpg and result.png; the window asks it to finish early by creating
"stop". The model is read again here: the viewer's copy has gone to the graphics card.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PIL import Image

from . import render_engine
from .render_scene import BACKGROUND, RenderJob, film_properties, scene_properties

STATUS_FILE = "status.json"
PREVIEW_FILE = "preview.jpg"
RESULT_FILE = "result.png"
STOP_FILE = "stop"
LOG_FILE = "log.txt"
PREVIEW_EVERY = 1.0  # seconds
POLL_EVERY = 0.2
# Before the first pass, on a computer that has never rendered: LuxCore compiles for its graphics card, once
SLOW_START_SECONDS = 4.0
REPLACE_TRIES = 40  # at 30 ms apart: the window only ever has a file open for moments


class RenderFailed(Exception):
    """The render can't be done (the message says why, for the user)."""


def tone_map(rgb: np.ndarray) -> np.ndarray:
    """Linear light, exposed, to display values 0-1: the viewer's shader's ACES curve and gamma."""
    x = np.maximum(rgb, 0.0)
    mapped = np.clip((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14), 0.0, 1.0)
    return mapped ** (1.0 / 2.2)


def write_ply(path: Path, points: np.ndarray, faces: np.ndarray, normals: np.ndarray) -> None:
    """The mesh as a binary PLY for LuxCore to read: 14 million triangles take about a second, and
    handing them over as Python lists 11 seconds and a gigabyte more."""
    vertex = np.zeros(len(points), dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("nx", "<f4"), ("ny", "<f4"), ("nz", "<f4")])
    vertex["x"], vertex["y"], vertex["z"] = points[:, 0], points[:, 1], points[:, 2]
    vertex["nx"], vertex["ny"], vertex["nz"] = normals[:, 0], normals[:, 1], normals[:, 2]
    face = np.zeros(len(faces), dtype=[("n", "u1"), ("a", "<u4"), ("b", "<u4"), ("c", "<u4")])
    face["n"], face["a"], face["b"], face["c"] = 3, faces[:, 0], faces[:, 1], faces[:, 2]
    header = (
        "ply\nformat binary_little_endian 1.0\n"
        f"element vertex {len(points)}\nproperty float x\nproperty float y\nproperty float z\n"
        "property float nx\nproperty float ny\nproperty float nz\n"
        f"element face {len(faces)}\nproperty list uchar uint vertex_indices\nend_header\n"
    )
    with open(path, "wb") as f:
        f.write(header.encode("ascii"))
        vertex.tofile(f)
        face.tofile(f)


def _replace(temporary: Path, target: Path) -> bool:
    """Put a finished file in place. On Windows that's refused for the moment the window has the old
    one open to read it: a few tries get past that. True if it's in place."""
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(temporary, target)
            return True
        except OSError:
            if attempt < REPLACE_TRIES - 1:
                time.sleep(0.03)
    return False


class _Status:
    """status.json, replaced whole, so the window never reads half of it."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.values: dict[str, object] = {"phase": "starting", "message": "Starting...", "passes": 0, "rendered": 0.0, "preview": 0}

    def update(self, **changes: object) -> None:
        self.values.update(changes)
        temporary = self.folder / (STATUS_FILE + ".tmp")
        temporary.write_text(json.dumps(self.values), encoding="utf-8")
        _replace(temporary, self.folder / STATUS_FILE)  # (a report that can't land isn't worth failing a render for)


def _properties(values: dict[str, object], pyluxcore):
    """LuxCore's Properties from property: value (a file name may hold anything its text form wouldn't take)."""
    props = pyluxcore.Properties()
    for name, value in values.items():
        props.Set(pyluxcore.Property(name, value if isinstance(value, list) else [value]))
    return props


def _pick_engine(pyluxcore) -> tuple[str, str]:
    """(LuxCore's engine, what it's running on): the graphics card if there's one it can use."""
    try:
        devices = pyluxcore.GetOpenCLDeviceDescs()
    except RuntimeError as exc:  # (CUDA is there but won't start: nothing renders then, not even on the processor)
        found = re.search(r"CUDA_ERROR_\w+", str(exc))
        raise RenderFailed(f"it can't use this computer's graphics card ({found.group(0) if found else exc})") from exc
    names = devices.GetAllUniqueSubNames("opencl.device")
    for name in names:
        kind = devices.Get(f"{name}.type").GetString()
        if "GPU" in kind:
            return "PATHOCL", devices.Get(f"{name}.name").GetString()
    return "PATHCPU", "the processor (slower)"


def _image(film, pyluxcore, job: RenderJob, index: int, alpha: Optional[np.ndarray]) -> np.ndarray:
    """One of the film's versions, as display values H x W x 3, the top row first."""
    rgb = np.zeros((job.height, job.width, 3), dtype=np.float32)
    film.GetOutputFloat(pyluxcore.FilmOutputType.RGB_IMAGEPIPELINE, rgb, index)
    shown = tone_map(rgb[::-1])
    if alpha is None:
        return shown
    return shown * alpha + np.array(BACKGROUND, dtype=np.float32) * (1.0 - alpha)


def _alpha(film, pyluxcore, job: RenderJob) -> Optional[np.ndarray]:
    """How much of each pixel is the model (without a backdrop: the rest is the viewer's background)."""
    if job.backdrop:
        return None
    alpha = np.zeros((job.height, job.width, 1), dtype=np.float32)
    film.GetOutputFloat(pyluxcore.FilmOutputType.ALPHA, alpha)
    return alpha[::-1].clip(0.0, 1.0)


def _to_8bit(image: np.ndarray) -> Image.Image:
    return Image.fromarray((image.clip(0.0, 1.0) * 255.0 + 0.5).astype(np.uint8))


def _save(image: Image.Image, path: Path, **options) -> bool:
    temporary = path.with_name(path.name + ".tmp")
    image.save(temporary, format=path.suffix[1:].replace("jpg", "jpeg").upper(), **options)
    return _replace(temporary, path)


def render(folder: Path, status: _Status, should_stop: Callable[[], bool]) -> None:
    job = RenderJob.load(folder)
    status.update(phase="starting", message="Starting LuxCore...")
    if not render_engine.is_installed():
        raise RenderFailed("it isn't installed")
    render_engine.add_to_path()
    import pyluxcore

    log = open(folder / LOG_FILE, "a", encoding="utf-8", errors="replace")

    def log_line(line: str) -> None:
        log.write(line + "\n")
        log.flush()

    pyluxcore.Init(log_line)
    engine, device = _pick_engine(pyluxcore)

    status.update(phase="reading", message="Reading the model...", device=device)
    from .model_loader import load_3d_model

    mesh = load_3d_model(Path(job.model))
    if mesh.stand_in or mesh.face_count == 0:
        raise RenderFailed("the model couldn't be read")
    mesh_file = folder / "mesh.ply"
    write_ply(mesh_file, np.asarray(mesh.vertices, dtype=np.float32), np.asarray(mesh.faces, dtype=np.uint32), np.asarray(mesh.normals, dtype=np.float32))
    del mesh

    # (LuxCore compiles for the graphics card inside this next step, and says nothing till it's done:
    # the only time to say what may be coming is before it)
    status.update(phase="preparing", message="Preparing the scene. On a computer's first render, this also prepares its graphics card: a couple of minutes...")
    scene = pyluxcore.Scene()
    scene.Parse(_properties(scene_properties(job, str(mesh_file)), pyluxcore))
    config = pyluxcore.RenderConfig(_properties(film_properties(job, engine), pyluxcore), scene)
    session = pyluxcore.RenderSession(config)
    session.Start()

    started = time.monotonic()
    rendering_since: Optional[float] = None
    last_preview = 0.0
    previews = 0
    try:
        while True:
            time.sleep(POLL_EVERY)
            session.UpdateStats()
            passes = session.GetStats().Get("stats.renderengine.pass").GetInt()
            now = time.monotonic()
            if rendering_since is None:
                if passes == 0:
                    if now - started > SLOW_START_SECONDS:
                        status.update(phase="compiling", message="Preparing your graphics card. The first time takes a couple of minutes...")
                    if should_stop():
                        return  # (nothing's been rendered: there's nothing to keep)
                    continue
                rendering_since = now
                status.update(phase="rendering", message=f"Rendering on {device}...")
            rendered = now - rendering_since
            status.update(passes=passes, rendered=rendered)
            if should_stop() or rendered >= job.seconds:
                break
            if now - last_preview >= PREVIEW_EVERY:
                last_preview = now
                film = session.GetFilm()
                if _save(_to_8bit(_image(film, pyluxcore, job, 0, _alpha(film, pyluxcore, job))), folder / PREVIEW_FILE, quality=88):
                    previews += 1
                    status.update(preview=previews)
    finally:
        session.Stop()

    film = session.GetFilm()
    alpha = _alpha(film, pyluxcore, job)
    if job.denoise:
        status.update(phase="denoising", message="Denoising...")
    _save(_to_8bit(_image(film, pyluxcore, job, 1 if job.denoise else 0, alpha)), folder / RESULT_FILE, compress_level=4)
    session = config = scene = None  # (LuxCore lets go of the mesh file)
    try:
        mesh_file.unlink()
    except OSError:
        pass
    status.update(phase="done", message="Done.")


def run(folder: Path) -> int:
    """The process's work: an exit code, with what became of it in status.json."""
    status = _Status(folder)
    status.update()
    try:
        render(folder, status, (folder / STOP_FILE).exists)
    except RenderFailed as exc:
        status.update(phase="failed", message=str(exc))
        return 1
    except Exception as exc:  # pylint: disable=broad-except
        try:
            (folder / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
        status.update(phase="failed", message=str(exc).strip().split("\n")[0] or type(exc).__name__)
        return 1
    if status.values["phase"] != "done":  # (stopped before anything was rendered)
        status.update(phase="stopped", message="Stopped.")
    if sys.stdout is not None:
        sys.stdout.flush()
    return 0
