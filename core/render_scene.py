"""What a render is: the viewer's model, camera, look and lighting as a job for LuxCore (core.render_worker).

Nothing here needs LuxCore. The job is plain numbers and paths, written to a file for the worker
process, and scene_properties() turns it into LuxCore's scene description. The look follows the viewer:
the same camera, the same HDRI turned the same way with the same exposure (core.environment), and the
same tone mapping (the worker's tone_map is the viewer's shader's sums).
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from .environment import view_to_env
from .model_look import ENV_HIGH, ENV_LOW, Lighting, light_directions

RESOLUTIONS = {"720p": (1280, 720), "1080p": (1920, 1080), "1440p": (2560, 1440)}
QUALITIES = {"draft": ("Draft", 5), "good": ("Good", 20), "best": ("Best", 60)}  # (name, seconds of rendering)
DEFAULT_RESOLUTION = "1080p"
DEFAULT_QUALITY = "good"
UNRENDERABLE = {"toon": "Toon", "normals": "Normals"}  # not real-world materials: the viewer shows these, a render can't
FIELD_OF_VIEW = 45.0  # degrees, top to bottom: the viewer's
CAMERA_DISTANCE = 3.2  # the viewer's, at zoom 1
BACKGROUND = (0.08, 0.09, 0.11)  # the viewer's, without a backdrop
PRESET_EXPOSURE = 1.4  # (a preset's light was made for no tone mapping: the curve darkens its middle)
JOB_FILE = "job.json"


@dataclass
class RenderJob:
    model: str
    width: int
    height: int
    seconds: float
    denoise: bool
    eye: list[float]
    target: list[float]
    up: list[float]
    material: dict[str, str] = field(default_factory=dict)  # LuxCore's properties for it, without the prefix
    textures: dict[str, str] = field(default_factory=dict)
    hdri: Optional[str] = None  # a file on disk
    sky_matrix: Optional[list[float]] = None  # how it's turned: 16 numbers, as LuxCore wants them
    lights: list[dict] = field(default_factory=list)  # a preset's: {"direction", "color"} (toward the light)
    ambient: Optional[list[float]] = None  # a preset's soft light
    exposure: float = 1.0
    backdrop: bool = False

    def save(self, folder: Path) -> Path:
        path = folder / JOB_FILE
        path.write_text(json.dumps(asdict(self), indent=1), encoding="utf-8")
        return path

    @staticmethod
    def load(folder: Path) -> "RenderJob":
        return RenderJob(**json.loads((folder / JOB_FILE).read_text(encoding="utf-8")))


def camera(rot_x: float, rot_y: float, pan_x: float, pan_y: float, zoom: float) -> tuple[list[float], list[float], list[float]]:
    """(eye, target, up) of the viewer's camera, in the model's space: its view is a turn by rot_x and
    rot_y, then a move by the pan and the distance."""
    to_world = view_to_env(rot_x, rot_y, 0.0)
    distance = CAMERA_DISTANCE / max(zoom, 0.05)
    eye = to_world @ np.array([-pan_x, -pan_y, distance])
    target = eye + to_world @ np.array([0.0, 0.0, -distance])
    return eye.tolist(), target.tolist(), (to_world @ np.array([0.0, 1.0, 0.0])).tolist()


def lens_angle(width: int, height: int) -> float:
    """LuxCore's field of view, which runs along an image's longer side: the viewer's top-to-bottom
    angle at this shape, so a wide image shows what's on screen, with more at the sides."""
    return math.degrees(2 * math.atan(math.tan(math.radians(FIELD_OF_VIEW / 2)) * max(width / height, 1.0)))


def sky_matrix(yaw: float) -> list[float]:
    """LuxCore lays a panorama round its own z axis: this puts it in the viewer's panorama space
    (y up, the image's middle along -z), turned yaw degrees as the viewer turns it. Column by column."""
    a = math.radians(yaw)
    turn = np.array([[math.cos(a), 0, math.sin(a)], [0, 1, 0], [-math.sin(a), 0, math.cos(a)]])
    m = turn @ np.array([[0, -1, 0], [0, 0, 1], [1, 0, 0]], dtype=float)
    return [m[0, 0], m[1, 0], m[2, 0], 0.0, m[0, 1], m[1, 1], m[2, 1], 0.0, m[0, 2], m[1, 2], m[2, 2], 0.0, 0.0, 0.0, 0.0, 1.0]


def linear(color: tuple[float, float, float]) -> list[float]:
    """A colour as picked (display) as light: as the viewer's shader decodes it under an HDRI."""
    return [max(0.0, c) ** 2.2 for c in color]


