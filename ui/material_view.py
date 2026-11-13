"""A texture set's material on a sphere, cube or plane (core.texture_sets, core.material_maps).

MaterialViewport is the 3D viewer's viewport (camera, lights, HDRIs, backdrop) drawing a shape with the
maps instead of a model: metal / roughness PBR (GGX lights, and under an HDRI its soft light and blurred
reflections), normal maps (green flipped for DirectX), height as real displacement, packed AO /
roughness / metallic maps, emissive, and opacity as a cut-out. MaterialViewerDialog adds the maps' list
(each one's kind can be corrected, or it can be left out), the shape, tiling, a strength slider per kind of
map (this preview only: not sent, not kept), and sending the material to Blender or Maya.
"""

from __future__ import annotations

import ctypes
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np
from OpenGL import GL
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QMatrix4x4
from PySide6.QtOpenGL import QOpenGLShader, QOpenGLShaderProgram
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import environment
from core.material_maps import (
    COLOR_ROLES, SHAPE_NAMES, SHAPES, LoadedMap, Shape, height_depth, height_level, load_maps, make_shape, map_choices,
)
from core.model_look import ENV_HIGH, ENV_LOW, MAX_LIGHTS, light_directions
from core.scanner import AssetItem
from core.texture_sets import ROLE_NAMES, ROLES, TextureSet
from ui import shortcuts
from ui.duotone import set_icon
from ui.model_lighting import LightingControls
from ui.tag_widgets import run_job
from ui.viewer import _ENV_GLSL, _FIELD_OF_VIEW, Model3DViewportWidget

HEIGHT_MAX = 0.04  # the height slider's top: the map's lowest to highest point, as a share of one copy of the map
HEIGHT_START = 0.01
# The Adjust sliders: (kind, label, top, start, steps, value shown, what it does). They change this preview
# only: Send to Blender / Maya sends the maps as they are, and the window starts from these each time.
STRENGTHS = (
    ("color", "Colour", 2.0, 1.0, 200, "{:.2f}\u00d7", "How bright the colour map is (1.00\u00d7: as it is)"),
    ("normal", "Normal", 2.0, 1.0, 200, "{:.2f}\u00d7", "How strong the normal map's detail is (0: smooth)"),
    ("roughness", "Roughness", 2.0, 1.0, 200, "{:.2f}\u00d7", "The roughness times this (lower: shinier)"),
    ("metal", "Metal", 1.0, 1.0, 100, "{:.2f}", "How much of the metal map is used (0: none metal)"),
    ("ao", "AO", 1.0, 1.0, 100, "{:.2f}", "How much the ambient occlusion map darkens the crevices"),
    ("emissive", "Emissive", 10.0, 1.0, 1000, "{:.2f}\u00d7", "How bright the emissive map glows"),
    ("height", "Height", HEIGHT_MAX, HEIGHT_START, 40, "{:.1%}",
     "How deep the height map is: its lowest to highest point, as a share of the map's width (0: flat)"),
)
_GAINS = {"color": "uColorGain", "normal": "uNormalStrength", "roughness": "uRoughGain", "metal": "uMetalGain",
          "ao": "uAoStrength", "emissive": "uEmissiveGain"}
_TEXTURE_ROLES = ("color", "normal", "roughness", "gloss", "metal", "ao", "height", "opacity", "emissive", "packed")
_UNITS = {role: unit for unit, role in enumerate(_TEXTURE_ROLES, start=1)}  # (unit 0: the HDRI)

_VERT = """#version 330 core
layout(location = 0) in vec3 aPos;
layout(location = 1) in vec3 aNormal;
layout(location = 2) in vec2 aUV;
layout(location = 3) in vec4 aTangent;

uniform mat4 uMVP;
uniform mat4 uModelView;
uniform mat3 uNormalMatrix;
uniform vec2 uTiling;
uniform sampler2D uHeightMap;
uniform float uHasHeight;
uniform float uHeightScale;
uniform float uHeightLod;

out vec3 vViewPos;
out vec3 vNormal;
out vec3 vTangent;
out float vSign;
out vec2 vUV;

void main() {
    vec2 uv = aUV * uTiling;
    vec3 p = aPos;
    if (uHasHeight > 0.5) p += aNormal * (textureLod(uHeightMap, uv, uHeightLod).r - 0.5) * uHeightScale;
    vUV = uv;
    vNormal = normalize(uNormalMatrix * aNormal);
    vTangent = normalize(uNormalMatrix * aTangent.xyz);
    vSign = aTangent.w;
    vViewPos = (uModelView * vec4(p, 1.0)).xyz;
    gl_Position = uMVP * vec4(p, 1.0);
}
"""

