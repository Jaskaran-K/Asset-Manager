"""Lightweight interactive viewer for 3D objects, textures, documents, and videos.

Includes:
- Model3DViewportWidget: 3D orbit viewport for .obj, .gltf, .glb, .fbx with shaded/wireframe modes.
- TextureViewerWidget: High-resolution texture/image viewer with interactive zoom, pan, and checkerboard.
- PDFs: ui.pdf_viewer.PdfViewer (pages, zoom, contents, thumbnails, links).
- VideoPlayer (ui.video_player): in-app video playback with proxy fallback.
- AssetViewerDialog: Unified modal viewer dialog hosting the appropriate view.
"""

from __future__ import annotations

import ctypes
import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np
import shiboken6
from OpenGL import GL
from PySide6.QtCore import (
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QDesktopServices,
    QFont,
    QGuiApplication,
    QIcon,
    QImage,
    QMatrix4x4,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QPolygonF,
    QVector3D,
)
from PySide6.QtOpenGL import QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat, QOpenGLShader, QOpenGLShaderProgram
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QStackedWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from core import alphas, app_icons, apps, archives, environment
from core.full_image import LoadedImage, load_full_image
from core.model_loader import Mesh3D, load_3d_model
from core.model_uvs import UvLayout, read_uvs, tiles_text
from core.obj_fast import LoadCancelled
from core.render_scene import UNRENDERABLE
from core.model_look import ENV_HIGH, ENV_LOW, LIGHTINGS, MATERIALS, MAX_LIGHTS, Lighting, Material, light_directions, with_roughness
from core.settings import ALPHA_PLANE_SIDES, TILE_GRIDS, get_model_view, set_model_view
from core.scanner import AssetItem
from core.tag_repository import Tag
from core.thumbnail_manager import load_app_file_picture
from ui import file_actions, shortcuts
from ui.archive_jobs import UnpackJob, unpack_in_background
from ui.duotone import set_icon
from ui.model_lighting import LightingControls, RoughnessBar
from ui.render_window import RenderWindow
from ui.uv_view import UvViewWidget
from ui.pdf_viewer import PdfViewer
from ui.tag_widgets import run_job, tag_chip_cloud
from ui.video_player import VideoPlayer, control_icon


# ----------------------------------------------------------------------
# 3D Model Viewport (Hardware-Accelerated OpenGL)
# ----------------------------------------------------------------------
_FIELD_OF_VIEW = 45.0  # degrees, top to bottom

_MESH_VERT_SHADER = """#version 330 core
layout(location = 0) in vec3 aPos;
layout(location = 1) in vec3 aNormal;

uniform mat4 uMVP;
uniform mat4 uModelView;
uniform mat3 uNormalMatrix;

out vec3 vNormal;
out vec3 vViewPos;

void main() {
    vNormal = normalize(uNormalMatrix * aNormal);
    vViewPos = (uModelView * vec4(aPos, 1.0)).xyz;
    gl_Position = uMVP * vec4(aPos, 1.0);
}
"""

# What the model and the backdrop share under an HDRI. The panorama's look-ups and tone mapping:
# textureLod always, since a seam in the panorama would otherwise show as a line of the blurriest mipmap.
_ENV_GLSL = """
uniform sampler2D uEnv;
uniform mat3 uViewToEnv;
uniform float uExposure;
const float PI = 3.14159265;

vec2 toEquirect(vec3 d) {
    return vec2(atan(d.x, -d.z) / (2.0 * PI) + 0.5, acos(clamp(d.y, -1.0, 1.0)) / PI);
}

vec3 aces(vec3 x) {
    return clamp((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14), 0.0, 1.0);
}
"""

# Lighting in view space (lights stay put while the model turns). The same sums as
# core.model_look.shade, which renders thumbnails: keep the two alike. (An HDRI has its own
# sums, and thumbnails don't use it.)
_MESH_FRAG_SHADER = """#version 330 core
in vec3 vNormal;
in vec3 vViewPos;

uniform vec3 uColor;
uniform float uUnlit;
uniform int uLightCount;
uniform vec3 uLightDir[%(lights)d];
uniform vec3 uLightColor[%(lights)d];
uniform vec3 uSky;
uniform vec3 uGround;
uniform vec3 uEnvLow;
uniform vec3 uEnvHigh;
uniform float uDiffuse;
uniform float uWrap;
uniform float uSpecular;
uniform float uShininess;
uniform float uPhong;
uniform float uMetal;
uniform float uRim;
uniform float uBands;
uniform float uShowNormals;
uniform float uUseEnv;
uniform vec3 uSH[9];
uniform float uEnvLevel;
uniform float uEnvSpread;
uniform float uRoughness;
%(environment)s
out vec4 FragColor;

vec3 irradiance(vec3 n) {
    return uSH[0] * 0.282095 * 3.141593
        + (uSH[1] * n.y + uSH[2] * n.z + uSH[3] * n.x) * 0.488603 * 2.094395
        + (uSH[4] * n.x * n.y + uSH[5] * n.y * n.z + uSH[7] * n.x * n.z) * 1.092548 * 0.785398
        + uSH[6] * 0.315392 * (3.0 * n.z * n.z - 1.0) * 0.785398
        + uSH[8] * 0.546274 * (n.x * n.x - n.y * n.y) * 0.785398;
}

// Reflections: five looks round the mirror direction, as one look at a small mipmap shows its square pixels.
vec3 blurredReflection(vec3 r) {
    vec3 side = normalize(cross(r, abs(r.y) < 0.99 ? vec3(0.0, 1.0, 0.0) : vec3(1.0, 0.0, 0.0)));
    vec3 other = cross(r, side);
    vec3 sum = textureLod(uEnv, toEquirect(r), uEnvLevel).rgb;
    sum += textureLod(uEnv, toEquirect(normalize(r + side * uEnvSpread)), uEnvLevel).rgb;
    sum += textureLod(uEnv, toEquirect(normalize(r - side * uEnvSpread)), uEnvLevel).rgb;
    sum += textureLod(uEnv, toEquirect(normalize(r + other * uEnvSpread)), uEnvLevel).rgb;
    sum += textureLod(uEnv, toEquirect(normalize(r - other * uEnvSpread)), uEnvLevel).rgb;
    return sum / 5.0;
}

// Under an HDRI (core.environment): in linear light, with the panorama's soft light and its lights.
// Highlights are in the reflections, which hold the sun too, so the lights only add their diffuse.
vec3 environmentLook(vec3 n, vec3 v) {
    vec3 base = pow(uColor, vec3(2.2));
    vec3 light = max(irradiance(uViewToEnv * n) / PI, vec3(0.0));
    float glints = 0.0;
    for (int i = 0; i < %(lights)d; i++) {
        if (i >= uLightCount) break;
        vec3 l = normalize(uLightDir[i]);
        float lit = dot(n, l);
        light += uLightColor[i] * max((lit + uWrap) / (1.0 + uWrap), 0.0);
        if (lit > 0.0 && pow(max(dot(n, normalize(l + v)), 0.0), uShininess) > 0.5) glints = 1.0;
    }
    float facing = max(dot(n, v), 0.0);
    if (uBands > 0.5) {  // Toon: the same light, in flat steps, on the exposed brightness
        float level = dot(light, vec3(0.2126, 0.7152, 0.0722)) * uExposure;
        float stepped = max(floor(min(level, 1.0) * uBands + 0.5) / uBands, 0.5 / uBands);
        vec3 tint = light / max(level / uExposure, 0.0001);  // (the light's colour, at brightness 1)
        vec3 toon = aces(base * tint * stepped + glints * uSpecular);
        return pow(toon, vec3(1.0 / 2.2)) * (facing < 0.22 ? 0.35 : 1.0);
    }
    vec3 reflected = blurredReflection(uViewToEnv * reflect(-v, n));
    float head_on = 0.04 + 0.12 * uSpecular;  // (a little more than physical: a viewer should show what a material does)
    float fresnel = head_on + (1.0 - head_on) * pow(1.0 - facing, 5.0);
    vec3 color = base * light * uDiffuse + mix(vec3(fresnel), base, uMetal) * reflected * uSpecular;
    return pow(aces(color * uExposure), vec3(1.0 / 2.2));
}

void main() {
    if (uUnlit > 0.5) {
        FragColor = vec4(uColor, 1.0);
        return;
    }
    vec3 n = normalize(vNormal);
    if (!gl_FrontFacing) n = -n;
    if (uShowNormals > 0.5) {
        FragColor = vec4(n * 0.5 + 0.5, 1.0);
        return;
    }
    vec3 v = normalize(-vViewPos);
    if (uUseEnv > 0.5) {
        FragColor = vec4(environmentLook(n, v), 1.0);
        return;
    }
    float hemi = uBands > 0.5 ? 0.5 : n.y * 0.5 + 0.5;
    vec3 ambient = mix(uGround, uSky, hemi);
    vec3 diffuse = vec3(0.0);
    vec3 spec = vec3(0.0);
    for (int i = 0; i < %(lights)d; i++) {
        if (i >= uLightCount) break;
        vec3 l = normalize(uLightDir[i]);
        float lit = dot(n, l);
        float d = max((lit + uWrap) / (1.0 + uWrap), 0.0);
        if (uBands > 0.5) d = floor(d * uBands + 0.5) / uBands;
        float s = uPhong > 0.5
            ? pow(max(dot(reflect(-l, n), v), 0.0), uShininess)
            : pow(max(dot(n, normalize(l + v)), 0.0), uShininess);
        if (uBands > 0.5) s = s > 0.5 ? 1.0 : 0.0;
        if (lit <= 0.0) s = 0.0;
        diffuse += uLightColor[i] * d;
        spec += uLightColor[i] * s;
    }
    vec3 specColor = mix(vec3(1.0), uColor, uMetal);
    vec3 color = uColor * (ambient + diffuse) * uDiffuse + specColor * spec * uSpecular;
    float facing = max(dot(n, v), 0.0);
    if (uMetal > 0.0) {
        vec3 r = normalize(mix(reflect(-v, n), n, uRoughness));
        vec3 env = mix(uEnvLow, uEnvHigh, r.y * 0.5 + 0.5);
        color += uColor * env * 0.9 * uMetal;
    }
    color += uRim * pow(1.0 - facing, 3.0) * specColor;
    if (uBands > 0.5 && facing < 0.22) color *= 0.35;
    FragColor = vec4(clamp(color, 0.0, 1.0), 1.0);
}
""" % {"lights": MAX_LIGHTS, "environment": _ENV_GLSL}

# The HDRI behind everything: a triangle over the whole window, looked up by the view direction
_BACKDROP_VERT_SHADER = """#version 330 core
out vec2 vScreen;

void main() {
    vScreen = vec2(gl_VertexID == 1 ? 3.0 : -1.0, gl_VertexID == 2 ? 3.0 : -1.0);
    gl_Position = vec4(vScreen, 1.0, 1.0);
}
"""

_BACKDROP_FRAG_SHADER = """#version 330 core
in vec2 vScreen;
%(environment)s
uniform vec2 uViewScale;
out vec4 FragColor;

void main() {
    vec3 d = normalize(uViewToEnv * vec3(vScreen * uViewScale, -1.0));
    FragColor = vec4(pow(aces(textureLod(uEnv, toEquirect(d), 0.0).rgb * uExposure), vec3(1.0 / 2.2)), 1.0);
}
""" % {"environment": _ENV_GLSL}

_GRID_VERT_SHADER = """#version 330 core
layout(location = 0) in vec3 aPos;
layout(location = 1) in vec4 aColor;

uniform mat4 uMVP;
out vec4 vColor;

void main() {
    vColor = aColor;
    gl_Position = uMVP * vec4(aPos, 1.0);
}
"""

_GRID_FRAG_SHADER = """#version 330 core
in vec4 vColor;
out vec4 FragColor;

void main() {
    FragColor = vColor;
}
"""


