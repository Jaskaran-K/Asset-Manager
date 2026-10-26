"""Lightweight interactive viewer for 3D objects, textures, documents, and videos.

Includes:
- Model3DViewportWidget: 3D orbit viewport for .obj, .gltf, .glb, .fbx with shaded/wireframe modes.
- TextureViewerWidget: High-resolution texture/image viewer with interactive zoom, pan, and checkerboard.
- DocumentViewerWidget: Embedded PDF document viewer with page navigation and zoom.
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
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
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
from core.scanner import AssetItem
from core.tag_repository import Tag
from ui import shortcuts
from ui.tag_widgets import tag_chip_cloud
from ui.video_player import VideoPlayer, control_icon


# ----------------------------------------------------------------------
# 3D Model Viewport (Hardware-Accelerated OpenGL)
# ----------------------------------------------------------------------
_MESH_VERT_SHADER = """#version 330 core
layout(location = 0) in vec3 aPos;
layout(location = 1) in vec3 aNormal;

uniform mat4 uMVP;
uniform mat4 uModel;
uniform mat3 uNormalMatrix;

out vec3 vNormal;
out vec3 vWorldPos;

void main() {
    vNormal = normalize(uNormalMatrix * aNormal);
    vec4 worldPos = uModel * vec4(aPos, 1.0);
    vWorldPos = worldPos.xyz;
    gl_Position = uMVP * vec4(aPos, 1.0);
}
"""

_MESH_FRAG_SHADER = """#version 330 core
in vec3 vNormal;
in vec3 vWorldPos;

uniform vec3 uColor;
uniform vec3 uLightDir;
uniform vec3 uCamPos;
uniform float uUnlit;

out vec4 FragColor;