def surface(material_id: str, color: tuple[float, float, float], roughness: float) -> tuple[dict[str, str], dict[str, str]]:
    """(the material's properties, any textures it needs) for a viewer material."""
    r, g, b = linear(color)
    rgb = f"{r:.5f} {g:.5f} {b:.5f}"
    if material_id == "metallic":
        amount = max(0.002, min(1.0, roughness) ** 2)  # (the slider's 50 is LuxCore's 0.25: a satin metal)
        return (
            {"type": "metal2", "fresnel": "metal_colour", "uroughness": f"{amount:.4f}", "vroughness": f"{amount:.4f}"},
            {"metal_colour.type": "fresnelcolor", "metal_colour.kr": rgb},
        )
    if material_id == "glossy":
        return {"type": "glossy2", "kd": rgb, "ks": "0.08 0.08 0.08", "uroughness": "0.06", "vroughness": "0.06"}, {}
    if material_id == "phong":
        return {"type": "glossy2", "kd": rgb, "ks": "0.04 0.04 0.04", "uroughness": "0.25", "vroughness": "0.25"}, {}
    if material_id == "clay":
        return {"type": "roughmatte", "kd": rgb, "sigma": "30"}, {}
    return {"type": "matte", "kd": rgb}, {}


def preset_lights(lighting: Lighting, rot_x: float, rot_y: float, yaw: float, pitch: float, metal: bool = False) -> tuple[list[dict], list[float]]:
    """A lighting preset's lights, which turn with the camera in the viewer, as they stand in the
    model's space now, and its soft light (the average of its sky and ground). Metal gets the soft box the
    viewer gives it, which every preset shares: with a preset's own soft light, there's nothing for it to reflect."""
    to_world = view_to_env(rot_x, rot_y, 0.0)
    if metal:
        lights = [
            {"direction": (to_world @ direction).tolist(), "color": linear(light.color)}
            for direction, light in zip(light_directions(lighting, yaw, pitch), lighting.lights)
        ]
        return lights, linear(tuple((low + high) / 2 for low, high in zip(ENV_LOW, ENV_HIGH)))
    lights = [
        {"direction": (to_world @ direction).tolist(), "color": linear(light.color)}
        for direction, light in zip(light_directions(lighting, yaw, pitch), lighting.lights)
    ]
    return lights, linear(tuple((s + g) / 2 for s, g in zip(lighting.sky, lighting.ground)))


def scene_properties(job: RenderJob, mesh_file: str) -> dict[str, object]:
    """LuxCore's description of the scene, as property: value."""
    props: dict[str, object] = {
        "scene.camera.type": "perspective",
        "scene.camera.lookat.orig": job.eye,
        "scene.camera.lookat.target": job.target,
        "scene.camera.up": job.up,
        "scene.camera.fieldofview": lens_angle(job.width, job.height),
        "scene.shapes.model.type": "mesh",
        "scene.shapes.model.ply": mesh_file,
        "scene.objects.model.shape": "model",
        "scene.objects.model.material": "surface",
    }
    props.update({f"scene.textures.{name}": value for name, value in job.textures.items()})
    props.update({f"scene.materials.surface.{name}": value for name, value in job.material.items()})
    if job.hdri is not None:
        props.update({
            "scene.lights.sky.type": "infinite",
            "scene.lights.sky.file": job.hdri,
            "scene.lights.sky.gamma": 1.0,  # (LuxCore takes images for gamma 2.2: a value of 120,000 goes astronomical)
            "scene.lights.sky.transformation": job.sky_matrix,
        })
    for number, light in enumerate(job.lights):
        prefix = f"scene.lights.key{number}"
        props.update({
            f"{prefix}.type": "sharpdistant",
            f"{prefix}.direction": [-v for v in light["direction"]],  # (LuxCore: the way the light travels)
            f"{prefix}.color": light["color"],
            f"{prefix}.gain": [math.pi] * 3,  # (a white surface facing it shows its colour)
        })
    if job.ambient is not None:
        props.update({"scene.lights.soft.type": "constantinfinite", "scene.lights.soft.color": job.ambient})
    return props


def film_properties(job: RenderJob, engine: str) -> dict[str, object]:
    """How it's rendered. Two versions of the image: 0 as it stands (the live preview), 1 denoised (the
    result: denoising takes seconds, so it isn't done for the preview). Both come out in linear light."""
    props: dict[str, object] = {
        "renderengine.type": engine,
        "sampler.type": "SOBOL",
        "path.pathdepth.total": 6,
        "path.forceblackbackground.enable": 0 if job.backdrop else 1,
        "film.width": job.width,
        "film.height": job.height,
        "opencl.cpu.use": 0,
        "opencl.gpu.use": 1,
        "film.imagepipelines.0.0.type": "TONEMAP_LINEAR",
        "film.imagepipelines.0.0.scale": job.exposure,
        "film.imagepipelines.1.0.type": "INTEL_OIDN",
        "film.imagepipelines.1.1.type": "TONEMAP_LINEAR",
        "film.imagepipelines.1.1.scale": job.exposure,
        "film.outputs.0.type": "RGB_IMAGEPIPELINE",
        "film.outputs.0.index": 0,
        "film.outputs.0.filename": "preview.exr",
        "film.outputs.1.type": "RGB_IMAGEPIPELINE",
        "film.outputs.1.index": 1,
        "film.outputs.1.filename": "denoised.exr",
        "film.outputs.2.type": "ALPHA",
        "film.outputs.2.filename": "alpha.exr",
    }
    return props