class Model3DViewportWidget(QOpenGLWidget):
    """Hardware-accelerated 3D viewport: orbit, pan, zoom, wireframe and ground grid, with the
    model's colour, material and lighting (core.model_look), and lights that can be turned.

    The mesh can come later (set_mesh): big models load on a worker thread meanwhile."""

    drag_lights_changed = Signal(bool)  # (L switches what dragging turns)
    lighting_changed = Signal()  # an HDRI arrived or was dropped: the toolbar shows what's in use
    backdrop_changed = Signal(bool)  # (B)

    def __init__(self, mesh: Optional[Mesh3D] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.mesh: Optional[Mesh3D] = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        # Camera orientation & transform
        self.rot_x: float = 20.0
        self.rot_y: float = -35.0
        self.pan_x: float = 0.0
        self.pan_y: float = 0.0
        self.zoom: float = 1.0

        # The look (kept between viewings: core.settings.get_model_view)
        look = get_model_view()
        self.color = QColor(look["color"])
        self.material_id: str = look["material"]
        self.lighting_id: str = look["lighting"]
        self.roughness: float = look["metal_roughness"]
        self.light_yaw: float = 0.0  # the lights (or the HDRI) turned round the model (degrees)
        self.light_pitch: float = 0.0
        self.drag_lights: bool = False  # left-drag turns the lights instead of the model
        self.status_text: str = ""  # shown instead of the model (e.g. "Loading...")
        # An HDRI as the lighting (core.environment), instead of the preset self.lighting_id
        self.environment: Optional[environment.Environment] = None
        self.hdri_key: str = ""  # the HDRI in use or on its way
        self.exposure_stops: float = look["hdri_exposure"]
        self.show_backdrop: bool = look["hdri_backdrop"]
        self.notice: str = ""  # a passing message over the model
        self._hdri_token = 0
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.setInterval(6000)
        self._notice_timer.timeout.connect(self._clear_notice)

        # Mouse interaction tracking
        self.last_mouse_pos: Optional[QPoint] = None
        self.mouse_button: Optional[Qt.MouseButton] = None
        self._turning_lights = False

        # Display modes: "Shaded", "Wireframe", "Shaded + Wireframe", "Vertices"
        self.render_mode: str = "Shaded"
        self.show_grid: bool = True

        # Turntable auto-rotate
        self.auto_rotate: bool = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._on_turntable_tick)

        # OpenGL resource IDs
        self.mesh_program: Optional[QOpenGLShaderProgram] = None
        self.grid_program: Optional[QOpenGLShaderProgram] = None
        self.vao: Optional[int] = None
        self.vbo: Optional[int] = None
        self.nbo: Optional[int] = None
        self.ebo: Optional[int] = None
        self.grid_vao: Optional[int] = None
        self.grid_vbo_pos: Optional[int] = None
        self.grid_vbo_col: Optional[int] = None
        self.grid_vert_count: int = 0
        self.index_count: int = 0
        self.vertex_count: int = 0
        self.face_count: int = 0
        self.backdrop_program: Optional[QOpenGLShaderProgram] = None
        self.backdrop_vao: Optional[int] = None
        self._env_texture: Optional[int] = None
        self._env_dirty = False
        self._gl_ready = False
        self._needs_upload = False
        if mesh is not None:
            self.set_mesh(mesh)
        if look["hdri"]:
            self.use_hdri(look["hdri"], chosen=False)

    # ------------------ the model and its look ------------------
    def set_mesh(self, mesh: Mesh3D) -> None:
        """Show this model (its arrays go to the GPU when it next paints, then are let go)."""
        self.mesh = mesh
        self.vertex_count, self.face_count = mesh.vertex_count, mesh.face_count
        self.status_text = ""
        self._needs_upload = True
        self.update()

    def set_status(self, text: str) -> None:
        self.status_text = text
        self.update()

    def material(self) -> Material:
        return with_roughness(MATERIALS[self.material_id], self.roughness)

    def lighting(self) -> Lighting:
        return LIGHTINGS[self.lighting_id]

    def set_color(self, color: QColor, remember: bool = True) -> None:
        """remember: keep it for next time (not on every step of a drag in the picker)."""
        self.color = QColor(color)
        self.color.setAlpha(255)
        if remember:
            set_model_view(color=self.color.name())
        self.update()

    def set_material(self, material_id: str) -> None:
        if material_id in MATERIALS:
            self.material_id = material_id
            set_model_view(material=material_id)
            self.update()

    def set_roughness(self, roughness: float, remember: bool = True) -> None:
        """Metal's roughness (remember: as for set_color)."""
        self.roughness = roughness
        if remember:
            set_model_view(metal_roughness=roughness)
        self.update()

    def set_lighting(self, lighting_id: str) -> None:
        """A preset: any HDRI goes (or stops being waited for)."""
        if lighting_id in LIGHTINGS:
            self.lighting_id = lighting_id
            self._hdri_token += 1
            had_hdri = bool(self.hdri_key)
            if self.environment is not None:  # (how it was turned means nothing to the preset's lights)
                self.light_yaw = self.light_pitch = 0.0
            self.hdri_key, self.environment = "", None
            self._env_dirty = self._env_texture is not None  # (let its texture go)
            set_model_view(lighting=lighting_id, hdri="")
            self._clear_notice()
            if had_hdri:
                self.lighting_changed.emit()
            self.update()

    def use_hdri(self, key: str, chosen: bool = True) -> None:
        """Light the model with this panorama (a path; read on a worker thread, once). The current
        lighting stays until it's ready. chosen: picked now, so it's remembered once it has worked
        (else it's the remembered one: left as it is if it can't be read, as on a drive that's
        unplugged for now)."""
        self._hdri_token += 1
        token = self._hdri_token
        self.hdri_key = key
        self.set_notice(f"Loading {environment.display_name(key)}...", lasting=True)

        def done(loaded: environment.Environment) -> None:
            if token != self._hdri_token or not shiboken6.isValid(self):
                return
            self.environment = loaded
            self._env_dirty = True
            self.light_yaw = self._key_light_yaw()
            if chosen:
                set_model_view(hdri=key)
            self._clear_notice()
            self.lighting_changed.emit()
            self.update()

        def failed(message: str) -> None:
            if token != self._hdri_token or not shiboken6.isValid(self):
                return
            self.hdri_key = self.environment.key if self.environment is not None else ""
            self.set_notice(f"That HDRI can't be used: {message}")
            self.lighting_changed.emit()

        run_job(lambda: environment.load(key), done, failed)

    def set_exposure(self, stops: float, remember: bool = True) -> None:
        self.exposure_stops = stops
        if remember:
            set_model_view(hdri_exposure=stops)
        self.update()

    def set_backdrop(self, on: bool) -> None:
        if on != self.show_backdrop:
            self.show_backdrop = on
            set_model_view(hdri_backdrop=on)
            self.backdrop_changed.emit(on)
            self.update()

    def set_notice(self, text: str, lasting: bool = False) -> None:
        self.notice = text
        self._notice_timer.stop()
        if not lasting:
            self._notice_timer.start()
        self.update()

    def _clear_notice(self) -> None:
        self.notice = ""
        self._notice_timer.stop()
        self.update()

    def _view_to_env(self) -> np.ndarray:
        return environment.view_to_env(self.rot_x, self.rot_y, self.light_yaw)

    def _key_light_yaw(self) -> float:
        """How an HDRI starts out turned: as its strongest light comes to the front right of what's
        seen now. Where it happens to sit in the panorama, it could be behind the model."""
        return environment.key_light_yaw(self.environment, self.rot_y)

    def set_drag_lights(self, on: bool) -> None:
        if on != self.drag_lights:
            self.drag_lights = on
            self.drag_lights_changed.emit(on)
            self.update()

    def reset_lights(self) -> None:
        self.light_yaw = self.light_pitch = 0.0
        if self.environment is not None:
            self.light_yaw = self._key_light_yaw()
        self.update()

    def set_render_mode(self, mode: str) -> None:
        """Update viewport rendering mode ('Shaded', 'Wireframe', 'Shaded + Wireframe', 'Vertices')."""
        self.render_mode = mode
        self.update()

    def toggle_ground_grid(self, enabled: bool) -> None:
        """Toggle ground reference grid plane."""
        self.show_grid = enabled
        self.update()

    def toggle_turntable(self, enabled: bool) -> None:
        """Toggle turntable continuous rotation."""
        self.auto_rotate = enabled
        if enabled:
            self.timer.start(16)  # ~60 FPS
        else:
            self.timer.stop()

    def reset_camera(self) -> None:
        """Reset camera orientation, pan, and zoom to defaults."""
        self.rot_x = 20.0
        self.rot_y = -35.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.zoom = 1.0
        self.update()

    def _on_turntable_tick(self) -> None:
        self.rot_y = (self.rot_y + 0.8) % 360.0
        if self.environment is not None:  # (the model spins in a still HDRI: it's the camera that orbits)
            self.light_yaw = (self.light_yaw - 0.8) % 360.0
        self.update()

    # ------------------ Mouse & Keyboard Controls ------------------
    def mousePressEvent(self, event) -> None:
        self.last_mouse_pos = event.position().toPoint()
        self.mouse_button = event.button()
        # Shift+drag (or Drag: Lights) turns the lights; the model stays as it is
        self._turning_lights = self.drag_lights or bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)

    def mouseMoveEvent(self, event) -> None:
        if self.last_mouse_pos is not None:
            dx = event.position().x() - self.last_mouse_pos.x()
            dy = event.position().y() - self.last_mouse_pos.y()

            if event.buttons() & Qt.MouseButton.LeftButton:
                if self._turning_lights:
                    self.light_yaw = (self.light_yaw + dx * 0.5) % 360.0
                    if self.environment is None:  # (an HDRI only turns round the vertical: its horizon stays level)
                        self.light_pitch = max(-85.0, min(85.0, self.light_pitch + dy * 0.5))  # (drag up: the light goes up)
                else:
                    # Orbit rotation
                    self.rot_y += dx * 0.5
                    self.rot_x += dy * 0.5
                    self.rot_x = max(-89.0, min(89.0, self.rot_x))
                self.update()
            elif (
                event.buttons() & Qt.MouseButton.MiddleButton
                or event.buttons() & Qt.MouseButton.RightButton
            ):
                # Pan
                factor = 0.003 / max(self.zoom, 0.05)
                self.pan_x += dx * factor
                self.pan_y -= dy * factor
                self.update()

            self.last_mouse_pos = event.position().toPoint()

    def mouseReleaseEvent(self, event) -> None:
        self.last_mouse_pos = None
        self.mouse_button = None
        self._turning_lights = False
        self.update()

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta > 0:
            self.zoom = min(20.0, self.zoom * 1.15)
        else:
            self.zoom = max(0.08, self.zoom / 1.15)
        self.update()

    def keyPressEvent(self, event) -> None:
        """Viewport keys (see ui.shortcuts): cycle wireframe/shaded, toggle grid, reset camera, drag lights."""
        if shortcuts.matches("viewer3d.wireframe", event):
            modes = ["Shaded", "Wireframe", "Shaded + Wireframe"]
            cur_idx = modes.index(self.render_mode) if self.render_mode in modes else 0
            self.render_mode = modes[(cur_idx + 1) % len(modes)]
            self.update()
        elif shortcuts.matches("viewer3d.grid", event):
            self.show_grid = not self.show_grid
            self.update()
        elif shortcuts.matches("viewer3d.reset", event):
            self.reset_camera()
        elif shortcuts.matches("viewer3d.drag_lights", event):
            self.set_drag_lights(not self.drag_lights)
        elif shortcuts.matches("viewer3d.backdrop", event) and self.environment is not None:
            self.set_backdrop(not self.show_backdrop)
        else:
            super().keyPressEvent(event)

    # ------------------ OpenGL Pipeline ------------------
    def initializeGL(self) -> None:
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glDepthFunc(GL.GL_LEQUAL)
        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)

        # 1. Compile Shaders
        self.mesh_program = QOpenGLShaderProgram(self)
        self.mesh_program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, _MESH_VERT_SHADER)
        self.mesh_program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, _MESH_FRAG_SHADER)
        self.mesh_program.link()
        self._uniforms = {
            name: self.mesh_program.uniformLocation(name)
            for name in ("uUnlit", "uLightCount", "uLightDir", "uLightColor", "uSky", "uGround", "uEnvLow", "uEnvHigh", "uDiffuse", "uWrap",
                         "uSpecular", "uShininess", "uPhong", "uMetal", "uRim", "uBands", "uShowNormals",
                         "uUseEnv", "uEnv", "uSH", "uViewToEnv", "uExposure", "uEnvLevel", "uEnvSpread", "uRoughness")
        }

        self.backdrop_program = QOpenGLShaderProgram(self)
        self.backdrop_program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, _BACKDROP_VERT_SHADER)
        self.backdrop_program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, _BACKDROP_FRAG_SHADER)
        self.backdrop_program.link()
        self.backdrop_vao = GL.glGenVertexArrays(1)  # (empty: the triangle comes from gl_VertexID)
        self._env_texture = None  # (it went with the old context, if there was one)
        self._env_dirty = self.environment is not None

        self.grid_program = QOpenGLShaderProgram(self)
        self.grid_program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, _GRID_VERT_SHADER)
        self.grid_program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, _GRID_FRAG_SHADER)
        self.grid_program.link()
        # A new context (the first, or after the widget moved window): the old buffers went with
        # the old one, so the model is uploaded again if its arrays are still here
        self.vao = self.vbo = self.nbo = self.ebo = self.grid_vao = self.grid_vbo_pos = self.grid_vbo_col = None
        self._needs_upload = self.mesh is not None and self.mesh.vertices is not None
        if not self._needs_upload:
            self._upload_grid_data()
        self._gl_ready = True

    def _free_mesh_buffers(self) -> None:
        for name in ("vbo", "nbo", "ebo"):
            buffer = getattr(self, name)
            if buffer is not None:
                GL.glDeleteBuffers(1, [buffer])
                setattr(self, name, None)
        for name in ("vao", "grid_vao"):
            vao = getattr(self, name)
            if vao is not None:
                GL.glDeleteVertexArrays(1, [vao])
                setattr(self, name, None)
        for name in ("grid_vbo_pos", "grid_vbo_col"):
            buffer = getattr(self, name)
            if buffer is not None:
                GL.glDeleteBuffers(1, [buffer])
                setattr(self, name, None)

    def _upload_mesh_data(self) -> None:
        """Upload vertex positions, normals, and indices to GPU (then let the CPU copies go:
        a big sculpt's are hundreds of MB)."""
        self._needs_upload = False
        self._free_mesh_buffers()
        mesh = self.mesh
        if mesh is None or mesh.vertices is None or mesh.vertex_count == 0:
            return

        verts = np.ascontiguousarray(mesh.vertices, dtype=np.float32)
        normals = np.ascontiguousarray(mesh.normals, dtype=np.float32)

        self.vao = GL.glGenVertexArrays(1)
        GL.glBindVertexArray(self.vao)

        # Positions (loc 0)
        self.vbo = GL.glGenBuffers(1)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.vbo)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, verts.nbytes, verts, GL.GL_STATIC_DRAW)
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, ctypes.c_void_p(0))

        # Normals (loc 1)
        self.nbo = GL.glGenBuffers(1)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.nbo)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, normals.nbytes, normals, GL.GL_STATIC_DRAW)
        GL.glEnableVertexAttribArray(1)
        GL.glVertexAttribPointer(1, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, ctypes.c_void_p(0))

        # Faces / Elements
        if mesh.face_count > 0:
            faces = np.ascontiguousarray(mesh.faces, dtype=np.uint32)
            self.index_count = faces.size
            self.ebo = GL.glGenBuffers(1)
            GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, self.ebo)
            GL.glBufferData(GL.GL_ELEMENT_ARRAY_BUFFER, faces.nbytes, faces, GL.GL_STATIC_DRAW)
        else:
            self.index_count = 0

        GL.glBindVertexArray(0)
        self._upload_grid_data()
        mesh.vertices = mesh.faces = mesh.normals = None  # (on the GPU now; the counts are kept above)

    def _upload_grid_data(self) -> None:
        """Generate and upload 3D ground plane grid lines to GPU."""
        ground_y = float(getattr(self.mesh, "ground_y", -0.9))
        extent = 2.2
        step = 0.2

        lines_pos: list[list[float]] = []
        lines_col: list[list[float]] = []

        col_regular = [0.24, 0.27, 0.34, 0.45]
        col_border = [0.36, 0.40, 0.50, 0.65]
        col_x_axis = [0.94, 0.27, 0.27, 0.85]  # Red for X
        col_z_axis = [0.23, 0.51, 0.96, 0.85]  # Blue for Z

        vals = np.arange(-extent, extent + step * 0.5, step)
        for v in vals:
            # Line parallel to X (Z constant)
            lines_pos.append([-extent, ground_y, float(v)])
            lines_pos.append([extent, ground_y, float(v)])
            if abs(v) < 1e-4:
                c = col_x_axis
            elif abs(abs(v) - extent) < 1e-4:
                c = col_border
            else:
                c = col_regular
            lines_col.append(c)
            lines_col.append(c)

            # Line parallel to Z (X constant)
            lines_pos.append([float(v), ground_y, -extent])
            lines_pos.append([float(v), ground_y, extent])
            if abs(v) < 1e-4:
                c = col_z_axis
            elif abs(abs(v) - extent) < 1e-4:
                c = col_border
            else:
                c = col_regular
            lines_col.append(c)
            lines_col.append(c)

        grid_pos = np.ascontiguousarray(lines_pos, dtype=np.float32)
        grid_col = np.ascontiguousarray(lines_col, dtype=np.float32)
        self.grid_vert_count = len(grid_pos)

        self.grid_vao = GL.glGenVertexArrays(1)
        GL.glBindVertexArray(self.grid_vao)

        self.grid_vbo_pos = GL.glGenBuffers(1)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.grid_vbo_pos)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, grid_pos.nbytes, grid_pos, GL.GL_STATIC_DRAW)
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, 0, ctypes.c_void_p(0))

        self.grid_vbo_col = GL.glGenBuffers(1)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.grid_vbo_col)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, grid_col.nbytes, grid_col, GL.GL_STATIC_DRAW)
        GL.glEnableVertexAttribArray(1)
        GL.glVertexAttribPointer(1, 4, GL.GL_FLOAT, GL.GL_FALSE, 0, ctypes.c_void_p(0))

        GL.glBindVertexArray(0)

    def resizeGL(self, w: int, h: int) -> None:
        GL.glViewport(0, 0, w, h)

    def _upload_environment(self) -> None:
        """The panorama to the GPU, with mipmaps for blurred reflections. Half floats, read straight
        from the array, as its pixels may be asked for again in a new context."""
        self._env_dirty = False
        if self._env_texture is not None:
            GL.glDeleteTextures(1, [self._env_texture])
            self._env_texture = None
        if self.environment is None:
            return
        pixels = self.environment.pixels
        self._env_texture = GL.glGenTextures(1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self._env_texture)
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGB16F, pixels.shape[1], pixels.shape[0], 0, GL.GL_RGB, GL.GL_HALF_FLOAT,
                        pixels.ctypes.data_as(ctypes.c_void_p))
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 4)
        GL.glGenerateMipmap(GL.GL_TEXTURE_2D)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR_MIPMAP_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_REPEAT)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)

    def _env_ready(self) -> bool:
        return self.environment is not None and self._env_texture is not None

    def _set_env_uniforms(self, program: QOpenGLShaderProgram, uniforms: dict[str, int]) -> None:
        """What the model and the backdrop share: the panorama, how it's turned, the exposure."""
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self._env_texture)
        GL.glUniform1i(uniforms["uEnv"], 0)
        GL.glUniformMatrix3fv(uniforms["uViewToEnv"], 1, GL.GL_TRUE, self._view_to_env().astype(np.float32))
        GL.glUniform1f(uniforms["uExposure"], self.environment.exposure * 2.0 ** self.exposure_stops)

    def _draw_backdrop(self, aspect: float) -> None:
        """The HDRI behind everything else (not in the depth buffer, so anything draws over it)."""
        program = self.backdrop_program
        program.bind()
        uniforms = {name: program.uniformLocation(name) for name in ("uEnv", "uViewToEnv", "uExposure", "uViewScale")}
        self._set_env_uniforms(program, uniforms)
        half = math.tan(math.radians(_FIELD_OF_VIEW / 2))
        GL.glUniform2f(uniforms["uViewScale"], half * aspect, half)
        GL.glDepthMask(GL.GL_FALSE)
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glBindVertexArray(self.backdrop_vao)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        GL.glBindVertexArray(0)
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glDepthMask(GL.GL_TRUE)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        program.release()

    def _set_look_uniforms(self, unlit: bool) -> None:
        """The material and lights for the next draw (or flat, unlit colour for lines and points)."""
        u = self._uniforms
        GL.glUniform1f(u["uUnlit"], 1.0 if unlit else 0.0)
        if unlit:
            return
        material = self.material()
        dirs = np.zeros((MAX_LIGHTS, 3), dtype=np.float32)
        colors = np.zeros((MAX_LIGHTS, 3), dtype=np.float32)
        use_env = self._env_ready()
        GL.glUniform1f(u["uUseEnv"], 1.0 if use_env else 0.0)
        lighting = self.lighting()
        if use_env:
            env = self.environment
            count = len(env.lights)
            if count:
                dirs[:count] = np.array([light.direction for light in env.lights]) @ self._view_to_env()  # (to view space)
                colors[:count] = [light.color for light in env.lights]
            self._set_env_uniforms(self.mesh_program, u)
            GL.glUniform3fv(u["uSH"], 9, env.harmonics)
            level = environment.reflection_level(material.reflection_blur, env.pixels.shape[1])
            GL.glUniform1f(u["uEnvLevel"], level)
            GL.glUniform1f(u["uEnvSpread"], environment.reflection_spread(level, env.pixels.shape[1]))
        else:
            count = len(lighting.lights)
            dirs[:count] = light_directions(lighting, self.light_yaw, self.light_pitch)
            colors[:count] = [light.color for light in lighting.lights]
        GL.glUniform1i(u["uLightCount"], count)
        GL.glUniform3fv(u["uLightDir"], MAX_LIGHTS, dirs)
        GL.glUniform3fv(u["uLightColor"], MAX_LIGHTS, colors)
        GL.glUniform3f(u["uSky"], *lighting.sky)
        GL.glUniform3f(u["uGround"], *lighting.ground)
        GL.glUniform3f(u["uEnvLow"], *ENV_LOW)
        GL.glUniform3f(u["uEnvHigh"], *ENV_HIGH)
        specular = material.env_specular if use_env and material.env_specular is not None else material.specular
        for name, value in (("uDiffuse", material.diffuse), ("uWrap", material.wrap), ("uSpecular", specular),
                            ("uShininess", material.shininess), ("uPhong", float(material.phong)), ("uMetal", material.metal),
                            ("uRim", material.rim), ("uBands", float(material.bands)), ("uShowNormals", float(material.normals)),
                            ("uRoughness", material.roughness)):
            GL.glUniform1f(u[name], value)

    def paintGL(self) -> None:
        self._draw_scene(self.width(), self.height(), self.show_grid)
        # 3. 2D HUD & Orientation Gizmo Overlay via QPainter
        self._draw_hud_and_gizmo(self.width(), self.height())

    def render_image(self, width: int, height: int) -> Optional[QImage]:
        """What's shown, without the grid or the overlay, as a picture of this size (the Render
        window's way when LuxCore can't be used). None if OpenGL isn't up yet."""
        if not self._gl_ready:
            return None
        self.makeCurrent()
        try:
            picture_format = QOpenGLFramebufferObjectFormat()
            picture_format.setAttachment(QOpenGLFramebufferObject.Attachment.CombinedDepthStencil)
            picture_format.setSamples(8)
            picture_format.setInternalTextureFormat(GL.GL_RGBA8)
            target = QOpenGLFramebufferObject(width, height, picture_format)
            if not target.isValid():
                return None
            target.bind()
            GL.glViewport(0, 0, width, height)
            self._draw_scene(width, height, False)
            target.release()
            return target.toImage().convertToFormat(QImage.Format.Format_RGB32)  # (the right way up)
        finally:
            GL.glViewport(0, 0, self.width(), self.height())
            self.doneCurrent()

    def _draw_scene(self, w: int, h: int, grid: bool) -> None:
        if self._needs_upload and self._gl_ready:
            self._upload_mesh_data()
        if self._env_dirty and self._gl_ready:
            self._upload_environment()
        # QPainter (used for the HUD overlay) resets GL state at the end of every
        # frame, so depth testing and blending must be re-applied before each draw.
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glDepthFunc(GL.GL_LEQUAL)
        GL.glDepthMask(GL.GL_TRUE)
        GL.glEnable(GL.GL_BLEND)
        # (the window's own alpha stays solid: Qt treats it as how see-through the pixel is)
        GL.glBlendFuncSeparate(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA, GL.GL_ONE, GL.GL_ONE_MINUS_SRC_ALPHA)
        GL.glDisable(GL.GL_CULL_FACE)

        GL.glClearColor(0.08, 0.09, 0.11, 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)

        aspect = float(w) / float(max(h, 1))

        # Matrices
        proj = QMatrix4x4()
        proj.perspective(_FIELD_OF_VIEW, aspect, 0.05, 100.0)

        view = QMatrix4x4()
        cam_dist = 3.2 / max(self.zoom, 0.05)
        view.translate(self.pan_x, self.pan_y, -cam_dist)
        view.rotate(self.rot_x, 1.0, 0.0, 0.0)
        view.rotate(self.rot_y, 0.0, 1.0, 0.0)

        model = QMatrix4x4()
        model_view = view * model
        mvp = proj * model_view

        if self.show_backdrop and self._env_ready() and self.backdrop_program is not None:
            self._draw_backdrop(aspect)

        # 1. Draw Ground Plane Grid
        if grid and self.grid_vao is not None and self.grid_program is not None:
            self.grid_program.bind()
            self.grid_program.setUniformValue("uMVP", mvp)
            GL.glBindVertexArray(self.grid_vao)
            GL.glDrawArrays(GL.GL_LINES, 0, self.grid_vert_count)
            GL.glBindVertexArray(0)
            self.grid_program.release()

        # 2. Draw 3D Model Mesh
        if self.vao is not None and self.mesh_program is not None and self.vertex_count > 0:
            self.mesh_program.bind()
            self.mesh_program.setUniformValue("uMVP", mvp)
            self.mesh_program.setUniformValue("uModelView", model_view)
            self.mesh_program.setUniformValue("uNormalMatrix", model_view.normalMatrix())

            GL.glBindVertexArray(self.vao)
            if self.index_count > 0 and self.ebo is not None:
                GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, self.ebo)

            mesh_color = QVector3D(self.color.redF(), self.color.greenF(), self.color.blueF())
            wire_color = QVector3D(0.38, 0.68, 1.0)
            overlay_wire_color = QVector3D(0.12, 0.16, 0.22)

            if self.render_mode == "Wireframe":
                GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_LINE)
                self.mesh_program.setUniformValue("uColor", wire_color)
                self._set_look_uniforms(unlit=True)
                if self.index_count > 0:
                    GL.glDrawElements(GL.GL_TRIANGLES, self.index_count, GL.GL_UNSIGNED_INT, ctypes.c_void_p(0))
                else:
                    GL.glDrawArrays(GL.GL_POINTS, 0, self.vertex_count)
                GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_FILL)

            elif self.render_mode == "Shaded + Wireframe":
                # Pass 1: Shaded geometry with polygon offset
                GL.glEnable(GL.GL_POLYGON_OFFSET_FILL)
                GL.glPolygonOffset(1.0, 1.0)
                GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_FILL)
                self.mesh_program.setUniformValue("uColor", mesh_color)
                self._set_look_uniforms(unlit=False)
                if self.index_count > 0:
                    GL.glDrawElements(GL.GL_TRIANGLES, self.index_count, GL.GL_UNSIGNED_INT, ctypes.c_void_p(0))
                GL.glDisable(GL.GL_POLYGON_OFFSET_FILL)

                # Pass 2: Wireframe lines overlay
                GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_LINE)
                self.mesh_program.setUniformValue("uColor", overlay_wire_color)
                self._set_look_uniforms(unlit=True)
                if self.index_count > 0:
                    GL.glDrawElements(GL.GL_TRIANGLES, self.index_count, GL.GL_UNSIGNED_INT, ctypes.c_void_p(0))
                GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_FILL)

            elif self.render_mode == "Vertices" or self.index_count == 0:
                GL.glPointSize(3.5)
                self.mesh_program.setUniformValue("uColor", wire_color)
                self._set_look_uniforms(unlit=True)
                GL.glDrawArrays(GL.GL_POINTS, 0, self.vertex_count)

            else:  # "Shaded"
                GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_FILL)
                self.mesh_program.setUniformValue("uColor", mesh_color)
                self._set_look_uniforms(unlit=False)
                GL.glDrawElements(GL.GL_TRIANGLES, self.index_count, GL.GL_UNSIGNED_INT, ctypes.c_void_p(0))

            GL.glBindVertexArray(0)
            GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
            self.mesh_program.release()

    def _draw_hud_and_gizmo(self, w: int, h: int) -> None:
        """Render miniature coordinate orientation gizmo, the lights' direction and the statistics badge."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self.status_text:
            painter.setPen(QColor(209, 213, 219))
            painter.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
            painter.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.status_text)
            painter.end()
            return

        # 1. Orientation Gizmo in bottom-left corner
        gx, gy = 44.0, float(h - 44)
        length = 26.0

        rad_x = math.radians(self.rot_x)
        rad_y = math.radians(self.rot_y)
        cos_x, sin_x = math.cos(rad_x), math.sin(rad_x)
        cos_y, sin_y = math.cos(rad_y), math.sin(rad_y)

        # X axis (Red)
        xx = length * cos_y
        xy = -length * sin_y * sin_x
        painter.setPen(QPen(QColor(239, 68, 68), 2.5))
        painter.drawLine(QPointF(gx, gy), QPointF(gx + xx, gy + xy))
        painter.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        painter.drawText(QPointF(gx + xx + 3, gy + xy + 3), "X")

        # Y axis (Green)
        yx = 0.0
        yy = -length * cos_x
        painter.setPen(QPen(QColor(34, 197, 94), 2.5))
        painter.drawLine(QPointF(gx, gy), QPointF(gx + yx, gy + yy))
        painter.drawText(QPointF(gx + yx + 3, gy + yy - 2), "Y")

        # Z axis (Blue)
        zx = length * sin_y
        zy = length * cos_y * sin_x
        painter.setPen(QPen(QColor(59, 130, 246), 2.5))
        painter.drawLine(QPointF(gx, gy), QPointF(gx + zx, gy + zy))
        painter.drawText(QPointF(gx + zx + 3, gy + zy + 3), "Z")

        # 2. Viewport Mode Badge in top-left
        stats = self.mesh.stats if self.mesh is not None else None
        count_text = stats.summary() if stats is not None and stats.polygons > 0 else f"{self.face_count:,} tris"
        badge_text = f"{self.render_mode}  {count_text}"
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(20, 22, 28, 190))
        badge_width = max(175, painter.fontMetrics().horizontalAdvance(badge_text) + 40)
        painter.drawRoundedRect(12, 12, badge_width, 26, 5, 5)
        painter.setPen(QColor(209, 213, 219))
        painter.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
        painter.drawText(QRectF(12, 12, badge_width, 26), Qt.AlignmentFlag.AlignCenter, badge_text)
        if self.notice:  # (an HDRI on its way, or why it can't be used)
            painter.drawText(QRectF(14, 42, w - 90, 40), Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap, self.notice)

        # 3. The key light's direction, top-right: a dot on a disc (hollow when it's behind)
        self._draw_light_dial(painter, QPointF(w - 40.0, 40.0), 22.0)

        # 4. How to move it, bottom-right
        painter.setPen(QColor(156, 163, 175, 170))
        painter.setFont(QFont("Segoe UI", 8))
        turned = "HDRI" if self.environment is not None else "lights"
        hint = f"Drag: turn   Shift+drag: {turned}   Right-drag: pan   Scroll: zoom"
        painter.drawText(QRectF(0, h - 26, w - 12, 20), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, hint)
        painter.end()

    def _draw_light_dial(self, painter: QPainter, center: QPointF, radius: float) -> None:
        turning = self._turning_lights or self.drag_lights
        painter.setPen(QPen(QColor(209, 213, 219, 200 if turning else 110), 1.2))
        painter.setBrush(QColor(20, 22, 28, 190))
        painter.drawEllipse(center, radius, radius)
        key = self._key_light_direction()
        if key is not None:  # (a soft, even HDRI has no light to point at)
            x, y, z = key
            dot = QPointF(center.x() + x * radius * 0.8, center.y() - y * radius * 0.8)
            painter.setPen(QPen(QColor(253, 224, 71), 1.5))
            painter.setBrush(QColor(253, 224, 71) if z >= 0 else Qt.BrushStyle.NoBrush)
            painter.drawEllipse(dot, 4.0, 4.0)
        if turning:
            painter.setPen(QColor(209, 213, 219))
            painter.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
            label = "HDRI" if self.environment is not None else "Lights"
            painter.drawText(QRectF(center.x() - 60, center.y() + radius + 2, 120, 16), Qt.AlignmentFlag.AlignCenter, label)

    def _key_light_direction(self) -> Optional[np.ndarray]:
        """The strongest light's direction in view space."""
        if self.environment is None:
            return light_directions(self.lighting(), self.light_yaw, self.light_pitch)[0]
        if not self.environment.lights:
            return None
        return np.array(self.environment.lights[0].direction) @ self._view_to_env()