_FRAG = """#version 330 core
in vec3 vViewPos;
in vec3 vNormal;
in vec3 vTangent;
in float vSign;
in vec2 vUV;

uniform sampler2D uColorMap;
uniform sampler2D uNormalMap;
uniform sampler2D uRoughnessMap;
uniform sampler2D uGlossMap;
uniform sampler2D uMetalMap;
uniform sampler2D uAoMap;
uniform sampler2D uOpacityMap;
uniform sampler2D uEmissiveMap;
uniform sampler2D uPackedMap;
uniform float uHasColor;
uniform float uHasNormal;
uniform float uHasRoughness;
uniform float uHasGloss;
uniform float uHasMetal;
uniform float uHasAo;
uniform float uHasOpacity;
uniform float uHasEmissive;
uniform float uHasPacked;
uniform float uColorAlpha;
uniform float uFlipGreen;
uniform float uColorGain;
uniform float uNormalStrength;
uniform float uRoughGain;
uniform float uMetalGain;
uniform float uAoStrength;
uniform float uEmissiveGain;
uniform ivec3 uPacked;  // which channel of the packed map is AO, roughness, metallic (-1: none)
uniform int uLightCount;
uniform vec3 uLightDir[%(lights)d];
uniform vec3 uLightColor[%(lights)d];
uniform vec3 uSky;
uniform vec3 uGround;
uniform vec3 uEnvLow;
uniform vec3 uEnvHigh;
uniform float uUseEnv;
uniform vec3 uSH[9];
uniform float uEnvMaxLevel;
%(environment)s
out vec4 FragColor;

vec3 irradiance(vec3 n) {
    return uSH[0] * 0.282095 * 3.141593
        + (uSH[1] * n.y + uSH[2] * n.z + uSH[3] * n.x) * 0.488603 * 2.094395
        + (uSH[4] * n.x * n.y + uSH[5] * n.y * n.z + uSH[7] * n.x * n.z) * 1.092548 * 0.785398
        + uSH[6] * 0.315392 * (3.0 * n.z * n.z - 1.0) * 0.785398
        + uSH[8] * 0.546274 * (n.x * n.x - n.y * n.y) * 0.785398;
}

// The split-sum's scale and bias for F0, fitted (Karis, mobile)
vec2 envBRDF(float NoV, float roughness) {
    const vec4 c0 = vec4(-1.0, -0.0275, -0.572, 0.022);
    const vec4 c1 = vec4(1.0, 0.0425, 1.04, -0.04);
    vec4 r = roughness * c0 + c1;
    float a004 = min(r.x * r.x, exp2(-9.28 * NoV)) * r.x + r.y;
    return vec2(-1.04, 1.04) * a004 + r.zw;
}

vec3 directLight(vec3 n, vec3 v, vec3 l, vec3 color, vec3 diffuseColor, vec3 F0, float roughness) {
    float NoL = max(dot(n, l), 0.0);
    if (NoL <= 0.0) return vec3(0.0);
    vec3 h = normalize(l + v);
    float NoH = max(dot(n, h), 0.0), NoV = max(dot(n, v), 1e-4), VoH = max(dot(v, h), 0.0);
    float a = roughness * roughness, a2 = a * a;
    float d = NoH * NoH * (a2 - 1.0) + 1.0;
    float D = a2 / (PI * d * d);
    float k = (roughness + 1.0) * (roughness + 1.0) / 8.0;
    float G = NoL / (NoL * (1.0 - k) + k) * NoV / (NoV * (1.0 - k) + k);
    vec3 F = F0 + (1.0 - F0) * pow(1.0 - VoH, 5.0);
    vec3 specular = D * G * F / max(4.0 * NoL * NoV, 1e-4);
    return (diffuseColor * (1.0 - F) / PI + specular) * color * NoL * PI;
}

void main() {
    vec4 base = uHasColor > 0.5 ? texture(uColorMap, vUV) : vec4(0.72, 0.72, 0.72, 1.0);
    float alpha = uHasOpacity > 0.5 ? texture(uOpacityMap, vUV).r : (uColorAlpha > 0.5 ? base.a : 1.0);
    if (alpha < 0.5) discard;
    float roughness = 0.5, metal = 0.0, ao = 1.0;
    if (uHasPacked > 0.5) {
        vec3 orm = texture(uPackedMap, vUV).rgb;  // ("packed" is a word GLSL keeps for itself)
        if (uPacked.x >= 0) ao = orm[uPacked.x];
        if (uPacked.y >= 0) roughness = orm[uPacked.y];
        if (uPacked.z >= 0) metal = orm[uPacked.z];
    }
    if (uHasRoughness > 0.5) roughness = texture(uRoughnessMap, vUV).r;
    else if (uHasGloss > 0.5) roughness = 1.0 - texture(uGlossMap, vUV).r;
    if (uHasMetal > 0.5) metal = texture(uMetalMap, vUV).r;
    if (uHasAo > 0.5) ao = texture(uAoMap, vUV).r;
    base.rgb *= uColorGain;
    roughness = clamp(roughness * uRoughGain, 0.04, 1.0);
    metal = clamp(metal * uMetalGain, 0.0, 1.0);
    ao = mix(1.0, ao, uAoStrength);

    vec3 n = normalize(vNormal);
    if (!gl_FrontFacing) n = -n;
    if (uHasNormal > 0.5) {
        vec3 t = normalize(vTangent - n * dot(n, vTangent));
        vec3 b = cross(n, t) * vSign;
        vec3 m = texture(uNormalMap, vUV).rgb * 2.0 - 1.0;
        if (uFlipGreen > 0.5) m.g = -m.g;
        m.xy *= uNormalStrength;
        n = normalize(t * m.x + b * m.y + n * max(m.z, 0.0001));
    }
    vec3 v = normalize(-vViewPos);
    float NoV = max(dot(n, v), 1e-4);
    vec3 F0 = mix(vec3(0.04), base.rgb, metal);
    vec3 diffuseColor = base.rgb * (1.0 - metal);
    vec2 brdf = envBRDF(NoV, roughness);
    vec3 color = vec3(0.0);
    for (int i = 0; i < %(lights)d; i++) {
        if (i >= uLightCount) break;
        color += directLight(n, v, normalize(uLightDir[i]), uLightColor[i], diffuseColor, F0, roughness);
    }
    vec3 r = reflect(-v, n);
    if (uUseEnv > 0.5) {
        vec3 diffuse = max(irradiance(uViewToEnv * n) / PI, vec3(0.0)) * diffuseColor;
        vec3 reflected = textureLod(uEnv, toEquirect(uViewToEnv * r), roughness * uEnvMaxLevel).rgb;
        color += (diffuse + reflected * (F0 * brdf.x + brdf.y)) * ao;
        color *= uExposure;
    } else {
        vec3 ambient = mix(uGround, uSky, n.y * 0.5 + 0.5);
        vec3 reflected = mix(uEnvLow, uEnvHigh, r.y * 0.5 + 0.5);
        color += (ambient * diffuseColor + reflected * (F0 * brdf.x + brdf.y)) * ao;
    }
    if (uHasEmissive > 0.5) color += texture(uEmissiveMap, vUV).rgb * uEmissiveGain;
    FragColor = vec4(pow(aces(color), vec3(1.0 / 2.2)), 1.0);
}
""" % {"lights": MAX_LIGHTS, "environment": _ENV_GLSL}

