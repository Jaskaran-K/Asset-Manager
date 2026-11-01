"""How 3D models are shaded: materials and lighting set-ups, for the 3D viewer and thumbnails.

The viewer's shader (ui.viewer) and the thumbnail renderer (core.model_thumbnail) both shade
with what's here, so a thumbnail looks like the model does in the viewer. Lights are given in
view space (x right, y up, z toward the viewer): they stay put while the model is turned, and
are turned themselves with a yaw / pitch (light_directions).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Material:
    label: str
    tip: str
    diffuse: float = 1.0  # how much of the light the colour takes
    wrap: float = 0.0  # light wrapping past the shadow line (soft, like clay)
    specular: float = 0.0  # highlight strength
    shininess: float = 16.0  # highlight tightness
    phong: bool = False  # Phong's reflection highlight (else Blinn's half-vector one)
    metal: float = 0.0  # highlights and reflections take the colour; the sky / floor reflect in it
    rim: float = 0.0  # light along the silhouette
    bands: int = 0  # toon: this many flat steps of light
    normals: bool = False  # show the surface direction as colour (to check normals)


MATERIALS: dict[str, Material] = {
    "matte": Material("Matte", "No shine: only the shape's light and shadow", diffuse=1.0),
    "clay": Material("Clay", "Soft, like sculpting clay: light wraps round the forms", diffuse=0.95, wrap=0.5, specular=0.06, shininess=6.0, rim=0.06),
    "phong": Material("Phong", "Classic shading with a soft highlight", diffuse=0.9, specular=0.45, shininess=24.0, phong=True, rim=0.12),
    "glossy": Material("Glossy", "Shiny plastic or varnish: tight, bright highlights", diffuse=0.85, specular=0.9, shininess=90.0, rim=0.22),
    "metallic": Material("Metallic", "Metal: the colour is in the highlights and reflections", diffuse=0.25, specular=1.0, shininess=60.0, metal=1.0, rim=0.2),
    "toon": Material("Toon", "Flat steps of light, like a cartoon", diffuse=1.0, specular=0.35, shininess=40.0, bands=3),
    "normals": Material("Normals", "The surface direction as colour: to check the model's normals", normals=True),
}


@dataclass(frozen=True)
class Light:
    direction: tuple[float, float, float]  # toward the light, in view space
    color: tuple[float, float, float]  # strength in each channel


@dataclass(frozen=True)
class Lighting:
    label: str
    tip: str
    lights: tuple[Light, ...]
    sky: tuple[float, float, float]  # ambient light from above
    ground: tuple[float, float, float]  # ... and from below


LIGHTINGS: dict[str, Lighting] = {
    "studio": Lighting(
        "Studio", "A key light, a cool fill and a rim light from behind",
        (Light((0.55, 0.60, 0.60), (0.85, 0.83, 0.80)), Light((-0.65, 0.20, 0.45), (0.28, 0.31, 0.36)),
         Light((0.0, 0.45, -1.0), (0.45, 0.45, 0.45))),
        (0.22, 0.23, 0.26), (0.10, 0.10, 0.11),
    ),
    "soft": Lighting(
        "Soft", "Even, low-contrast light, like an overcast sky",
        (Light((0.30, 0.80, 0.50), (0.50, 0.50, 0.50)), Light((-0.50, 0.30, 0.60), (0.30, 0.30, 0.30))),
        (0.46, 0.47, 0.50), (0.28, 0.28, 0.29),
    ),
    "dramatic": Lighting(
        "Dramatic", "One strong warm light from the side and a cool edge light: deep shadows",
        (Light((1.0, 0.35, 0.15), (1.15, 1.0, 0.85)), Light((-0.6, 0.3, -0.8), (0.35, 0.45, 0.65))),
        (0.05, 0.05, 0.06), (0.02, 0.02, 0.02),
    ),
    "top": Lighting(
        "Top", "Light from straight above, as under a lamp",
        (Light((0.0, 1.0, 0.15), (1.0, 0.98, 0.95)),),
        (0.14, 0.14, 0.16), (0.04, 0.04, 0.05),
    ),
    "headlight": Lighting(
        "Headlight", "Light from where you're looking: every side you see is lit",
        (Light((0.0, 0.0, 1.0), (0.9, 0.9, 0.9)),),
        (0.16, 0.16, 0.18), (0.10, 0.10, 0.11),
    ),
}
MAX_LIGHTS = max(len(lighting.lights) for lighting in LIGHTINGS.values())
# What metal reflects, whatever the lighting: a dark floor below and a bright soft box above
# (with only the lighting's ambient, metal under Dramatic lighting was nearly black)
ENV_LOW = (0.05, 0.05, 0.06)
ENV_HIGH = (0.72, 0.74, 0.78)
DEFAULT_COLOR = "#8a9bb4"
DEFAULT_MATERIAL = "phong"
DEFAULT_LIGHTING = "studio"


def light_directions(lighting: Lighting, yaw: float = 0.0, pitch: float = 0.0) -> np.ndarray:
    """The lights' directions (unit vectors, view space): tipped by pitch (degrees; positive tips
    them toward the viewer and down, from above), then swung round the vertical by yaw
    (positive: to the right, from the front)."""
    dirs = np.array([light.direction for light in lighting.lights], dtype=np.float64)
    dirs /= np.linalg.norm(dirs, axis=1, keepdims=True)
    a, b = math.radians(yaw), math.radians(pitch)
    turn_y = np.array([[math.cos(a), 0, math.sin(a)], [0, 1, 0], [-math.sin(a), 0, math.cos(a)]])
    turn_x = np.array([[1, 0, 0], [0, math.cos(b), -math.sin(b)], [0, math.sin(b), math.cos(b)]])
    return dirs @ (turn_y @ turn_x).T


def shade(normals: np.ndarray, to_eye: np.ndarray, color: tuple[float, float, float], material: Material,
          lighting: Lighting, yaw: float = 0.0, pitch: float = 0.0) -> np.ndarray:
    """Colours (N x 3, 0-1) for surface points: their unit normals and unit directions toward the
    eye, in view space. The same sums as the viewer's fragment shader (ui.viewer)."""
    n = normals
    base = np.asarray(color, dtype=np.float64)
    if material.normals:
        return np.clip(n * 0.5 + 0.5, 0.0, 1.0)
    hemi = n[:, 1:2] * 0.5 + 0.5
    if material.bands:
        hemi = np.full_like(hemi, 0.5)  # (toon: one even ambient, so the light comes in flat steps)
    ambient = np.asarray(lighting.ground) * (1 - hemi) + np.asarray(lighting.sky) * hemi
    diffuse = np.zeros_like(n)
    spec = np.zeros_like(n)
    for light_dir, light in zip(light_directions(lighting, yaw, pitch), lighting.lights):
        lit = n @ light_dir
        d = np.maximum((lit + material.wrap) / (1.0 + material.wrap), 0.0)
        if material.bands:
            d = np.floor(d * material.bands + 0.5) / material.bands
        if material.phong:
            reflected = 2 * lit[:, None] * n - light_dir
            s = np.maximum(np.sum(reflected * to_eye, axis=1), 0.0) ** material.shininess
        else:
            half = to_eye + light_dir
            half /= np.maximum(np.linalg.norm(half, axis=1, keepdims=True), 1e-9)
            s = np.maximum(np.sum(n * half, axis=1), 0.0) ** material.shininess
        if material.bands:
            s = (s > 0.5).astype(np.float64)
        s = s * (lit > 0)
        diffuse += d[:, None] * np.asarray(light.color)
        spec += s[:, None] * np.asarray(light.color)
    spec_color = (1 - material.metal) + material.metal * base
    out = base * (ambient + diffuse) * material.diffuse + spec_color * spec * material.specular
    facing = np.maximum(np.sum(n * to_eye, axis=1, keepdims=True), 0.0)
    if material.metal > 0:
        reflected = 2 * facing * n - to_eye
        t = reflected[:, 1:2] * 0.5 + 0.5
        env = np.asarray(ENV_LOW) * (1 - t) + np.asarray(ENV_HIGH) * t
        out += base * env * 0.9 * material.metal
    out += material.rim * (1 - facing) ** 3 * spec_color
    if material.bands:
        out = np.where(facing < 0.22, out * 0.35, out)  # a dark outline at the silhouette
    return np.clip(out, 0.0, 1.0)