def _swatch_icon(color: QColor) -> QIcon:
    """A rounded square of the colour, for the colour button."""
    pixmap = QPixmap(18, 18)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(0, 0, 0, 90), 1))
    painter.setBrush(color)
    painter.drawRoundedRect(QRectF(0.5, 0.5, 17, 17), 4, 4)
    painter.end()
    return QIcon(pixmap)


# ----------------------------------------------------------------------
# Texture / Image Viewer (Interactive Zoom & Pan)
# ----------------------------------------------------------------------
def _checker_brush(tile_size: int = 8) -> QBrush:
    """Repeating light grey / white checker (the usual transparency pattern)."""
    tile = QPixmap(tile_size * 2, tile_size * 2)
    tile.fill(QColor(255, 255, 255))
    p = QPainter(tile)
    light_grey = QColor(204, 204, 204)
    p.fillRect(tile_size, 0, tile_size, tile_size, light_grey)
    p.fillRect(0, tile_size, tile_size, tile_size, light_grey)
    p.end()
    return QBrush(tile)


class TextureViewerWidget(QWidget):
    """Interactive texture and image viewer supporting zoom, pan, and alpha checkerboard.

    It starts empty, saying the image is loading: the dialog decodes it on a worker thread
    (core.full_image) and hands it over with set_image, or says why it can't with set_message."""

    def __init__(self, file_path: Path, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.file_path = file_path
        self.pixmap, self.has_alpha = QPixmap(), False
        self.message = f"Loading {file_path.name}..."
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.zoom: float = 1.0
        self.pan_x: float = 0.0
        self.pan_y: float = 0.0
        # On by default only when there's transparency to see
        self.show_checkerboard: bool = self.has_alpha
        self._checker_brush = _checker_brush()

        self.last_mouse_pos: Optional[QPoint] = None
        self._fit_to_screen_requested: bool = True
        self.tiles = 1  # the image repeated tiles x tiles (Tile: to check the seams)

    def set_tiles(self, tiles: int) -> None:
        """Show the image repeated tiles x tiles (1: once), fitted to the view again."""
        self.tiles = max(1, tiles)
        self.fit_to_screen()
        self.update()

    def set_image(self, pixmap: QPixmap, has_alpha: bool) -> None:
        self.pixmap, self.has_alpha, self.message = pixmap, has_alpha, ""
        self.show_checkerboard = has_alpha
        self._fit_to_screen_requested = True
        self.fit_to_screen()
        if self.width() > 20 and self.height() > 20:
            self._fit_to_screen_requested = False
        self.update()

    def set_message(self, text: str) -> None:
        self.message = text
        self.update()

    def fit_to_screen(self) -> None:
        """Scale image to fit within the viewport window."""
        if self.pixmap.isNull():
            return
        w, h = self.width(), self.height()
        if w > 20 and h > 20:
            scale_w = (w - 40) / (self.pixmap.width() * self.tiles)
            scale_h = (h - 40) / (self.pixmap.height() * self.tiles)
            self.zoom = min(scale_w, scale_h, 1.0)
            self.pan_x = 0.0
            self.pan_y = 0.0
            self.update()

    def set_actual_size(self) -> None:
        """Reset zoom to 100% (1:1 pixel mapping)."""
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.update()

    def toggle_checkerboard(self, enabled: bool) -> None:
        """Toggle transparency checkerboard background."""
        self.show_checkerboard = enabled
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._fit_to_screen_requested and not self.pixmap.isNull():
            self.fit_to_screen()
            self._fit_to_screen_requested = False

    def mousePressEvent(self, event) -> None:
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton):
            self.last_mouse_pos = event.position().toPoint()

    def mouseMoveEvent(self, event) -> None:
        if self.last_mouse_pos is not None:
            dx = event.position().x() - self.last_mouse_pos.x()
            dy = event.position().y() - self.last_mouse_pos.y()
            self.pan_x += dx
            self.pan_y += dy
            self.last_mouse_pos = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        self.last_mouse_pos = None

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        mouse_pos = event.position()

        old_zoom = self.zoom
        if delta > 0:
            self.zoom = min(20.0, self.zoom * 1.2)
        else:
            self.zoom = max(0.05, self.zoom / 1.2)

        # Zoom toward cursor position
        zoom_factor = self.zoom / old_zoom
        self.pan_x = mouse_pos.x() - zoom_factor * (mouse_pos.x() - self.pan_x)
        self.pan_y = mouse_pos.y() - zoom_factor * (mouse_pos.y() - self.pan_y)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        w, h = self.width(), self.height()

        # Dark viewport background
        painter.fillRect(0, 0, w, h, QColor(18, 19, 24))

        if self.pixmap.isNull():
            painter.setPen(QColor(160, 160, 170))
            painter.drawText(
                QRectF(20, 0, w - 40, h),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                self.message or f"Unable to load image format:\n{self.file_path.name}",
            )
            return

        tile_w, tile_h = self.pixmap.width() * self.zoom, self.pixmap.height() * self.zoom
        target_w, target_h = tile_w * self.tiles, tile_h * self.tiles

        # Center if no manual pan has taken place
        if self.pan_x == 0.0 and self.pan_y == 0.0:
            dest_x = (w - target_w) / 2.0
            dest_y = (h - target_h) / 2.0
            self.pan_x = dest_x
            self.pan_y = dest_y
        else:
            dest_x = self.pan_x
            dest_y = self.pan_y

        img_rect = QRectF(dest_x, dest_y, target_w, target_h)

        # Checkerboard behind the image so transparent pixels are visible. One tiled-brush fill
        # of the on-screen part only, so the cost doesn't grow with the zoom level.
        if self.show_checkerboard:
            visible = img_rect.intersected(QRectF(0, 0, w, h))
            if not visible.isEmpty():
                painter.save()
                painter.setBrushOrigin(img_rect.topLeft())  # pattern moves with the image when panning
                painter.fillRect(visible, self._checker_brush)
                painter.restore()

        # Render texture smoothly (repeated, when tiled: each copy edge to edge, so seams show)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        for row in range(self.tiles):
            for col in range(self.tiles):
                tile = QRectF(dest_x + col * tile_w, dest_y + row * tile_h, tile_w, tile_h)
                if tile.intersects(QRectF(0, 0, w, h)):
                    painter.drawPixmap(tile, self.pixmap, QRectF(self.pixmap.rect()))

        # Subtle card border around image
        painter.setPen(QPen(QColor(60, 65, 80), 1))
        painter.drawRect(img_rect.toRect())

        # Display zoom percentage indicator badge in top-left
        zoom_text = f"{int(self.zoom * 100)}%"
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(20, 22, 28, 200))
        painter.drawRoundedRect(12, 12, 60, 24, 4, 4)
        painter.setPen(QColor(220, 220, 230))
        painter.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        painter.drawText(QRectF(12, 12, 60, 24), Qt.AlignmentFlag.AlignCenter, zoom_text)