void main() {
    if (uUnlit > 0.5) {
        FragColor = vec4(uColor, 1.0);
        return;
    }
    vec3 n = normalize(vNormal);
    if (!gl_FrontFacing) n = -n;

    // Key directional light
    vec3 l1 = normalize(uLightDir);
    float diff1 = max(dot(n, l1), 0.0);

    // Fill light
    vec3 l2 = normalize(vec3(-l1.x, 0.35, -l1.z));
    float diff2 = max(dot(n, l2), 0.0) * 0.40;

    // Ambient
    float ambient = 0.28;

    // Specular highlight
    vec3 viewDir = normalize(uCamPos - vWorldPos);
    vec3 halfDir = normalize(l1 + viewDir);
    float spec = pow(max(dot(n, halfDir), 0.0), 28.0) * 0.28;

    // Silhouette rim lighting
    float rim = pow(1.0 - max(dot(viewDir, n), 0.0), 3.0) * 0.20;

    vec3 litColor = uColor * (ambient + diff1 * 0.70 + diff2) + vec3(spec + rim);
    FragColor = vec4(litColor, 1.0);
}
"""

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
    """Hardware-accelerated 3D viewport supporting orbit, pan, zoom, wireframe, and ground grid."""

    def __init__(self, mesh: Mesh3D, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.mesh = mesh
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

        # Camera orientation & transform
        self.rot_x: float = 20.0
        self.rot_y: float = -35.0
        self.pan_x: float = 0.0
        self.pan_y: float = 0.0
        self.zoom: float = 1.0

        # Mouse interaction tracking
        self.last_mouse_pos: Optional[QPoint] = None
        self.mouse_button: Optional[Qt.MouseButton] = None

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
        self.last_mouse_pos = event.pos()
        self.mouse_button = event.button()

    def mouseMoveEvent(self, event) -> None:
        if self.last_mouse_pos is not None:
            dx = event.position().x() - self.last_mouse_pos.x()
            dy = event.position().y() - self.last_mouse_pos.y()

            if event.buttons() & Qt.MouseButton.LeftButton:
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

            self.last_mouse_pos = event.pos()

    def mouseReleaseEvent(self, event) -> None:
        self.last_mouse_pos = None
        self.mouse_button = None

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta > 0:
            self.zoom = min(20.0, self.zoom * 1.15)
        else:
            self.zoom = max(0.08, self.zoom / 1.15)
        self.update()

    def keyPressEvent(self, event) -> None:
        """Viewport keys (see ui.shortcuts): cycle wireframe/shaded, toggle grid, reset camera."""
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
        self.u_unlit_loc = self.mesh_program.uniformLocation("uUnlit")

        self.grid_program = QOpenGLShaderProgram(self)
        self.grid_program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, _GRID_VERT_SHADER)
        self.grid_program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, _GRID_FRAG_SHADER)
        self.grid_program.link()

        # 2. Upload Mesh Buffers
        self._upload_mesh_data()

        # 3. Generate & Upload Ground Grid
        self._upload_grid_data()

    def _upload_mesh_data(self) -> None:
        """Upload vertex positions, normals, and indices to GPU."""
        if self.mesh is None or self.mesh.vertex_count == 0:
            return

        verts = np.ascontiguousarray(self.mesh.vertices, dtype=np.float32)
        normals = np.ascontiguousarray(self.mesh.normals, dtype=np.float32)

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
        if self.mesh.face_count > 0:
            faces = np.ascontiguousarray(self.mesh.faces, dtype=np.uint32)
            self.index_count = faces.size
            self.ebo = GL.glGenBuffers(1)
            GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, self.ebo)
            GL.glBufferData(GL.GL_ELEMENT_ARRAY_BUFFER, faces.nbytes, faces, GL.GL_STATIC_DRAW)
        else:
            self.index_count = 0

        GL.glBindVertexArray(0)

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

    def paintGL(self) -> None:
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
        mvp = proj * view * model

        # 1. Draw Ground Plane Grid
        if self.show_grid and self.grid_vao is not None and self.grid_program is not None:
            self.grid_program.bind()
            self.grid_program.setUniformValue("uMVP", mvp)
            GL.glBindVertexArray(self.grid_vao)
            GL.glDrawArrays(GL.GL_LINES, 0, self.grid_vert_count)
            GL.glBindVertexArray(0)
            self.grid_program.release()

        # 2. Draw 3D Model Mesh
        if self.vao is not None and self.mesh_program is not None and self.mesh.vertex_count > 0:
            self.mesh_program.bind()
            self.mesh_program.setUniformValue("uMVP", mvp)
            self.mesh_program.setUniformValue("uModel", model)
            self.mesh_program.setUniformValue("uNormalMatrix", model.normalMatrix())
            self.mesh_program.setUniformValue("uLightDir", QVector3D(0.55, 0.80, 0.65))
            # Camera position in world space (origin of view space), for specular/rim
            cam_pos = view.inverted()[0].map(QVector3D(0.0, 0.0, 0.0))
            self.mesh_program.setUniformValue("uCamPos", cam_pos)

            GL.glBindVertexArray(self.vao)
            if self.index_count > 0 and self.ebo is not None:
                GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, self.ebo)

            # Slate blue model base color
            mesh_color = QVector3D(0.28, 0.52, 0.88)
            wire_color = QVector3D(0.38, 0.68, 1.0)
            overlay_wire_color = QVector3D(0.12, 0.16, 0.22)

            if self.render_mode == "Wireframe":
                GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_LINE)
                self.mesh_program.setUniformValue("uColor", wire_color)
                if getattr(self, "u_unlit_loc", -1) >= 0:
                    GL.glUniform1f(self.u_unlit_loc, 1.0)
                if self.index_count > 0:
                    GL.glDrawElements(GL.GL_TRIANGLES, self.index_count, GL.GL_UNSIGNED_INT, ctypes.c_void_p(0))
                else:
                    GL.glDrawArrays(GL.GL_POINTS, 0, self.mesh.vertex_count)
                GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_FILL)

            elif self.render_mode == "Shaded + Wireframe":
                # Pass 1: Shaded geometry with polygon offset
                GL.glEnable(GL.GL_POLYGON_OFFSET_FILL)
                GL.glPolygonOffset(1.0, 1.0)
                GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_FILL)
                self.mesh_program.setUniformValue("uColor", mesh_color)
                if getattr(self, "u_unlit_loc", -1) >= 0:
                    GL.glUniform1f(self.u_unlit_loc, 0.0)
                if self.index_count > 0:
                    GL.glDrawElements(GL.GL_TRIANGLES, self.index_count, GL.GL_UNSIGNED_INT, ctypes.c_void_p(0))
                GL.glDisable(GL.GL_POLYGON_OFFSET_FILL)

                # Pass 2: Wireframe lines overlay
                GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_LINE)
                self.mesh_program.setUniformValue("uColor", overlay_wire_color)
                if getattr(self, "u_unlit_loc", -1) >= 0:
                    GL.glUniform1f(self.u_unlit_loc, 1.0)
                if self.index_count > 0:
                    GL.glDrawElements(GL.GL_TRIANGLES, self.index_count, GL.GL_UNSIGNED_INT, ctypes.c_void_p(0))
                GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_FILL)

            elif self.render_mode == "Vertices" or self.index_count == 0:
                GL.glPointSize(3.5)
                self.mesh_program.setUniformValue("uColor", wire_color)
                if getattr(self, "u_unlit_loc", -1) >= 0:
                    GL.glUniform1f(self.u_unlit_loc, 1.0)
                GL.glDrawArrays(GL.GL_POINTS, 0, self.mesh.vertex_count)

            else:  # "Shaded"
                GL.glPolygonMode(GL.GL_FRONT_AND_BACK, GL.GL_FILL)
                self.mesh_program.setUniformValue("uColor", mesh_color)
                if getattr(self, "u_unlit_loc", -1) >= 0:
                    GL.glUniform1f(self.u_unlit_loc, 0.0)
                if self.index_count > 0:
                    GL.glDrawElements(GL.GL_TRIANGLES, self.index_count, GL.GL_UNSIGNED_INT, ctypes.c_void_p(0))
                else:
                    GL.glDrawArrays(GL.GL_POINTS, 0, self.mesh.vertex_count)

            GL.glBindVertexArray(0)
            self.mesh_program.release()

        # 3. 2D HUD & Orientation Gizmo Overlay via QPainter
        self._draw_hud_and_gizmo(w, h)

    def _draw_hud_and_gizmo(self, w: int, h: int) -> None:
        """Render miniature coordinate orientation gizmo and statistics badge."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

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
        badge_text = f"{self.render_mode} • {self.mesh.face_count:,} tris"
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(20, 22, 28, 190))
        painter.drawRoundedRect(12, 12, 175, 26, 5, 5)
        painter.setPen(QColor(209, 213, 219))
        painter.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
        painter.drawText(QRectF(12, 12, 175, 26), Qt.AlignmentFlag.AlignCenter, badge_text)

        painter.end()


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
            self.last_mouse_pos = event.pos()

    def mouseMoveEvent(self, event) -> None:
        if self.last_mouse_pos is not None:
            dx = event.position().x() - self.last_mouse_pos.x()
            dy = event.position().y() - self.last_mouse_pos.y()
            self.pan_x += dx
            self.pan_y += dy
            self.last_mouse_pos = event.pos()
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
# PDF Document Viewer
# ----------------------------------------------------------------------
class DocumentViewerWidget(QWidget):
    """Embedded PDF document viewer with page navigation and zoom controls."""

    def __init__(self, file_path: Path, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.file_path = file_path
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # PDF Document & View
        self.doc = QPdfDocument(self)
        self.doc.load(str(self.file_path))

        self.pdf_view = QPdfView(self)
        self.pdf_view.setDocument(self.doc)
        self.pdf_view.setPageMode(QPdfView.PageMode.MultiPage)
        self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitInView)

        # Toolbar controls
        toolbar = QWidget()
        toolbar.setObjectName("viewerToolbar")
        t_layout = QHBoxLayout(toolbar)
        t_layout.setContentsMargins(10, 4, 10, 4)
        t_layout.setSpacing(8)

        btn_fit = QPushButton("Fit Page")
        btn_fit.setToolTip("Zoom so the whole page fits")
        btn_fit.clicked.connect(lambda: self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitInView))
        t_layout.addWidget(btn_fit)

        btn_fit_width = QPushButton("Fit Width")
        btn_fit_width.setToolTip("Zoom so the page fills the width")
        btn_fit_width.clicked.connect(lambda: self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth))
        t_layout.addWidget(btn_fit_width)

        btn_zoom_in = QPushButton("+ Zoom In")
        btn_zoom_in.setToolTip("Zoom in by 25%")
        btn_zoom_in.clicked.connect(lambda: self.pdf_view.setZoomFactor(self.pdf_view.zoomFactor() * 1.25))
        t_layout.addWidget(btn_zoom_in)

        btn_zoom_out = QPushButton("- Zoom Out")
        btn_zoom_out.setToolTip("Zoom out by 25%")
        btn_zoom_out.clicked.connect(lambda: self.pdf_view.setZoomFactor(self.pdf_view.zoomFactor() / 1.25))
        t_layout.addWidget(btn_zoom_out)

        t_layout.addStretch(1)

        page_count = self.doc.pageCount()
        self.lbl_pages = QLabel(f"Total Pages: {page_count}")
        self.lbl_pages.setObjectName("toolbarLabel")
        t_layout.addWidget(self.lbl_pages)

        layout.addWidget(toolbar)
        layout.addWidget(self.pdf_view, 1)


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
    ):
        """tags: the asset's tags to show (None hides the section).
        edit_tags: opens the tagging dialog; returns the updated tags, or None if nothing changed.
        navigate: navigate(-1 / +1) returns the previous / next file, or None at either end;
        it enables the < > buttons and Page Up / Page Down. position: e.g. "3 of 120"."""
        super().__init__(parent)
        self.asset = asset
        self.tags = tags
        self._edit_tags = edit_tags
        self._navigate = navigate
        self._position = position
        self.video_player: Optional[VideoPlayer] = None  # at most one, owned by this dialog
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

        viewer_widget, metadata_dict = self._create_viewer_and_metadata()
        splitter.addWidget(viewer_widget)

        self.inspector_panel = self._build_inspector_panel(metadata_dict)
        splitter.addWidget(self.inspector_panel)

        splitter.setSizes([840, 260])
        main_layout.addWidget(splitter, 1)

        # 3. Bottom Action Bar
        self.bottom_bar = self._build_bottom_bar()
        main_layout.addWidget(self.bottom_bar)

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

        # Case A: 3D Model Viewer
        if self.asset.is_3d_model:
            mesh = load_3d_model(self.asset.path)
            metadata.update(mesh.metadata)

            viewport = Model3DViewportWidget(mesh)

            # Viewport Control Toolbar
            v_toolbar = QWidget()
            v_toolbar.setObjectName("viewerToolbar")
            tb_layout = QHBoxLayout(v_toolbar)
            tb_layout.setContentsMargins(8, 4, 8, 4)
            tb_layout.setSpacing(8)

            lbl_mode = QLabel("Shading:")
            lbl_mode.setObjectName("toolbarLabel")
            tb_layout.addWidget(lbl_mode)

            cb_mode = QComboBox()
            cb_mode.addItems(["Shaded", "Wireframe", "Shaded + Wireframe", "Vertices"])
            cb_mode.currentTextChanged.connect(viewport.set_render_mode)
            tb_layout.addWidget(cb_mode)

            btn_grid = QPushButton("⊞ Ground Grid")
            btn_grid.setCheckable(True)
            btn_grid.setChecked(True)
            btn_grid.setToolTip("Toggle 3D ground plane reference grid (or press 'G')")
            btn_grid.toggled.connect(viewport.toggle_ground_grid)
            tb_layout.addWidget(btn_grid)

            btn_turntable = QPushButton("▶ Auto Rotate")
            btn_turntable.setToolTip("Spin the model slowly to see it from every side")
            btn_turntable.setCheckable(True)

            def on_turntable_toggled(checked: bool):
                viewport.toggle_turntable(checked)
                btn_turntable.setText("⏸ Pause" if checked else "▶ Auto Rotate")

            btn_turntable.toggled.connect(on_turntable_toggled)
            tb_layout.addWidget(btn_turntable)

            btn_reset = QPushButton("Reset View")
            btn_reset.setToolTip("Reset camera orientation and zoom (or press 'R')")
            btn_reset.clicked.connect(viewport.reset_camera)
            tb_layout.addWidget(btn_reset)

            tb_layout.addStretch(1)
            hint_lbl = QLabel("Left-drag: orbit • Right-drag: pan • Scroll: zoom • W: wireframe • G: grid")
            hint_lbl.setObjectName("hintLabel")
            tb_layout.addWidget(hint_lbl)

            layout.addWidget(v_toolbar)
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
            doc_viewer = DocumentViewerWidget(self.asset.path)
            metadata["Pages"] = str(doc_viewer.doc.pageCount())
            layout.addWidget(doc_viewer, 1)

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