_SAMPLERS = {"color": "uColorMap", "normal": "uNormalMap", "roughness": "uRoughnessMap", "gloss": "uGlossMap",
             "metal": "uMetalMap", "ao": "uAoMap", "height": "uHeightMap", "opacity": "uOpacityMap",
             "emissive": "uEmissiveMap", "packed": "uPackedMap"}
_FLAGS = {"color": "uHasColor", "normal": "uHasNormal", "roughness": "uHasRoughness", "gloss": "uHasGloss",
          "metal": "uHasMetal", "ao": "uHasAo", "height": "uHasHeight", "opacity": "uHasOpacity",
          "emissive": "uHasEmissive", "packed": "uHasPacked"}


class MaterialViewport(Model3DViewportWidget):
    """The 3D viewer's viewport, drawing a shape with a texture set's maps (set_maps)."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(None, parent)
        self.show_grid = False
        self.shape_name = "sphere"
        self.render_mode = SHAPE_NAMES[self.shape_name]  # (the corner badge says which shape)
        self.tiling = 1
        self.height_scale = HEIGHT_START
        self.strengths = {kind: start for kind, _label, _top, start, *_rest in STRENGTHS if kind in _GAINS}
        self.flip_green = False
        self.packed_channels: tuple[str, str, str] = ("ao", "roughness", "metal")
        self.maps: dict[str, LoadedMap] = {}
        self._shape: Optional[Shape] = None
        self._textures: dict[str, int] = {}
        self._buffers: list[int] = []
        self._material_vao: Optional[int] = None
        self._material_count = 0
        self._material_program: Optional[QOpenGLShaderProgram] = None
        self._mu: dict[str, int] = {}
        self._shape_dirty = True
        self._maps_dirty = True
        self.set_shape("sphere")

    # ------------------ what's shown ------------------
    def set_shape(self, name: str) -> None:
        self.shape_name = name
        self.render_mode = SHAPE_NAMES[name]
        self._shape = make_shape(name)
        self.face_count = len(self._shape.indices) // 3
        self.vertex_count = 0  # (the base viewport draws no model of its own)
        self._shape_dirty = True
        self.update()

    def set_maps(self, maps: dict[str, LoadedMap], flip_green: bool, packed_channels: Sequence[str]) -> None:
        self.maps = dict(maps)
        self.flip_green = flip_green
        self.packed_channels = tuple(packed_channels)  # type: ignore[assignment]
        self._maps_dirty = True
        self.status_text = ""
        self.update()

    def set_tiling(self, tiling: int) -> None:
        self.tiling = max(1, int(tiling))
        self.update()

    def set_height_scale(self, scale: float) -> None:
        self.height_scale = scale
        self.update()

    def set_strength(self, kind: str, value: float) -> None:
        """One of the Adjust sliders (STRENGTHS): "height", or a kind's gain."""
        if kind == "height":
            self.height_scale = value
        else:
            self.strengths[kind] = value
        self.update()

    def set_flip_green(self, flip: bool) -> None:
        self.flip_green = flip
        self.update()

    def keyPressEvent(self, event) -> None:
        if shortcuts.matches("viewer3d.wireframe", event):
            return  # (a material is always shaded)
        super().keyPressEvent(event)

    # ------------------ OpenGL ------------------
    def initializeGL(self) -> None:
        super().initializeGL()
        program = QOpenGLShaderProgram(self)
        compiled = (program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, _VERT)
                    and program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, _FRAG) and program.link())
        if not compiled:
            from core import crash_log

            crash_log.log_error(f"The material preview's shaders didn't compile: {program.log()}")
            self.status_text = "This graphics card can't show the material preview."
            return
        self._material_program = program
        names = ["uMVP", "uModelView", "uNormalMatrix", "uTiling", "uHeightScale", "uHeightLod", "uColorAlpha", "uFlipGreen", "uPacked",
                 "uLightCount", "uLightDir", "uLightColor", "uSky", "uGround", "uEnvLow", "uEnvHigh", "uUseEnv", "uSH",
                 "uEnvMaxLevel", "uEnv", "uViewToEnv", "uExposure", *_SAMPLERS.values(), *_FLAGS.values(), *_GAINS.values()]
        self._mu = {name: program.uniformLocation(name) for name in names}
        self._material_vao, self._buffers, self._textures = None, [], {}  # (a new context: the old ones went with it)
        self._shape_dirty = self._maps_dirty = True

    def _upload_shape(self) -> None:
        self._shape_dirty = False
        if self._buffers:
            GL.glDeleteBuffers(len(self._buffers), self._buffers)
        if self._material_vao is not None:
            GL.glDeleteVertexArrays(1, [self._material_vao])
        shape = self._shape
        self._material_vao = GL.glGenVertexArrays(1)
        GL.glBindVertexArray(self._material_vao)
        self._buffers = []
        for location, (array, width) in enumerate(((shape.positions, 3), (shape.normals, 3), (shape.uvs, 2), (shape.tangents, 4))):
            buffer = GL.glGenBuffers(1)
            data = np.ascontiguousarray(array, dtype=np.float32)
            GL.glBindBuffer(GL.GL_ARRAY_BUFFER, buffer)
            GL.glBufferData(GL.GL_ARRAY_BUFFER, data.nbytes, data, GL.GL_STATIC_DRAW)
            GL.glEnableVertexAttribArray(location)
            GL.glVertexAttribPointer(location, width, GL.GL_FLOAT, GL.GL_FALSE, 0, ctypes.c_void_p(0))
            self._buffers.append(buffer)
        indices = np.ascontiguousarray(shape.indices, dtype=np.uint32)
        element = GL.glGenBuffers(1)
        GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, element)
        GL.glBufferData(GL.GL_ELEMENT_ARRAY_BUFFER, indices.nbytes, indices, GL.GL_STATIC_DRAW)
        self._buffers.append(element)
        self._material_count = indices.size
        GL.glBindVertexArray(0)

    def _upload_maps(self) -> None:
        self._maps_dirty = False
        if self._textures:
            GL.glDeleteTextures(len(self._textures), list(self._textures.values()))
        self._textures = {}
        anisotropy = 0.0
        try:
            anisotropy = float(GL.glGetFloatv(0x84FF))  # (MAX_TEXTURE_MAX_ANISOTROPY)
        except Exception:  # pylint: disable=broad-except
            pass
        for role, loaded in self.maps.items():
            if role not in _SAMPLERS:
                continue
            texture = GL.glGenTextures(1)
            GL.glBindTexture(GL.GL_TEXTURE_2D, texture)
            GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
            pixels = np.ascontiguousarray(loaded.pixels)
            height, width = pixels.shape[:2]
            if pixels.ndim == 3:
                internal = GL.GL_SRGB8_ALPHA8 if role in COLOR_ROLES else GL.GL_RGBA8
                GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, internal, width, height, 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, pixels)
            else:
                GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_R16, width, height, 0, GL.GL_RED, GL.GL_UNSIGNED_SHORT, pixels)
            GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 4)
            GL.glGenerateMipmap(GL.GL_TEXTURE_2D)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR_MIPMAP_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_REPEAT)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_REPEAT)
            if anisotropy > 1.0:
                GL.glTexParameterf(GL.GL_TEXTURE_2D, 0x84FE, min(8.0, anisotropy))  # (TEXTURE_MAX_ANISOTROPY)
            self._textures[role] = texture
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)

    def _draw_scene(self, w: int, h: int, grid: bool) -> None:
        super()._draw_scene(w, h, grid)  # (the backdrop and the grid: the base has no model to draw)
        if self._material_program is None or self._shape is None:
            return
        if self._shape_dirty:
            self._upload_shape()
        if self._maps_dirty:
            self._upload_maps()
        aspect = float(w) / float(max(h, 1))
        proj = QMatrix4x4()
        proj.perspective(_FIELD_OF_VIEW, aspect, 0.05, 100.0)
        view = QMatrix4x4()
        view.translate(self.pan_x, self.pan_y, -3.2 / max(self.zoom, 0.05))
        view.rotate(self.rot_x, 1.0, 0.0, 0.0)
        view.rotate(self.rot_y, 0.0, 1.0, 0.0)
        program, u = self._material_program, self._mu
        program.bind()
        program.setUniformValue("uMVP", proj * view)
        program.setUniformValue("uModelView", view)
        program.setUniformValue("uNormalMatrix", view.normalMatrix())
        GL.glUniform2f(u["uTiling"], float(self.tiling), float(self.tiling))
        GL.glUniform1f(u["uHeightScale"], height_depth(self.height_scale, self.tiling, self.shape_name))
        height = self.maps.get("height")
        side = max(height.pixels.shape[:2]) if height is not None else 1
        GL.glUniform1f(u["uHeightLod"], height_level(side, self.tiling, self.shape_name))
        GL.glUniform1f(u["uFlipGreen"], 1.0 if self.flip_green else 0.0)
        for kind, uniform in _GAINS.items():
            GL.glUniform1f(u[uniform], self.strengths[kind])
        color = self.maps.get("color")
        GL.glUniform1f(u["uColorAlpha"], 1.0 if color is not None and color.has_alpha else 0.0)
        packed = [self.packed_channels.index(kind) if kind in self.packed_channels else -1 for kind in ("ao", "roughness", "metal")]
        GL.glUniform3i(u["uPacked"], *packed)
        for role, sampler in _SAMPLERS.items():
            unit = _UNITS[role]
            GL.glActiveTexture(GL.GL_TEXTURE0 + unit)
            GL.glBindTexture(GL.GL_TEXTURE_2D, self._textures.get(role, 0))
            GL.glUniform1i(u[sampler], unit)
            GL.glUniform1f(u[_FLAGS[role]], 1.0 if role in self._textures else 0.0)
        self._set_lights()
        GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_FILL)
        GL.glBindVertexArray(self._material_vao)
        GL.glDrawElements(GL.GL_TRIANGLES, self._material_count, GL.GL_UNSIGNED_INT, ctypes.c_void_p(0))
        GL.glBindVertexArray(0)
        for unit in range(len(_SAMPLERS) + 1):
            GL.glActiveTexture(GL.GL_TEXTURE0 + unit)
            GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        program.release()

    def _set_lights(self) -> None:
        """The viewer's lights (or the HDRI's), as the model viewer has them."""
        u = self._mu
        dirs = np.zeros((MAX_LIGHTS, 3), dtype=np.float32)
        colors = np.zeros((MAX_LIGHTS, 3), dtype=np.float32)
        lighting = self.lighting()
        use_env = self._env_ready()
        GL.glUniform1f(u["uUseEnv"], 1.0 if use_env else 0.0)
        if use_env:
            env = self.environment
            count = len(env.lights)
            if count:
                dirs[:count] = np.array([light.direction for light in env.lights]) @ self._view_to_env()
                colors[:count] = [light.color for light in env.lights]
            self._set_env_uniforms(self._material_program, u)
            GL.glUniform3fv(u["uSH"], 9, env.harmonics)
            GL.glUniform1f(u["uEnvMaxLevel"], max(1.0, float(np.log2(env.pixels.shape[1])) - 2.0))
        else:
            count = len(lighting.lights)
            dirs[:count] = light_directions(lighting, self.light_yaw, self.light_pitch)
            colors[:count] = [light.color for light in lighting.lights]
            GL.glUniform1i(u["uEnv"], 0)
        GL.glUniform1i(u["uLightCount"], count)
        GL.glUniform3fv(u["uLightDir"], MAX_LIGHTS, dirs)
        GL.glUniform3fv(u["uLightColor"], MAX_LIGHTS, colors)
        GL.glUniform3f(u["uSky"], *lighting.sky)
        GL.glUniform3f(u["uGround"], *lighting.ground)
        GL.glUniform3f(u["uEnvLow"], *ENV_LOW)
        GL.glUniform3f(u["uEnvHigh"], *ENV_HIGH)


