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
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

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
    QImageReader,
    QMatrix4x4,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QPolygonF,
    QVector3D,
)
from PySide6.QtOpenGL import QOpenGLShader, QOpenGLShaderProgram
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QStackedWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from core.model_loader import Mesh3D, load_3d_model
from core.model_look import ENV_HIGH, ENV_LOW, LIGHTINGS, MATERIALS, MAX_LIGHTS, Lighting, Material, light_directions
from core.settings import get_model_view, set_model_view
from core.scanner import AssetItem
from core.tag_repository import Tag
from ui import shortcuts
from ui.duotone import set_icon
from ui.pdf_viewer import PdfViewer
from ui.tag_widgets import run_job, tag_chip_cloud
from ui.video_player import VideoPlayer, control_icon


# ----------------------------------------------------------------------
# 3D Model Viewport (Hardware-Accelerated OpenGL)
# ----------------------------------------------------------------------
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

# Lighting in view space (lights stay put while the model turns). The same sums as
# core.model_look.shade, which renders thumbnails: keep the two alike.
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

out vec4 FragColor;

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
        vec3 r = reflect(-v, n);
        vec3 env = mix(uEnvLow, uEnvHigh, r.y * 0.5 + 0.5);
        color += uColor * env * 0.9 * uMetal;
    }
    color += uRim * pow(1.0 - facing, 3.0) * specColor;
    if (uBands > 0.5 && facing < 0.22) color *= 0.35;
    FragColor = vec4(clamp(color, 0.0, 1.0), 1.0);
}
""" % {"lights": MAX_LIGHTS}

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
        self.light_yaw: float = 0.0  # the lights turned round the model (degrees)
        self.light_pitch: float = 0.0
        self.drag_lights: bool = False  # left-drag turns the lights instead of the model
        self.status_text: str = ""  # shown instead of the model (e.g. "Loading...")

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
        self._gl_ready = False
        self._needs_upload = False
        if mesh is not None:
            self.set_mesh(mesh)

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
        return MATERIALS[self.material_id]

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

    def set_lighting(self, lighting_id: str) -> None:
        if lighting_id in LIGHTINGS:
            self.lighting_id = lighting_id
            set_model_view(lighting=lighting_id)
            self.update()

    def set_drag_lights(self, on: bool) -> None:
        if on != self.drag_lights:
            self.drag_lights = on
            self.drag_lights_changed.emit(on)
            self.update()

    def reset_lights(self) -> None:
        self.light_yaw = self.light_pitch = 0.0
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
                         "uSpecular", "uShininess", "uPhong", "uMetal", "uRim", "uBands", "uShowNormals")
        }

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

    def _set_look_uniforms(self, unlit: bool) -> None:
        """The material and lights for the next draw (or flat, unlit colour for lines and points)."""
        u = self._uniforms
        GL.glUniform1f(u["uUnlit"], 1.0 if unlit else 0.0)
        if unlit:
            return
        material, lighting = self.material(), self.lighting()
        dirs = np.zeros((MAX_LIGHTS, 3), dtype=np.float32)
        colors = np.zeros((MAX_LIGHTS, 3), dtype=np.float32)
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
        for name, value in (("uDiffuse", material.diffuse), ("uWrap", material.wrap), ("uSpecular", material.specular),
                            ("uShininess", material.shininess), ("uPhong", float(material.phong)), ("uMetal", material.metal),
                            ("uRim", material.rim), ("uBands", float(material.bands)), ("uShowNormals", float(material.normals))):
            GL.glUniform1f(u[name], value)

    def paintGL(self) -> None:
        if self._needs_upload and self._gl_ready:
            self._upload_mesh_data()
        # QPainter (used for the HUD overlay) resets GL state at the end of every
        # frame, so depth testing and blending must be re-applied before each draw.
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glDepthFunc(GL.GL_LEQUAL)
        GL.glDepthMask(GL.GL_TRUE)
        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFunc(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA)
        GL.glDisable(GL.GL_CULL_FACE)

        GL.glClearColor(0.08, 0.09, 0.11, 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)

        w = self.width()
        h = self.height()
        aspect = float(w) / float(max(h, 1))

        # Matrices
        proj = QMatrix4x4()
        proj.perspective(45.0, aspect, 0.05, 100.0)

        view = QMatrix4x4()
        cam_dist = 3.2 / max(self.zoom, 0.05)
        view.translate(self.pan_x, self.pan_y, -cam_dist)
        view.rotate(self.rot_x, 1.0, 0.0, 0.0)
        view.rotate(self.rot_y, 0.0, 1.0, 0.0)

        model = QMatrix4x4()
        model_view = view * model
        mvp = proj * model_view

        # 1. Draw Ground Plane Grid
        if self.show_grid and self.grid_vao is not None and self.grid_program is not None:
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
            self.mesh_program.release()

        # 3. 2D HUD & Orientation Gizmo Overlay via QPainter
        self._draw_hud_and_gizmo(w, h)

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
        badge_text = f"{self.render_mode}  {self.face_count:,} tris"
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(20, 22, 28, 190))
        badge_width = max(175, painter.fontMetrics().horizontalAdvance(badge_text) + 40)
        painter.drawRoundedRect(12, 12, badge_width, 26, 5, 5)
        painter.setPen(QColor(209, 213, 219))
        painter.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
        painter.drawText(QRectF(12, 12, badge_width, 26), Qt.AlignmentFlag.AlignCenter, badge_text)

        # 3. The key light's direction, top-right: a dot on a disc (hollow when it's behind)
        self._draw_light_dial(painter, QPointF(w - 40.0, 40.0), 22.0)

        # 4. How to move it, bottom-right
        painter.setPen(QColor(156, 163, 175, 170))
        painter.setFont(QFont("Segoe UI", 8))
        hint = "Drag: turn   Shift+drag: lights   Right-drag: pan   Scroll: zoom"
        painter.drawText(QRectF(0, h - 26, w - 12, 20), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, hint)
        painter.end()

    def _draw_light_dial(self, painter: QPainter, center: QPointF, radius: float) -> None:
        turning = self._turning_lights or self.drag_lights
        painter.setPen(QPen(QColor(209, 213, 219, 200 if turning else 110), 1.2))
        painter.setBrush(QColor(20, 22, 28, 190))
        painter.drawEllipse(center, radius, radius)
        x, y, z = light_directions(self.lighting(), self.light_yaw, self.light_pitch)[0]
        dot = QPointF(center.x() + x * radius * 0.8, center.y() - y * radius * 0.8)
        painter.setPen(QPen(QColor(253, 224, 71), 1.5))
        painter.setBrush(QColor(253, 224, 71) if z >= 0 else Qt.BrushStyle.NoBrush)
        painter.drawEllipse(dot, 4.0, 4.0)
        if turning:
            painter.setPen(QColor(209, 213, 219))
            painter.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
            painter.drawText(QRectF(center.x() - 60, center.y() + radius + 2, 120, 16), Qt.AlignmentFlag.AlignCenter, "Lights")


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
def load_full_texture(file_path: Path) -> tuple[QPixmap, bool]:
    """Load high-resolution texture safely without hitting 256MB memory limit.

    Returns the pixmap and whether the image actually contains transparent pixels.
    """
    QImageReader.setAllocationLimit(2048)
    try:
        from PIL import Image
        from core.thumbnail_manager import _safe_convert_pil_to_rgba, open_image

        with open_image(file_path) as im:
            # Convert to RGBA first – normalizes 16-bit / HDR images and
            # avoids Pillow BILINEAR crash on I;16 mode.
            rgba = _safe_convert_pil_to_rgba(im)

            # If extremely large (e.g. > 8192px), downsample to prevent GPU VRAM exhaustion
            if rgba.width > 8192 or rgba.height > 8192:
                rgba.thumbnail((8192, 8192), Image.Resampling.BILINEAR)

            # Every image is RGBA after conversion, so check the alpha values themselves
            has_alpha = rgba.getchannel("A").getextrema()[0] < 255
            qimg = QImage(
                rgba.tobytes(), rgba.width, rgba.height, QImage.Format.Format_RGBA8888
            ).copy()
            return QPixmap.fromImage(qimg), has_alpha
    except Exception:
        pass

    # Fallback to Qt QImageReader
    reader = QImageReader(str(file_path))
    reader.setAutoTransform(True)
    reader.setAllocationLimit(2048)
    qimg = reader.read()
    if not qimg.isNull():
        return QPixmap.fromImage(qimg), qimg.hasAlphaChannel()
    return QPixmap(), False


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
    """Interactive texture and image viewer supporting zoom, pan, and alpha checkerboard."""

    def __init__(self, file_path: Path, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.file_path = file_path
        self.pixmap, self.has_alpha = load_full_texture(file_path)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.zoom: float = 1.0
        self.pan_x: float = 0.0
        self.pan_y: float = 0.0
        # On by default only when there's transparency to see
        self.show_checkerboard: bool = self.has_alpha
        self._checker_brush = _checker_brush()

        self.last_mouse_pos: Optional[QPoint] = None
        self._fit_to_screen_requested: bool = True

    def fit_to_screen(self) -> None:
        """Scale image to fit within the viewport window."""
        if self.pixmap.isNull():
            return
        w, h = self.width(), self.height()
        if w > 20 and h > 20:
            scale_w = (w - 40) / self.pixmap.width()
            scale_h = (h - 40) / self.pixmap.height()
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
                QRectF(0, 0, w, h),
                Qt.AlignmentFlag.AlignCenter,
                f"Unable to load image format:\n{self.file_path.name}",
            )
            return

        target_w = self.pixmap.width() * self.zoom
        target_h = self.pixmap.height() * self.zoom

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

        # Render texture smoothly
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(img_rect.toRect(), self.pixmap)

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
    ):
        """tags: the asset's tags to show (None hides the section).
        edit_tags: opens the tagging dialog; returns the updated tags, or None if nothing changed.
        navigate: navigate(-1 / +1) returns the previous / next file, or None at either end;
        it enables the < > buttons and Page Up / Page Down. position: e.g. "3 of 120".
        artbook: the main window (fill_artbook_menu, send_image_to_artbook): a PDF's pages can be
        sent to the Artbook (None: they can't). model_viewed(asset, mesh): a 3D model was loaded
        (the grid renders its picture from it; its arrays are let go after the call)."""
        super().__init__(parent)
        self.asset = asset
        self.tags = tags
        self._edit_tags = edit_tags
        self._navigate = navigate
        self._position = position
        self._artbook = artbook
        self._model_viewed = model_viewed
        self.video_player: Optional[VideoPlayer] = None  # at most one, owned by this dialog
        self.pdf_viewer: Optional[PdfViewer] = None  # (saves where you were before it goes)
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
        if self.video_player is not None:
            self.video_player.set_fullscreen(False)
            self.video_player.stop()
            self.video_player = None
        if self.pdf_viewer is not None:
            self.pdf_viewer.save_state()
            self.pdf_viewer = None
        self.asset, self.tags, self._edit_tags, self._position = target.asset, target.tags, target.edit_tags, target.position
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

        # Case A: 3D Model Viewer (loaded on a worker thread: a big sculpt takes seconds)
        if self.asset.is_3d_model:
            viewport = Model3DViewportWidget()
            viewport.set_status(f"Loading {self.asset.name}...")
            self.viewport = viewport
            self._load_model(viewport)
            layout.addWidget(self._build_model_toolbar(viewport))
            layout.addWidget(viewport, 1)

        # Case B: Texture / Image Viewer
        elif self.asset.is_texture:
            tex_viewer = TextureViewerWidget(self.asset.path)

            if not tex_viewer.pixmap.isNull():
                w = tex_viewer.pixmap.width()
                h = tex_viewer.pixmap.height()
                gcd_val = math.gcd(w, h)
                aspect = f"{w // gcd_val}:{h // gcd_val}" if gcd_val > 0 else "N/A"
                metadata.update(
                    {
                        "Resolution": f"{w} × {h} px",
                        "Aspect Ratio": aspect,
                        "Color Depth": f"{tex_viewer.pixmap.depth()}-bit",
                        "Transparency": "Yes" if tex_viewer.has_alpha else "No",
                    }
                )

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
            btn_checker.setToolTip("Check Transparency")
            btn_checker.toggled.connect(tex_viewer.toggle_checkerboard)
            tb_layout.addWidget(btn_checker)

            tb_layout.addStretch(1)
            hint_lbl = QLabel("Scroll to zoom • Drag to pan")
            hint_lbl.setObjectName("hintLabel")
            tb_layout.addWidget(hint_lbl)

            layout.addWidget(t_toolbar)
            layout.addWidget(tex_viewer, 1)

        # Case C: Document (PDF) Viewer
        elif self.asset.is_document:
            self.pdf_viewer = PdfViewer(self.asset.path, artbook=self._artbook)
            metadata["Pages"] = str(self.pdf_viewer.doc.pageCount())
            layout.addWidget(self.pdf_viewer, 1)

        # Case D: Video Player
        elif self.asset.is_video:
            self.video_player = VideoPlayer()
            self.video_player.fullscreen_changed.connect(self._on_video_fullscreen)
            layout.addWidget(self.video_player, 1)
            self.video_player.load(self.asset.path, autoplay=True)
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
    def _load_model(self, viewport: Model3DViewportWidget) -> None:
        """Read the model on a worker thread; show it (and its details) when it's ready."""
        self._load_token = getattr(self, "_load_token", 0) + 1
        token, path = self._load_token, self.asset.path

        def done(mesh: Mesh3D) -> None:
            if token != self._load_token or not shiboken6.isValid(viewport):
                return  # (another file is shown now, or the viewer was closed)
            if self._model_viewed is not None:
                self._model_viewed(self.asset, mesh)
            viewport.set_mesh(mesh)
            self._metadata.update(mesh.metadata)
            self._refresh_inspector()

        def failed(message: str) -> None:
            if token == self._load_token and shiboken6.isValid(viewport):
                viewport.set_status(f"This model couldn't be opened.\n{message}")

        run_job(lambda: load_3d_model(path), done, failed)

    def _refresh_inspector(self) -> None:
        sizes = self._splitter.sizes()
        old = self.inspector_panel
        self.inspector_panel = self._build_inspector_panel(self._metadata)
        self._splitter.replaceWidget(1, self.inspector_panel)
        old.deleteLater()
        self._splitter.setSizes(sizes)

    def _build_model_toolbar(self, viewport: Model3DViewportWidget) -> QWidget:
        """Shading, material, colour, lighting, and what dragging turns."""
        v_toolbar = QWidget()
        v_toolbar.setObjectName("viewerToolbar")
        tb_layout = QHBoxLayout(v_toolbar)
        tb_layout.setContentsMargins(8, 4, 8, 4)
        tb_layout.setSpacing(8)

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
        self.cmb_material.currentIndexChanged.connect(lambda _i: viewport.set_material(self.cmb_material.currentData()))
        tb_layout.addWidget(self.cmb_material)

        self.btn_model_color = QPushButton()
        self.btn_model_color.setToolTip("The model's colour (remembered for every model)")
        self.btn_model_color.setIconSize(QSize(18, 18))
        self.btn_model_color.setIcon(_swatch_icon(viewport.color))
        self.btn_model_color.clicked.connect(lambda: self._pick_model_color(viewport))
        tb_layout.addWidget(self.btn_model_color)

        self.cmb_lighting = QComboBox()
        for lighting_id, lighting in LIGHTINGS.items():
            self.cmb_lighting.addItem(lighting.label, lighting_id)
            self.cmb_lighting.setItemData(self.cmb_lighting.count() - 1, lighting.tip, Qt.ItemDataRole.ToolTipRole)
        self.cmb_lighting.setCurrentIndex(self.cmb_lighting.findData(viewport.lighting_id))
        self.cmb_lighting.setToolTip("The lighting set-up")
        self.cmb_lighting.currentIndexChanged.connect(lambda _i: viewport.set_lighting(self.cmb_lighting.currentData()))
        tb_layout.addWidget(self.cmb_lighting)

        self.btn_drag_lights = QPushButton("Turn Lights")
        set_icon(self.btn_drag_lights, "light")
        self.btn_drag_lights.setCheckable(True)
        self.btn_drag_lights.setToolTip(
            "On: dragging turns the lights round the model. Off: it turns the model (L; or hold Shift while dragging)"
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
        tb_layout.addWidget(btn_turntable)

        btn_reset = QPushButton("Reset View")
        set_icon(btn_reset, "refresh")
        btn_reset.setToolTip("Reset the camera and the lights (R resets the camera)")
        btn_reset.clicked.connect(viewport.reset_camera)
        btn_reset.clicked.connect(viewport.reset_lights)
        tb_layout.addWidget(btn_reset)
        tb_layout.addStretch(1)
        return v_toolbar

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
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

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
        else:
            super().keyPressEvent(event)

    def done(self, result: int) -> None:
        """Stop playback and release the video file however the dialog closes."""
        if self.video_player is not None:
            self.video_player.stop()
        if self.pdf_viewer is not None:
            self.pdf_viewer.save_state()
        super().done(result)

    def _open_external(self) -> None:
        """Open asset in system default application."""
        if self.video_player is not None:
            self.video_player.player.pause()
        if self.asset.path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.asset.path)))

    def _reveal_in_explorer(self) -> None:
        """Reveal file in Windows File Explorer."""
        try:
            if sys.platform == "win32":
                import os
                norm_path = os.path.normpath(str(self.asset.path.resolve()))
                subprocess.Popen(f'explorer /select,"{norm_path}"', shell=True)
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.asset.path.parent)))
        except Exception as exc:  # pylint: disable=broad-except
            print(f"Error revealing in explorer: {exc}")