# ----------------------------------------------------------------------
# Unified Asset Viewer Dialog
# ----------------------------------------------------------------------
@dataclass
class ViewerTarget:
    """A file to show in the viewer, as handed out by the viewer's navigate callback."""

    asset: AssetItem
    tags: Optional[list[Tag]] = None
    edit_tags: Optional[Callable[[], Optional[list[Tag]]]] = None
    position: str = ""  # e.g. "3 of 120"


APP_FILE_PICTURE_SIDE = 480  # the picture of a file only its app can show


class AssetViewerDialog(QDialog):
    """Unified modal viewer dialog for 3D models, textures, documents, and videos."""

    def __init__(
        self,
        asset: AssetItem,
        parent: Optional[QWidget] = None,
        tags: Optional[list[Tag]] = None,
        edit_tags: Optional[Callable[[], Optional[list[Tag]]]] = None,
        navigate: Optional[Callable[[int], Optional[ViewerTarget]]] = None,
        position: str = "",
        artbook=None,
        model_viewed: Optional[Callable[[AssetItem, Mesh3D], None]] = None,
        hdri_assets: Optional[Callable[[], Sequence[AssetItem]]] = None,
    ):
        """tags: the asset's tags to show (None hides the section).
        edit_tags: opens the tagging dialog; returns the updated tags, or None if nothing changed.
        navigate: navigate(-1 / +1) returns the previous / next file, or None at either end;
        it enables the < > buttons and Page Up / Page Down. position: e.g. "3 of 120".
        artbook: a ui.asset_menu.ArtbookSender (fill_artbook_menu, send_image_to_artbook): a PDF's pages can be
        sent to the Artbook (None: they can't). model_viewed(asset, mesh): a 3D model was loaded
        (the grid renders its picture from it; its arrays are let go after the call).
        hdri_assets: the library's files, to offer its HDRIs as lighting."""
        super().__init__(parent)
        self.asset = asset
        self.tags = tags
        self._edit_tags = edit_tags
        self._navigate = navigate
        self._position = position
        self._artbook = artbook
        self._model_viewed = model_viewed
        self._hdri_assets = hdri_assets
        self.video_player: Optional[VideoPlayer] = None  # at most one, owned by this dialog
        self.pdf_viewer: Optional[PdfViewer] = None  # (saves where you were before it goes)
        self._file: Optional[Path] = None  # a file inside an archive: its unpacked copy, once unpacked
        self.btn_tile: Optional[QPushButton] = None  # an image's Tile button (and its other tools), when one is shown
        self.unpacked: set[str] = set()  # str(asset.path) of the files inside archives unpacked meanwhile
        self.board_wanted = False  # a PureRef board's Open in Artbook: the window opens self.asset as a page after this closes
        self._btn_open_app: Optional[QPushButton] = None  # a file only its app can show: the first of its buttons (O)
        self._app_status: Optional[QLabel] = None  # (what became of opening it)
        self._launch_after: Optional[apps.App] = None  # open it in this app once it's been unpacked
        # Window state to return to when leaving fullscreen
        self._state_before_fullscreen = Qt.WindowState.WindowNoState
        self._was_fullscreen_for_video = False  # viewer was already full screen when the video went full screen

        # Dialogs get no maximize button by default
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.resize(1100, 720)
        self.setMinimumSize(800, 500)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._page: Optional[QWidget] = None
        self._init_ui()

    def show_target(self, target: ViewerTarget) -> None:
        """Switch to another file, keeping the window (and full screen) as it is."""
        self._cancel_load()
        self._cancel_uvs()
        if self.video_player is not None:
            self.video_player.set_fullscreen(False)
            self.video_player.stop()
            self.video_player = None
        if self.pdf_viewer is not None:
            self.pdf_viewer.save_state()
            self.pdf_viewer = None
        self.asset, self.tags, self._edit_tags, self._position = target.asset, target.tags, target.edit_tags, target.position
        self._file = None
        self._launch_after = None
        self._init_ui()
        self.setFocus()

    def go(self, step: int) -> None:
        """Show the previous (-1) or next (+1) file."""
        target = self._navigate(step) if self._navigate is not None else None
        if target is not None:
            self.show_target(target)

    def _init_ui(self) -> None:
        """(Re)build the page for self.asset."""
        self.setWindowTitle(f"Viewer - {self.asset.name}")
        page = QWidget()
        main_layout = QVBoxLayout(page)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        if self._page is None:
            self._layout.addWidget(page)
        else:
            self._layout.replaceWidget(self._page, page)
            self._page.deleteLater()
        self._page = page

        # 1. Top Header Bar
        self.header_bar = self._build_header_bar()
        main_layout.addWidget(self.header_bar)

        # 2. Main Content Splitter (Viewer + Metadata Inspector)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(2)

        self.viewport: Optional[Model3DViewportWidget] = None
        viewer_widget, metadata_dict = self._create_viewer_and_metadata()
        splitter.addWidget(viewer_widget)

        self._metadata = metadata_dict
        self.inspector_panel = self._build_inspector_panel(metadata_dict)
        splitter.addWidget(self.inspector_panel)
        self._splitter = splitter

        splitter.setSizes([840, 260])
        main_layout.addWidget(splitter, 1)

        # 3. Bottom Action Bar
        self.bottom_bar = self._build_bottom_bar()
        main_layout.addWidget(self.bottom_bar)

        # Enter in a text box (the PDF's page box, zoom box) is for that box: no button is the
        # dialog's default, so Enter never also presses one (it opened the tag editor)
        for button in page.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)

    def _build_header_bar(self) -> QWidget:
        header = QWidget()
        header.setObjectName("headerBar")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(12)

        # File name
        title = QLabel(self.asset.name)
        title.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
        title.setObjectName("viewerTitle")
        layout.addWidget(title)

        # Format Badge
        badge = QLabel(f" {self.asset.category_name} ")
        badge.setObjectName("formatBadge")
        layout.addWidget(badge)

        # Group & Size
        meta = QLabel(f"{self.asset.asset_group} • {self.asset.formatted_size}")
        meta.setObjectName("toolbarLabel")
        layout.addWidget(meta)

        layout.addStretch(1)

        icon_color = self.palette().color(QPalette.ColorRole.ButtonText)
        if self._navigate is not None:
            for step, kind, tip in ((-1, "back", "Previous file (Page Up or Alt+Left)"), (1, "forward", "Next file (Page Down or Alt+Right)")):
                button = QPushButton()
                button.setIcon(control_icon(kind, icon_color))
                button.setToolTip(tip)
                button.setFixedWidth(40)
                button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                button.clicked.connect(lambda _checked=False, s=step: self.go(s))
                layout.addWidget(button)
                if step < 0 and self._position:
                    position = QLabel(self._position)
                    position.setObjectName("toolbarLabel")
                    layout.addWidget(position)

        self._fullscreen_icons = (control_icon("fullscreen", icon_color), control_icon("exit_fullscreen", icon_color))
        fullscreen = self.isFullScreen()
        self.btn_fullscreen = QPushButton(" Exit Full Screen" if fullscreen else " Full Screen")
        self.btn_fullscreen.setIcon(self._fullscreen_icons[1 if fullscreen else 0])
        self.btn_fullscreen.setToolTip("Toggle full screen (F11)")
        self.btn_fullscreen.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_fullscreen.clicked.connect(self.toggle_fullscreen)
        layout.addWidget(self.btn_fullscreen)

        return header

    def _create_viewer_and_metadata(self) -> tuple[QWidget, dict[str, str]]:
        """Instantiate the specialized viewer widget and gather metadata dictionary."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        metadata: dict[str, str] = {
            "File Name": self.asset.name,
            "Type": self.asset.category_name,
            "Group": self.asset.asset_group,
            "Size": self.asset.formatted_size,
            "Modified": self.asset.formatted_modified_time,
            "Location": str(self.asset.path.parent),
        }
        self.btn_tile: Optional[QPushButton] = None  # (an image's tools: _add_image_tools)
        self.btn_uv: Optional[QPushButton] = None  # (a model's UVs: _add_uv_controls)
        self.btn_render: Optional[QPushButton] = None  # (a model from a file can be rendered)
        self._btn_open_app = self._app_status = None
        member = self.asset.archive
        if member is not None:
            metadata["Location"] = f"Inside {member.archive.name}" + (f" > {member.folder}" if member.folder else "")
            metadata["Archive"] = str(member.archive)
            if self._file is None:
                self._file = archives.cached_copy(member)  # (unpacked before: no waiting)
        path = self._file_path()

        if member is not None and self.asset.is_archive:
            lbl = QLabel("An archive inside this archive. Extract it (right-click it in the grid) to look inside.")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setWordWrap(True)
            layout.addWidget(lbl, 1)
        elif self.asset.is_archive:
            lbl = QLabel(f"{self.asset.name} is an archive. Double-click it in the grid to look inside.")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setWordWrap(True)
            layout.addWidget(lbl, 1)
        elif self.asset.opens_in_app and (path is not None or self._launch_after is None):
            layout.addWidget(self._build_app_file_panel(), 1)
            app, self._launch_after = self._launch_after, None
            if app is not None:  # (it was unpacked to be opened in this app)
                self._launch_in(app, path)
        elif path is None:
            layout.addWidget(self._build_unpack_panel(), 1)

        # Case A: 3D Model Viewer (loaded on a worker thread: a big sculpt takes seconds)
        elif self.asset.is_3d_model:
            viewport = Model3DViewportWidget()
            viewport.set_status(f"Loading {self.asset.name}...")
            self.viewport = viewport
            self._load_model(viewport)
            self._model_stack = QStackedWidget()  # (the model, and its UVs once they're asked for)
            self._model_stack.addWidget(viewport)
            layout.addWidget(self._build_model_toolbar(viewport, has_uvs=True))
            layout.addWidget(self._model_stack, 1)

        # Case B: Texture / Image Viewer (decoded on a worker thread: a big one takes seconds)
        elif self.asset.is_texture:
            tex_viewer = TextureViewerWidget(path)
            self.tex_viewer = tex_viewer
            self._load_image(tex_viewer)

            # Texture Control Toolbar
            t_toolbar = QWidget()
            t_toolbar.setObjectName("viewerToolbar")
            tb_layout = QHBoxLayout(t_toolbar)
            tb_layout.setContentsMargins(8, 4, 8, 4)
            tb_layout.setSpacing(8)

            btn_fit = QPushButton("Fit Screen")
            btn_fit.setToolTip("Fit the whole image in the window")
            btn_fit.clicked.connect(tex_viewer.fit_to_screen)
            tb_layout.addWidget(btn_fit)

            btn_100 = QPushButton("100% (1:1)")
            btn_100.setToolTip("Show the image at its real size: one image pixel per screen pixel")
            btn_100.clicked.connect(tex_viewer.set_actual_size)
            tb_layout.addWidget(btn_100)

            btn_checker = QPushButton("Checkerboard")
            btn_checker.setCheckable(True)
            btn_checker.setChecked(tex_viewer.show_checkerboard)
            self._btn_checker = btn_checker  # (on once the image turns out to have transparency)
            btn_checker.setToolTip("Check Transparency")
            btn_checker.toggled.connect(tex_viewer.toggle_checkerboard)
            tb_layout.addWidget(btn_checker)
            self._add_image_tools(t_toolbar, tb_layout, tex_viewer, path)

            tb_layout.addStretch(1)
            hint_lbl = QLabel("Scroll to zoom • Drag to pan")
            hint_lbl.setObjectName("hintLabel")
            tb_layout.addWidget(hint_lbl)

            layout.addWidget(t_toolbar)
            # The flat image, and (an alpha's, once asked for) its 3D surface
            self._image_stack = QStackedWidget()
            self._image_stack.addWidget(tex_viewer)
            layout.addWidget(self._image_stack, 1)

        # Case C: Document (PDF) Viewer
        elif self.asset.is_document:
            self.pdf_viewer = PdfViewer(path, artbook=self._artbook)
            metadata["Pages"] = str(self.pdf_viewer.doc.pageCount())
            layout.addWidget(self.pdf_viewer, 1)

        # Case D: Video Player
        elif self.asset.is_video:
            self.video_player = VideoPlayer()
            self.video_player.fullscreen_changed.connect(self._on_video_fullscreen)
            layout.addWidget(self.video_player, 1)
            self.video_player.load(path, autoplay=True)
            self.video_player.setFocus()

        # Fallback
        else:
            lbl = QLabel(f"Preview not supported for {self.asset.extension}")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(lbl, 1)

        return container, metadata

    # ------------------------------------------------------------------
    # 3D models
    # ------------------------------------------------------------------
    def _next_load(self) -> tuple[int, threading.Event]:
        """Start a new background load: the previous one's result is dropped, and a model still
        being read stops (it would hold GBs for minutes after the viewer moved on)."""
        self._cancel_load()
        self._load_stop = threading.Event()
        return self._load_token, self._load_stop

    def _cancel_load(self) -> None:
        self._load_token = getattr(self, "_load_token", 0) + 1
        stop = getattr(self, "_load_stop", None)
        if stop is not None:
            stop.set()

    def _load_image(self, tex_viewer: TextureViewerWidget) -> None:
        """Decode the image on a worker thread; show it (and its size) when it's ready."""
        token, _stop = self._next_load()
        path = self._file_path()

        def done(loaded: Optional[LoadedImage]) -> None:
            if loaded is None or token != self._load_token or not shiboken6.isValid(tex_viewer):
                return  # (another file is shown now, or the viewer was closed)
            tex_viewer.set_image(QPixmap.fromImage(loaded.image), loaded.has_alpha)
            self._btn_checker.setChecked(loaded.has_alpha)
            w, h = loaded.size
            gcd_val = math.gcd(w, h)
            self._metadata.update({
                "Resolution": f"{w} × {h} px",
                "Aspect Ratio": f"{w // gcd_val}:{h // gcd_val}" if gcd_val > 0 else "N/A",
                "Color Depth": f"{tex_viewer.pixmap.depth()}-bit",
                "Transparency": "Yes" if loaded.has_alpha else "No",
            })
            if (loaded.image.width(), loaded.image.height()) != (w, h):
                self._metadata["Shown At"] = f"{loaded.image.width()} × {loaded.image.height()} px (big images are shown smaller)"
            self._refresh_inspector()

        def failed(message: str) -> None:
            if token == self._load_token and shiboken6.isValid(tex_viewer):
                tex_viewer.set_message(f"This image couldn't be opened.\n{message}")

        run_job(lambda: load_full_image(path, still_wanted=lambda: token == self._load_token), done, failed)

    def _load_model(self, viewport: Model3DViewportWidget) -> None:
        """Read the model on a worker thread; show it (and its details) when it's ready."""
        token, stop = self._next_load()
        path = self._file_path()

        def done(mesh: Mesh3D) -> None:
            if token != self._load_token or not shiboken6.isValid(viewport):
                return  # (another file is shown now, or the viewer was closed)
            if self._model_viewed is not None:
                self._model_viewed(self.asset, mesh)
            viewport.set_mesh(mesh)
            self._metadata.update(mesh.metadata)
            self._refresh_inspector()
            self._update_render_button()

        def failed(message: str) -> None:
            if token == self._load_token and shiboken6.isValid(viewport):
                viewport.set_status(f"This model couldn't be opened.\n{message}")

        run_job(lambda: load_3d_model(path, should_stop=stop.is_set), done, failed)

    def _refresh_inspector(self) -> None:
        sizes = self._splitter.sizes()
        old = self.inspector_panel
        self.inspector_panel = self._build_inspector_panel(self._metadata)
        self._splitter.replaceWidget(1, self.inspector_panel)
        old.deleteLater()
        self._splitter.setSizes(sizes)

    def _build_model_toolbar(self, viewport: Model3DViewportWidget, has_uvs: bool = False) -> QWidget:
        """Shading, material, colour, lighting, what dragging turns, and (a model from a file) its UVs."""
        v_toolbar = QWidget()
        v_toolbar.setObjectName("viewerToolbar")
        rows = QVBoxLayout(v_toolbar)
        rows.setContentsMargins(8, 4, 8, 4)
        rows.setSpacing(4)
        tb_layout = QHBoxLayout()
        tb_layout.setSpacing(8)
        rows.addLayout(tb_layout)
        tools = QHBoxLayout()  # (a second row: one is too wide for the window, and squeezes the details beside it)
        tools.setSpacing(8)
        rows.addLayout(tools)

        cb_mode = QComboBox()
        cb_mode.addItems(["Shaded", "Wireframe", "Shaded + Wireframe", "Vertices"])
        cb_mode.setToolTip("How the model is drawn (W cycles shaded / wireframe / both)")
        cb_mode.currentTextChanged.connect(viewport.set_render_mode)
        tb_layout.addWidget(cb_mode)

        self.cmb_material = QComboBox()
        for material_id, material in MATERIALS.items():
            self.cmb_material.addItem(material.label, material_id)
            self.cmb_material.setItemData(self.cmb_material.count() - 1, material.tip, Qt.ItemDataRole.ToolTipRole)
        self.cmb_material.setCurrentIndex(self.cmb_material.findData(viewport.material_id))
        self.cmb_material.setToolTip("The material: how the surface takes the light")
        tb_layout.addWidget(self.cmb_material)

        self.btn_model_color = QPushButton()
        self.btn_model_color.setToolTip("The model's colour (remembered for every model)")
        self.btn_model_color.setIconSize(QSize(18, 18))
        self.btn_model_color.setIcon(_swatch_icon(viewport.color))
        self.btn_model_color.clicked.connect(lambda: self._pick_model_color(viewport))
        tb_layout.addWidget(self.btn_model_color)

        self.cmb_lighting = QComboBox()
        self._look_details = QWidget(v_toolbar)  # (what applies to the look now: none of it to UVs)
        self.lighting_controls = LightingControls(viewport, self.cmb_lighting, self._look_details, self._hdri_assets)
        tb_layout.addWidget(self.cmb_lighting)

        self.btn_drag_lights = QPushButton("Turn Lights")
        set_icon(self.btn_drag_lights, "light")
        self.btn_drag_lights.setCheckable(True)
        self.btn_drag_lights.setToolTip(
            "On: dragging turns the lights or the HDRI round the model. Off: it turns the model (L; or hold Shift while dragging)"
        )
        self.btn_drag_lights.toggled.connect(viewport.set_drag_lights)
        viewport.drag_lights_changed.connect(self.btn_drag_lights.setChecked)
        tb_layout.addWidget(self.btn_drag_lights)

        btn_grid = QPushButton("Grid")
        set_icon(btn_grid, "grid")
        btn_grid.setCheckable(True)
        btn_grid.setChecked(True)
        btn_grid.setToolTip("Toggle the ground grid (G)")
        btn_grid.toggled.connect(viewport.toggle_ground_grid)
        tb_layout.addWidget(btn_grid)

        btn_turntable = QPushButton("Auto Rotate")
        set_icon(btn_turntable, "play")
        btn_turntable.setToolTip("Spin the model slowly to see it from every side")
        btn_turntable.setCheckable(True)

        def on_turntable_toggled(checked: bool):
            viewport.toggle_turntable(checked)
            btn_turntable.setText("Pause" if checked else "Auto Rotate")
            set_icon(btn_turntable, "pause" if checked else "play")

        btn_turntable.toggled.connect(on_turntable_toggled)
        tb_layout.addStretch(1)
        tools.addWidget(btn_turntable)

        btn_reset = QPushButton("Reset View")
        set_icon(btn_reset, "refresh")
        btn_reset.setToolTip("Reset the camera and the lights (R resets the camera)")
        btn_reset.clicked.connect(viewport.reset_camera)
        btn_reset.clicked.connect(viewport.reset_lights)
        tools.addWidget(btn_reset)

        details = QHBoxLayout(self._look_details)
        details.setContentsMargins(0, 0, 0, 0)
        details.setSpacing(16)
        self.roughness_bar = RoughnessBar(viewport, self._look_details)  # (shows on metal)
        details.addWidget(self.roughness_bar)
        details.addWidget(self.lighting_controls.bar)  # (shows while an HDRI is in use)
        details.addStretch(1)
        tools.addStretch(1)
        rows.addWidget(self._look_details)  # (a row of its own: with the tools, it squeezes the details panel beside it)
        material_combo = self.cmb_material

        def material_picked(_index: int) -> None:
            viewport.set_material(material_combo.currentData())
            self.roughness_bar.show_for_material()
            self._update_render_button()

        material_combo.currentIndexChanged.connect(material_picked)
        self.roughness_bar.show_for_material()

        self.btn_uv = self.btn_render = None
        if has_uvs:
            self.btn_render = QPushButton("Render")
            set_icon(self.btn_render, "render")
            self.btn_render.clicked.connect(self._open_render_window)
            tools.insertWidget(0, self.btn_render)
            self._add_uv_controls(tools, btn_reset, v_toolbar,
                                  [cb_mode, material_combo, self.btn_model_color, self.cmb_lighting, self.btn_drag_lights, btn_grid, btn_turntable,
                                   self.btn_render])
            self._update_render_button()
        return v_toolbar

    # ------------------------------------------------------------------
    # Rendering (ui.render_window)
    # ------------------------------------------------------------------
    def _render_refusal(self) -> str:
        """Why the model can't be rendered as it's shown now ("": it can)."""
        viewport = self.viewport
        if viewport is None or viewport.mesh is None or viewport.face_count == 0:
            return "Once the model has loaded, you can render it"
        if viewport.mesh.stand_in:
            return "This model couldn't be read"
        unrenderable = UNRENDERABLE.get(viewport.material_id)
        return f"{unrenderable} isn't a real-world material, so it can't be rendered: pick another" if unrenderable else ""

    def _update_render_button(self) -> None:
        if self.btn_render is None:
            return
        refusal = self._render_refusal()
        uv_shown = self.btn_uv is not None and self.btn_uv.isChecked()
        self.btn_render.setEnabled(not refusal and not uv_shown)
        self.btn_render.setToolTip(refusal or "Render what's in the viewer, with real light and cast shadows (F12)")

    def _open_render_window(self) -> None:
        if self._render_refusal() or self.viewport is None or self._file_path() is None:
            return
        window = RenderWindow(self.viewport, self._file_path(), self.asset.name, self)
        window.exec()
        window.deleteLater()

    # ------------------------------------------------------------------
    # A model's UVs (core.model_uvs): read the first time they're asked for
    # ------------------------------------------------------------------
    def _add_uv_controls(self, row: QHBoxLayout, btn_reset: QPushButton, toolbar: QWidget, model_only: list[QWidget]) -> None:
        self.uv_view: Optional[UvViewWidget] = None
        self._uv_stop: Optional[threading.Event] = None
        self._uv_set_names: list[str] = []
        self._model_only_controls = model_only
        self.btn_uv = QPushButton("UV")
        set_icon(self.btn_uv, "uv")
        self.btn_uv.setCheckable(True)
        self.btn_uv.setToolTip("See the model's UV layout: its polygons flat, with the UDIM tiles they're in (U)")
        self.btn_uv.toggled.connect(self._show_uvs)
        row.insertWidget(0, self.btn_uv)
        self.cmb_uv_set = QComboBox(toolbar)
        self.cmb_uv_set.setToolTip("Which of the model's UV sets to show")
        self.cmb_uv_set.hide()
        self.cmb_uv_set.activated.connect(self._read_uvs)
        row.insertWidget(row.count() - 1, self.cmb_uv_set)  # (before the stretch)
        btn_reset.clicked.connect(self._fit_uvs)

    def _show_uvs(self, on: bool) -> None:
        """Between the model and its UVs (read the first time)."""
        for control in self._model_only_controls:
            control.setEnabled(not on)
        self._look_details.setVisible(not on)
        if on:
            if self.uv_view is None:
                self.uv_view = UvViewWidget()
                self._model_stack.addWidget(self.uv_view)
                self._read_uvs(0)
            self._model_stack.setCurrentWidget(self.uv_view)
            self.uv_view.setFocus()
        else:
            self._model_stack.setCurrentWidget(self.viewport)
            self.viewport.setFocus()
        self.cmb_uv_set.setVisible(on and len(self._uv_set_names) > 1)
        self._update_render_button()

    def _fit_uvs(self) -> None:
        if self.uv_view is not None:
            self.uv_view.fit()

    def _cancel_uvs(self) -> None:
        self._uv_token = getattr(self, "_uv_token", 0) + 1
        stop = getattr(self, "_uv_stop", None)
        if stop is not None:
            stop.set()

    def _read_uvs(self, set_index: int) -> None:
        """Read the UV set on a worker thread; show it (and the tiles it's in) when it's ready."""
        self._cancel_uvs()
        token, view, path = self._uv_token, self.uv_view, self._file_path()
        stop = self._uv_stop = threading.Event()
        view.set_status("Reading the UVs...")

        def done(layout: Optional[UvLayout]) -> None:
            if token != self._uv_token or not shiboken6.isValid(view):
                return  # (another file is shown now, or the viewer was closed)
            if layout is None:
                view.set_status("This model has no UVs.")
                return
            view.set_layout(layout)
            self._uv_loaded(layout)

        def failed(message: str) -> None:
            if token == self._uv_token and shiboken6.isValid(view):
                view.set_status(f"The UVs couldn't be shown.\n{message}")

        def work() -> Optional[UvLayout]:
            try:
                return read_uvs(path, set_index, stop.is_set)
            except LoadCancelled:
                return None  # (nobody wants them any more: done() sees the token has moved on)

        run_job(work, done, failed)

    def _uv_loaded(self, layout: UvLayout) -> None:
        self._metadata["UV Tiles"] = tiles_text(layout)
        self._uv_set_names = layout.set_names
        combo = self.cmb_uv_set
        if len(layout.set_names) > 1:
            self._metadata["UV Set"] = f"{layout.set_names[layout.set_index]} ({layout.set_index + 1} of {len(layout.set_names)})"
            combo.clear()
            combo.addItems([f"{number + 1}: {name}" for number, name in enumerate(layout.set_names)])  # (sets can share a name)
            combo.setCurrentIndex(layout.set_index)
        combo.setVisible(len(layout.set_names) > 1 and self.btn_uv.isChecked())
        self._refresh_inspector()

    # ------------------------------------------------------------------
    # Images: Tile, and an alpha's 3D surface (core.alphas)
    # ------------------------------------------------------------------
    def _add_image_tools(self, toolbar: QWidget, layout: QHBoxLayout, tex_viewer: TextureViewerWidget, path: Path) -> None:
        """Tile (2 x 2 / 3 x 3) for every image; 3D for an alpha (shown once it's known to be one)."""
        self._alpha_page: Optional[QWidget] = None
        self._alpha_viewport: Optional[Model3DViewportWidget] = None
        self._alpha_heights: dict[int, np.ndarray] = {}  # read heights, by plane size (rebuilt from them)
        self._alpha_token = 0
        view = get_model_view()

        self.btn_tile = QPushButton("Tile")
        set_icon(self.btn_tile, "tile")
        self.btn_tile.setCheckable(True)
        self.btn_tile.setToolTip("Show the image repeated, edge to edge, to check its seams (T)")
        layout.addWidget(self.btn_tile)
        self.tile_choice = QButtonGroup(toolbar)
        for tiles in TILE_GRIDS:
            button = QPushButton(f"{tiles}×{tiles}")
            button.setCheckable(True)
            button.setChecked(tiles == view["tile"])
            button.setToolTip(f"Tile {tiles} × {tiles}")
            self.tile_choice.addButton(button, tiles)
            layout.addWidget(button)

        def apply_tiles() -> None:
            tex_viewer.set_tiles(self.tile_choice.checkedId() if self.btn_tile.isChecked() else 1)

        def tiles_picked(tiles: int) -> None:
            set_model_view(tile=tiles)
            if self.btn_tile.isChecked():
                apply_tiles()
            else:
                self.btn_tile.setChecked(True)  # (picking a grid turns Tile on)

        self.btn_tile.toggled.connect(lambda _checked: apply_tiles())
        self.tile_choice.idClicked.connect(tiles_picked)

        self.btn_3d = QPushButton("3D")
        set_icon(self.btn_3d, "cube")
        self.btn_3d.setCheckable(True)
        self.btn_3d.setToolTip("See this alpha as a 3D surface, with the 3D viewer's materials and lights (3)")
        self.btn_3d.toggled.connect(self._show_alpha_3d)
        self.btn_3d.hide()
        layout.addWidget(self.btn_3d)

        asset, name = self.asset, self.asset.name

        def detected(is_alpha: bool) -> None:
            if is_alpha and self.asset is asset and shiboken6.isValid(self.btn_3d):
                self.btn_3d.show()

        run_job(lambda: alphas.detect(path, name), detected)

    def _show_alpha_3d(self, on: bool) -> None:
        """Switch between the flat image and the alpha's 3D surface (made the first time)."""
        if on:
            if self._alpha_page is None:
                self._alpha_page = self._build_alpha_page()
                self._image_stack.addWidget(self._alpha_page)
                self._load_alpha_mesh()
            self._image_stack.setCurrentWidget(self._alpha_page)
            self._alpha_viewport.setFocus()
        else:
            self._image_stack.setCurrentWidget(self.tex_viewer)
            self.tex_viewer.setFocus()

    def _build_alpha_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        viewport = Model3DViewportWidget()
        viewport.set_status(f"Making a 3D surface of {self.asset.name}...")
        self._alpha_viewport = viewport

        bar = QWidget()
        bar.setObjectName("viewerToolbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 4, 8, 4)
        row.setSpacing(8)
        label = QLabel("Height")
        label.setObjectName("toolbarLabel")
        row.addWidget(label)
        self.slider_alpha_height = QSlider(Qt.Orientation.Horizontal)
        self.slider_alpha_height.setRange(1, 60)
        self.slider_alpha_height.setValue(15)  # (x 1/100 of the longest side)
        self.slider_alpha_height.setFixedWidth(160)
        self.slider_alpha_height.setToolTip("How high the white parts rise")
        row.addWidget(self.slider_alpha_height)
        self.btn_alpha_invert = QPushButton("Invert")
        set_icon(self.btn_alpha_invert, "invert")
        self.btn_alpha_invert.setCheckable(True)
        self.btn_alpha_invert.setToolTip("Turn the heights upside down: for alphas made the other way round (I)")
        row.addWidget(self.btn_alpha_invert)
        self.alpha_detail = QButtonGroup(bar)
        for side in ALPHA_PLANE_SIDES:
            button = QPushButton(f"{side} px")
            button.setCheckable(True)
            button.setChecked(side == get_model_view()["alpha_plane"])
            button.setToolTip(
                f"Detail: {side} points along the longest side "
                + ("(smooth and instant)" if side == ALPHA_PLANE_SIDES[0] else "(finer, for close looks: about 2 million triangles)")
            )
            self.alpha_detail.addButton(button, side)
            row.addWidget(button)
        row.addStretch(1)

        rebuild = QTimer(page)  # (a slider drag rebuilds once it pauses, not on every step)
        rebuild.setSingleShot(True)
        rebuild.setInterval(120)
        rebuild.timeout.connect(self._load_alpha_mesh)
        self.slider_alpha_height.valueChanged.connect(lambda _value: rebuild.start())
        self.btn_alpha_invert.toggled.connect(lambda _checked: self._load_alpha_mesh())

        def detail_picked(side: int) -> None:
            set_model_view(alpha_plane=side)
            self._load_alpha_mesh()

        self.alpha_detail.idClicked.connect(detail_picked)
        layout.addWidget(bar)
        layout.addWidget(self._build_model_toolbar(viewport))
        layout.addWidget(viewport, 1)
        return page

    def _load_alpha_mesh(self) -> None:
        """(Re)build the 3D surface on a worker thread: its heights are read once per detail size."""
        viewport = self._alpha_viewport
        if viewport is None:
            return
        self._alpha_token += 1
        token, asset, path = self._alpha_token, self.asset, self._file_path()
        side = self.alpha_detail.checkedId()
        strength, invert = self.slider_alpha_height.value() / 100.0, self.btn_alpha_invert.isChecked()
        known = self._alpha_heights.get(side)

        def work():
            height = known if known is not None else alphas.read_height(path, asset.name, side)
            if height is None:
                raise ValueError("It isn't a grey height image.")
            return side, height, alphas.height_mesh(height, strength, invert, asset.name)

        def done(result) -> None:
            if token != self._alpha_token or self.asset is not asset or not shiboken6.isValid(viewport):
                return  # (a newer one is on its way, or another file is shown)
            made_side, height, mesh = result
            self._alpha_heights[made_side] = height
            viewport.set_mesh(mesh)

        def failed(message: str) -> None:
            if token == self._alpha_token and shiboken6.isValid(viewport):
                viewport.set_status(f"No 3D surface could be made.\n{message}")

        run_job(work, done, failed)

    def _pick_model_color(self, viewport: Model3DViewportWidget) -> None:
        """The Artbook's colour picker: the model follows it as you drag."""
        from core.model_look import DEFAULT_COLOR
        from ui.color_popup import ColorPopup

        popup = ColorPopup("Model Colour", viewport.color, self)

        def changed(color: QColor) -> None:
            viewport.set_color(color, remember=False)
            self.btn_model_color.setIcon(_swatch_icon(viewport.color))

        popup.changed.connect(changed)
        popup.default_picked.connect(lambda: changed(QColor(DEFAULT_COLOR)))
        popup.finished.connect(lambda: shiboken6.isValid(viewport) and viewport.set_color(viewport.color))  # (kept for next time)
        button = self.btn_model_color
        popup.show_beside(QRect(button.mapToGlobal(QPoint(0, 0)), button.size()))

    def _build_inspector_panel(self, metadata: dict[str, str]) -> QWidget:
        """Construct sidebar listing metadata details."""
        panel = QWidget()
        panel.setObjectName("infoPanel")
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        # (it scrolls: a model's details and the file's tags are taller than a small window)
        scroll = QScrollArea()
        scroll.setObjectName("infoPanelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        contents = QWidget()
        contents.setObjectName("infoPanelContents")
        layout = QVBoxLayout(contents)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        scroll.setWidget(contents)
        outer.addWidget(scroll)

        header = QLabel("METADATA INSPECTOR")
        header.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        header.setObjectName("inspectorHeader")
        layout.addWidget(header)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setObjectName("inspectorLine")
        layout.addWidget(line)

        for key, val in metadata.items():
            prop_box = QVBoxLayout()
            prop_box.setSpacing(2)

            k_lbl = QLabel(key.upper())
            k_lbl.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            k_lbl.setObjectName("inspectorKey")

            v_lbl = QLabel(str(val))
            v_lbl.setWordWrap(True)
            v_lbl.setObjectName("inspectorValue")

            prop_box.addWidget(k_lbl)
            prop_box.addWidget(v_lbl)
            layout.addLayout(prop_box)

        if self.tags is not None:
            layout.addWidget(self._build_tags_section())

        layout.addStretch(1)
        return panel

    def _build_tags_section(self) -> QWidget:
        section = QWidget()
        box = QVBoxLayout(section)
        box.setContentsMargins(0, 6, 0, 0)
        box.setSpacing(4)
        k_lbl = QLabel("TAGS")
        k_lbl.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        k_lbl.setObjectName("inspectorKey")
        box.addWidget(k_lbl)
        self._tag_cloud_holder = QVBoxLayout()
        self._tag_cloud_holder.setContentsMargins(0, 0, 0, 0)
        self._tag_cloud_holder.addWidget(tag_chip_cloud(self.tags))
        box.addLayout(self._tag_cloud_holder)
        if self._edit_tags is not None:
            btn = QPushButton("Edit Tags...")
            btn.setToolTip("Add or remove custom tags on this file")
            btn.clicked.connect(self._on_edit_tags)
            box.addWidget(btn, 0, Qt.AlignmentFlag.AlignLeft)
        return section

    def _on_edit_tags(self) -> None:
        updated = self._edit_tags()
        if updated is None:
            return
        self.tags = updated
        old = self._tag_cloud_holder.takeAt(0).widget()
        old.deleteLater()
        self._tag_cloud_holder.addWidget(tag_chip_cloud(updated))

    def _build_bottom_bar(self) -> QWidget:
        """Construct bottom action bar."""
        bar = QWidget()
        bar.setObjectName("bottomBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(10)

        btn_open = QPushButton("Open in External App")
        btn_open.setToolTip("Open the file in the program Windows uses for it")
        btn_open.clicked.connect(self._open_external)
        layout.addWidget(btn_open)

        btn_explorer = QPushButton("Show in File Explorer")
        btn_explorer.setToolTip("Open its folder in File Explorer with the file selected")
        btn_explorer.clicked.connect(self._reveal_in_explorer)
        layout.addWidget(btn_explorer)

        btn_copy = QPushButton("Copy Path")
        btn_copy.setToolTip("Copy the file's full path to the clipboard")
        btn_copy.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(str(self.asset.path))
        )
        layout.addWidget(btn_copy)

        layout.addStretch(1)

        btn_close = QPushButton("Close")
        btn_close.setToolTip("Close the viewer (Esc)")
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close)

        return bar

    # ------------------------------------------------------------------
    # Full screen
    # ------------------------------------------------------------------
    def toggle_fullscreen(self) -> None:
        """Whole viewer (header, inspector and actions included) full screen, or back."""
        if self.isFullScreen():
            self._leave_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self) -> None:
        if not self.isFullScreen():
            self._state_before_fullscreen = self.windowState() & ~Qt.WindowState.WindowFullScreen
            self.showFullScreen()
        self.btn_fullscreen.setText(" Exit Full Screen")
        self.btn_fullscreen.setIcon(self._fullscreen_icons[1])

    def _leave_fullscreen(self) -> None:
        self.btn_fullscreen.setText(" Full Screen")
        self.btn_fullscreen.setIcon(self._fullscreen_icons[0])
        if self._state_before_fullscreen & Qt.WindowState.WindowMaximized:
            self.showMaximized()
        else:
            self.showNormal()

    def _on_video_fullscreen(self, enabled: bool) -> None:
        """Video-only full screen: hide the viewer's own chrome around the player."""
        for widget in (self.header_bar, self.inspector_panel, self.bottom_bar):
            widget.setVisible(not enabled)
        if enabled:
            self._was_fullscreen_for_video = self.isFullScreen()
            self._enter_fullscreen()
        elif not self._was_fullscreen_for_video:
            self._leave_fullscreen()

    def keyPressEvent(self, event) -> None:
        if self.btn_tile is not None and self._image_keys(event):
            return
        if shortcuts.matches("viewer.previous", event) or shortcuts.matches("viewer.previous_alt", event):
            self.go(-1)
        elif shortcuts.matches("viewer.next", event) or shortcuts.matches("viewer.next_alt", event):
            self.go(1)
        elif shortcuts.matches("viewer.fullscreen", event):
            if self.video_player is not None and self.video_player.is_fullscreen():
                self.video_player.set_fullscreen(False)
            else:
                self.toggle_fullscreen()
        elif shortcuts.matches("viewer.exit_fullscreen", event) and self.isFullScreen():
            # Esc leaves full screen first instead of closing the viewer
            if self.video_player is not None and self.video_player.is_fullscreen():
                self.video_player.set_fullscreen(False)
            else:
                self._leave_fullscreen()
        elif shortcuts.matches("viewer.open_app", event) and self._btn_open_app is not None:
            self._btn_open_app.click()
        elif shortcuts.matches("viewer3d.uv", event) and self.btn_uv is not None:
            self.btn_uv.toggle()
        elif shortcuts.matches("viewer3d.render", event) and self.btn_render is not None and self.btn_render.isEnabled():
            self._open_render_window()
        else:
            super().keyPressEvent(event)

    def _image_keys(self, event) -> bool:
        """T, 3 and I on an image (True if the key was one of them)."""
        if shortcuts.matches("image.tile", event):
            self.btn_tile.toggle()
        elif shortcuts.matches("image.view_3d", event) and self.btn_3d.isVisible():
            self.btn_3d.toggle()
        elif shortcuts.matches("image.invert", event) and self.btn_3d.isChecked():
            self.btn_alpha_invert.toggle()
        else:
            return False
        return True

    def done(self, result: int) -> None:
        """Stop playback and release the video file however the dialog closes."""
        self._cancel_load()  # (a model still being read stops)
        self._cancel_uvs()
        if self.video_player is not None:
            self.video_player.stop()
        if self.pdf_viewer is not None:
            self.pdf_viewer.save_state()
        super().done(result)

    def _open_external(self) -> None:
        """Open asset in system default application (a file inside an archive: its unpacked copy)."""
        if self.video_player is not None:
            self.video_player.player.pause()
        path = self._file_path()
        if path is not None and path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _reveal_in_explorer(self) -> None:
        """Reveal file in Windows File Explorer (a file inside an archive: the archive)."""
        file_actions.reveal(self.asset.archive.archive if self.asset.in_archive else self.asset.path)

    # ------------------------------------------------------------------
    # Files only their apps can show
    # ------------------------------------------------------------------
    def _build_app_file_panel(self) -> QWidget:
        """Its picture (or its app's icon), and buttons that open it. Going through the grid with the
        viewer lands on these too, so nothing opens until a button is pressed."""
        asset = self.asset
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setSpacing(14)
        layout.addStretch(1)
        picture = QLabel()
        picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(picture)
        row = QHBoxLayout()
        row.addStretch(1)
        buttons: list[QPushButton] = []
        if asset.extension.lower() == ".pur":
            button = QPushButton("Open in Artbook")
            button.setToolTip("Open this PureRef board as an Artbook page, the same page each time (O)")
            button.clicked.connect(self._open_board)
            buttons.append(button)
        for app in apps.apps_for(asset.extension):
            button = QPushButton(f"Open in {app.name}")
            unpack = "Unpacks it first, then opens it" if asset.in_archive else "Open it"
            button.setToolTip(f"{unpack} in {app.name}" + (" (O)" if not buttons else ""))
            button.clicked.connect(lambda _checked=False, a=app: self._open_in_app(a))
            buttons.append(button)
        for button in buttons:
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        self._btn_open_app = buttons[0] if buttons else None
        message = "" if buttons else f"No app for {asset.category_name} files was found. Settings > Apps can point to one."
        self._app_status = QLabel(message)
        self._app_status.setObjectName("hintLabel")
        self._app_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._app_status.setWordWrap(True)
        layout.addWidget(self._app_status)
        layout.addStretch(2)
        self._load_app_file_picture(picture)
        return panel

    def _load_app_file_picture(self, label: QLabel) -> None:
        token, _stop = self._next_load()
        asset = self.asset
        relief = get_model_view()["alpha_relief"]

        def work() -> Optional[QImage]:
            try:
                picture = load_app_file_picture(asset, APP_FILE_PICTURE_SIDE, relief)
            except Exception:  # pylint: disable=broad-except
                picture = None  # (a picture is a bonus here: the buttons still work)
            return picture if picture is not None else app_icons.icon_for(asset.extension)

        def done(image: Optional[QImage]) -> None:
            if image is not None and token == self._load_token and shiboken6.isValid(label):
                label.setPixmap(QPixmap.fromImage(image))

        run_job(work, done)

    def _open_board(self) -> None:
        self.board_wanted = True
        self.accept()

    def _open_in_app(self, app: apps.App) -> None:
        path = self._file_path()
        if path is not None:
            self._launch_in(app, path)
            return
        self._launch_after = app  # (shown again once it's unpacked, and opened then)
        self._init_ui()

    def _launch_in(self, app: apps.App, path: Path) -> None:
        try:
            sent = apps.launch(app, path)
        except apps.AppError as exc:
            self._app_status.setText(str(exc))
            return
        if sent:
            self._app_status.setText(f"Sent to the {app.name} that's open: it imports it as soon as it's free.")
            return
        copy = " It's an unpacked copy: save it somewhere else to keep any changes." if self.asset.in_archive else ""
        self._app_status.setText(f"Opened in {app.name}.{copy}")

    # ------------------------------------------------------------------
    # Files inside archives
    # ------------------------------------------------------------------
    def _file_path(self) -> Optional[Path]:
        """The file to show: the asset's own, or for a file inside an archive its unpacked copy
        (None until it's unpacked)."""
        return self._file if self.asset.in_archive else self.asset.path

    def _build_unpack_panel(self) -> QWidget:
        """Shown while a file inside an archive is unpacked; the file is shown when it's done."""
        member = self.asset.archive
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addStretch(1)
        label = QLabel(f"Unpacking {member.base_name} from {member.archive.name}...")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        layout.addWidget(label)
        bar = QProgressBar()
        bar.setRange(0, 1000)
        bar.setTextVisible(False)
        bar.setMaximumWidth(420)
        layout.addWidget(bar, 0, Qt.AlignmentFlag.AlignHCenter)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.setToolTip("Stop unpacking (the next file can still be shown)")
        btn_cancel.setMaximumWidth(120)
        layout.addWidget(btn_cancel, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(2)

        token, stop = self._next_load()
        job = UnpackJob()
        stop_check = stop.is_set

        def work(progress, should_stop):
            return archives.materialize(member, progress, lambda: should_stop() or stop_check())

        def progress(done: int, total: int) -> None:
            if token == self._load_token and shiboken6.isValid(bar):
                bar.setValue(int(1000 * done / total) if total else 1000)
                label.setText(
                    f"Unpacking {member.base_name} from {member.archive.name}... {int(100 * done / total) if total else 100}%"
                )

        def done(path: Path) -> None:
            if token != self._load_token or not shiboken6.isValid(panel):
                return  # (another file is shown now, or the viewer was closed)
            self._file = path
            self.unpacked.add(str(self.asset.path))
            self._init_ui()

        def failed(message: str) -> None:
            if token == self._load_token and shiboken6.isValid(label):
                label.setText(f"{member.base_name} couldn't be unpacked.\n{message}")
                bar.hide()
                btn_cancel.hide()

        def cancel() -> None:
            job.cancel()
            stop.set()
            label.setText(f"Stopped unpacking {member.base_name}.")
            bar.hide()
            btn_cancel.hide()

        btn_cancel.clicked.connect(cancel)
        unpack_in_background(work, done, failed, progress, job)
        return panel