# ----------------------------------------------------------------------
# The window
# ----------------------------------------------------------------------
class StrengthSlider(QSlider):
    """A slider that goes back to where it started on a double-click (material.reset_slider)."""

    def __init__(self, steps: int, start: int, parent: Optional[QWidget] = None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setRange(0, steps)
        self.start = start
        self.setValue(start)

    def mouseDoubleClickEvent(self, event) -> None:
        self.setValue(self.start)
        event.accept()


class MaterialViewerDialog(QDialog):
    """A texture set as a material. send(app id, the maps used {role: TextureMap}, flip green) sends it to
    Blender or Maya (None: not offered); reveal() shows its folder."""

    def __init__(self, texture_set: TextureSet, parent: Optional[QWidget] = None,
                 hdri_assets: Optional[Callable[[], Sequence[AssetItem]]] = None,
                 send: Optional[Callable[[str, dict, bool], None]] = None, reveal: Optional[Callable[[], None]] = None):
        super().__init__(parent)
        self.texture_set = texture_set
        self._send_to = send
        self._flip_guessed = False
        self._roles: dict[str, str] = {}  # kinds corrected by hand {str(path): role}
        self._off: set[str] = set()  # files left out
        self._load_token = 0
        self._closed = False
        self.setWindowTitle(f"{texture_set.name} - Material")
        self.resize(1200, 760)
        root = QHBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        left = QVBoxLayout()
        self.viewport = MaterialViewport(self)
        self.viewport.status_text = "Loading the maps..."
        toolbar = QWidget()
        toolbar.setObjectName("viewerToolbar")
        rows = QVBoxLayout(toolbar)
        rows.setContentsMargins(8, 4, 8, 4)
        rows.setSpacing(4)
        top, bottom = QHBoxLayout(), QHBoxLayout()
        top.setSpacing(8)
        bottom.setSpacing(12)
        rows.addLayout(top)
        rows.addLayout(bottom)

        self.cmb_shape = QComboBox()
        for name in SHAPES:
            self.cmb_shape.addItem(SHAPE_NAMES[name], name)
        self.cmb_shape.setToolTip("The shape the material is shown on")
        self.cmb_shape.currentIndexChanged.connect(lambda _i: self.viewport.set_shape(self.cmb_shape.currentData()))
        top.addWidget(self.cmb_shape)

        self.cmb_lighting = QComboBox()
        self._lighting_details = QWidget(toolbar)
        self.lighting_controls = LightingControls(self.viewport, self.cmb_lighting, self._lighting_details, hdri_assets)
        top.addWidget(self.cmb_lighting)

        self.btn_drag_lights = QPushButton("Turn Lights")
        set_icon(self.btn_drag_lights, "light")
        self.btn_drag_lights.setCheckable(True)
        self.btn_drag_lights.setToolTip("On: dragging turns the lights or the HDRI. Off: it turns the shape (L; or Shift+drag)")
        self.btn_drag_lights.toggled.connect(self.viewport.set_drag_lights)
        self.viewport.drag_lights_changed.connect(self.btn_drag_lights.setChecked)
        top.addWidget(self.btn_drag_lights)

        self.btn_turntable = QPushButton("Auto Rotate")
        set_icon(self.btn_turntable, "play")
        self.btn_turntable.setCheckable(True)
        self.btn_turntable.setToolTip("Spin the shape slowly")
        self.btn_turntable.toggled.connect(self._on_turntable)
        top.addWidget(self.btn_turntable)

        btn_reset = QPushButton("Reset View")
        set_icon(btn_reset, "refresh")
        btn_reset.setToolTip("Reset the camera and the lights (R resets the camera)")
        btn_reset.clicked.connect(self.viewport.reset_camera)
        btn_reset.clicked.connect(self.viewport.reset_lights)
        top.addWidget(btn_reset)
        top.addStretch(1)

        bottom.addWidget(QLabel("Tiling"))
        self.spin_tiling = QSpinBox()
        self.spin_tiling.setRange(1, 16)
        self.spin_tiling.setToolTip("How many times the maps repeat across the shape")
        self.spin_tiling.valueChanged.connect(self.viewport.set_tiling)
        bottom.addWidget(self.spin_tiling)
        self.chk_flip = QCheckBox("Flip Green (DirectX)", toolbar)
        self.chk_flip.setToolTip("The normal map's green channel the other way up: DirectX (Unreal) normal maps")
        self.chk_flip.toggled.connect(self.viewport.set_flip_green)
        bottom.addWidget(self.chk_flip)
        bottom.addStretch(1)
        rows.addWidget(self._lighting_details)

        left.addWidget(toolbar)
        left.addWidget(self.viewport, 1)
        root.addLayout(left, 1)

        side = QVBoxLayout()
        title = QLabel(texture_set.name)
        title.setObjectName("viewerTitle")
        title.setWordWrap(True)
        side.addWidget(title)
        where = texture_set.assets[0]
        place = where.archive.archive.name if where.in_archive else str(Path(where.path).parent)
        info = QLabel(f"{len(texture_set.maps)} files" + (f", UDIM tiles {texture_set.tiles[0]}-{texture_set.tiles[-1]}"
                                                         f" (the first is shown)" if texture_set.tiles else "") + f"\n{place}")
        info.setObjectName("mutedLabel")
        info.setWordWrap(True)
        side.addWidget(info)
        hint = QLabel("Untick a map to leave it out, or change what it is.")
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        side.addWidget(hint)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Map", "Used as"])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 210)
        self.tree.setMinimumWidth(380)
        self.tree.itemChanged.connect(self._on_item_changed)
        side.addWidget(self.tree, 1)
        self._fill_maps()
        side.addWidget(self._build_strengths())

        buttons = QVBoxLayout()
        self.btn_blender = QPushButton("Send to Blender as Material")
        set_icon(self.btn_blender, "open_in")
        self.btn_blender.setToolTip("Make a Principled BSDF material with these maps in the Blender that's open, on its selected objects")
        self.btn_blender.clicked.connect(lambda: self.send("blender"))
        self.btn_maya = QPushButton("Send to Maya as Material")
        set_icon(self.btn_maya, "open_in")
        self.btn_maya.setToolTip("Make an Arnold aiStandardSurface (or standardSurface: Settings > Apps) in the Maya that's open, on its selected objects")
        self.btn_maya.clicked.connect(lambda: self.send("maya"))
        for button in (self.btn_blender, self.btn_maya):
            button.setVisible(send is not None)
            buttons.addWidget(button)
        if reveal is not None:
            btn_reveal = QPushButton("Show in File Explorer")
            btn_reveal.setToolTip("Show the maps' folder (or archive) in File Explorer")
            btn_reveal.clicked.connect(reveal)
            buttons.addWidget(btn_reveal)
        btn_close = QPushButton("Close")
        btn_close.setToolTip("Close the material (Esc)")
        btn_close.clicked.connect(self.accept)
        buttons.addWidget(btn_close)
        side.addLayout(buttons)
        side_widget = QWidget()
        side_widget.setLayout(side)
        side_widget.setFixedWidth(400)
        root.addWidget(side_widget)
        QTimer.singleShot(0, self._load)

    # ------------------ the Adjust sliders ------------------
    def _build_strengths(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(4)
        title = QLabel("Adjust")
        title.setObjectName("viewerTitle")
        layout.addWidget(title)
        hint = QLabel("For this preview only: the maps are sent to Blender / Maya as they are. Double-click a slider to reset it.")
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        self.strength_rows: dict[str, tuple[QLabel, StrengthSlider, QLabel]] = {}
        for row, (kind, label, top, start, steps, shown, what) in enumerate(STRENGTHS):
            name = QLabel(label)
            slider = StrengthSlider(steps, round(start / top * steps))
            slider.setToolTip(f"{what}. Double-click: back to {shown.format(start)}")
            value = QLabel(shown.format(start))
            value.setMinimumWidth(52)
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            slider.valueChanged.connect(lambda step, k=kind: self._on_strength(k, step))
            grid.addWidget(name, row, 0)
            grid.addWidget(slider, row, 1)
            grid.addWidget(value, row, 2)
            self.strength_rows[kind] = (name, slider, value)
            for widget in (name, slider, value):
                widget.setVisible(False)  # (shown once the maps are loaded: only the kinds there are)
        layout.addLayout(grid)
        self.lbl_height, self.slider_height, _value = self.strength_rows["height"]
        return box

    def strength(self, kind: str) -> float:
        _name, slider, _value = self.strength_rows[kind]
        top = next(entry[2] for entry in STRENGTHS if entry[0] == kind)
        return top * slider.value() / slider.maximum()

    def _on_strength(self, kind: str, _step: int) -> None:
        value = self.strength(kind)
        shown = next(entry[5] for entry in STRENGTHS if entry[0] == kind)
        self.strength_rows[kind][2].setText(shown.format(value))
        self.viewport.set_strength(kind, value)

    def _show_strengths(self, maps: dict[str, LoadedMap], packed: Sequence[str]) -> None:
        """A slider for each kind of map shown (a packed map's channels count)."""
        there = set(maps) | (set(packed) if "packed" in maps else set())
        if "gloss" in there:
            there.add("roughness")
        for kind, widgets in self.strength_rows.items():
            for widget in widgets:
                widget.setVisible(kind in there)

    # ------------------ the maps ------------------
    def _fill_maps(self) -> None:
        """Build the list once. Later changes go through _mark_maps, which keeps the rows: a row must
        never be deleted from its own itemChanged (a tick box being clicked): that crashed the app."""
        self.tree.blockSignals(True)
        self.tree.clear()
        order = {role: n for n, role in enumerate(ROLES)}
        for texture_map in sorted(self.texture_set.maps, key=lambda m: (order[m.role], m.tile or 0, m.asset.name.lower())):
            key = str(texture_map.asset.path)
            item = QTreeWidgetItem(self.tree, [texture_map.asset.name, ""])
            item.setData(0, Qt.ItemDataRole.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Unchecked if key in self._off else Qt.CheckState.Checked)
            box = QComboBox(self.tree)
            for role in ROLES:
                box.addItem(ROLE_NAMES[role], role)
            box.setCurrentIndex(box.findData(self._roles.get(key, texture_map.role)))
            box.setToolTip("What this map is (guessed from its name)")
            box.setFixedHeight(26)
            item.setSizeHint(0, QSize(0, 32))
            box.currentIndexChanged.connect(lambda _i, k=key, b=box: self._on_role_changed(k, b.currentData()))
            self.tree.setItemWidget(item, 1, box)
        self.tree.blockSignals(False)
        self._mark_maps()

    def _mark_maps(self) -> None:
        """Grey out the ticked maps that aren't shown (another of the kind is used, or a later tile)."""
        chosen = {str(m.asset.path) for m in self.choices().values()}
        dim = self.tree.palette().placeholderText()
        normal = self.tree.palette().text()
        self.tree.blockSignals(True)  # (a colour or tooltip change sends itemChanged too)
        try:
            for n in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(n)
                key = item.data(0, Qt.ItemDataRole.UserRole)
                used = key in chosen
                name = item.text(0)
                item.setToolTip(0, name + ("" if used else "\n(not shown: another map of this kind is used, or a later tile)"))
                item.setForeground(0, dim if not used and key not in self._off else normal)
        finally:
            self.tree.blockSignals(False)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if item.checkState(0) == Qt.CheckState.Checked:
            self._off.discard(key)
        else:
            self._off.add(key)
        self._mark_maps()
        self._load()

    def _on_role_changed(self, key: str, role: str) -> None:
        self._roles[key] = role
        self._mark_maps()
        self._load()

    def choices(self):
        return map_choices(self.texture_set, self._roles, frozenset(self._off))

    def _load(self) -> None:
        """Read the maps in the background (the newest request wins)."""
        self._load_token += 1
        token, choices = self._load_token, self.choices()
        packed = choices.get("packed")

        def done(maps) -> None:
            if self._closed or token != self._load_token:
                return
            if not self._flip_guessed:  # (guessed once from the names: then it's the box's)
                self._flip_guessed = True
                self.chk_flip.setChecked(self.texture_set.flip_green())
            channels = packed.channels if packed else ("ao", "roughness", "metal")
            self.viewport.set_maps(maps, self.chk_flip.isChecked(), channels)
            self._show_strengths(maps, channels)
            self.chk_flip.setEnabled("normal" in maps)
            missing = len(choices) - len(maps)
            if not maps:
                self.viewport.set_status("The maps couldn't be read.")
            elif missing:
                self.viewport.set_notice(f"{missing} map{'s' if missing != 1 else ''} couldn't be read.")

        def failed(message: str) -> None:
            if not self._closed and token == self._load_token:
                self.viewport.set_status(f"The maps couldn't be read: {message}")

        run_job(lambda: load_maps(choices), done, failed)

    # ------------------ the rest ------------------
    def _on_turntable(self, on: bool) -> None:
        self.viewport.toggle_turntable(on)
        self.btn_turntable.setText("Pause" if on else "Auto Rotate")
        set_icon(self.btn_turntable, "pause" if on else "play")

    def send(self, app_id: str) -> None:
        if self._send_to is not None:
            self._send_to(app_id, self.choices(), self.chk_flip.isChecked())

    def done(self, result: int) -> None:
        self._closed = True
        self.viewport.toggle_turntable(False)
        super().done(result)
