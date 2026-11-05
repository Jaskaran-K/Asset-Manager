"""A model's UV layout (core.model_uvs), drawn with OpenGL lines: a sculpt's millions of edges would
take a QPainter seconds a frame. The whole-number squares in use are lit, with their UDIM numbers."""

from __future__ import annotations

import ctypes
from typing import Optional

import numpy as np
from OpenGL import GL
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtOpenGL import QOpenGLShader, QOpenGLShaderProgram
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QWidget

from core.model_uvs import UvLayout, tiles_text, udim_number
from ui import shortcuts

_VERT_SHADER = """#version 330 core
layout(location = 0) in vec2 aPos;
uniform vec2 uScale;
uniform vec2 uCenter;

void main() {
    gl_Position = vec4((aPos - uCenter) * uScale, 0.0, 1.0);
}
"""

_FRAG_SHADER = """#version 330 core
uniform vec4 uColor;
out vec4 FragColor;

void main() {
    FragColor = uColor;
}
"""

MIN_LABEL_PIXELS = 44  # a square smaller than this on screen has no room for its number
FIT_MARGIN = 0.08  # of the window, around the layout
ZOOM_STEP = 1.15


def edge_opacity(edge_pixels: float) -> float:
    """How solid the lines are, given how long an edge is on screen: where they're far shorter than
    the gaps a line needs, solid ones would fill the shape in and hide its density."""
    return max(0.18, min(0.9, 0.18 + edge_pixels * 0.09))


class UvViewWidget(QOpenGLWidget):
    """Pans with any button and zooms round the pointer. A layout can come later (set_layout), as
    it's read on a worker thread."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.layout_shown: Optional[UvLayout] = None
        self.status_text: str = ""
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.center_x, self.center_y = 0.5, 0.5
        self.pixels_per_unit = 400.0
        self._last_mouse: Optional[QPoint] = None
        self._fitted = False
        self._program: Optional[QOpenGLShaderProgram] = None
        self._uniforms: dict[str, int] = {}
        self._needs_upload = False
        self._edge_vao: Optional[int] = None
        self._cell_vao: Optional[int] = None
        self._buffers: list[int] = []
        self._cell_fill_count = 0
        self._cell_line_count = 0

    # ------------------ what's shown ------------------
    def set_status(self, text: str) -> None:
        """Shown instead of the layout (e.g. "Reading UVs...", or that there are none)."""
        self.status_text = text
        self.layout_shown = None
        self._needs_upload = True  # (the old one's lines go)
        self.update()

    def set_layout(self, layout: UvLayout) -> None:
        self.layout_shown = layout
        self.status_text = ""
        self._needs_upload = True
        self.fit()

    def fit(self) -> None:
        """Frame everything in the layout, and the 0-1 square."""
        layout = self.layout_shown
        if layout is None:
            return
        low_u, low_v, high_u, high_v = layout.bounds
        low_u, low_v, high_u, high_v = min(low_u, 0.0), min(low_v, 0.0), max(high_u, 1.0), max(high_v, 1.0)
        self.center_x, self.center_y = (low_u + high_u) / 2, (low_v + high_v) / 2
        usable = 1.0 - 2 * FIT_MARGIN
        self.pixels_per_unit = max(1e-3, min(max(self.width(), 1) * usable / max(high_u - low_u, 1e-6),
                                             max(self.height(), 1) * usable / max(high_v - low_v, 1e-6)))
        self._fitted = self.width() > 0 and self.height() > 0
        self.update()

    def to_screen(self, u: float, v: float) -> QPointF:
        return QPointF(self.width() / 2 + (u - self.center_x) * self.pixels_per_unit,
                       self.height() / 2 - (v - self.center_y) * self.pixels_per_unit)

    # ------------------ the mouse and keys ------------------
    def mousePressEvent(self, event) -> None:
        self._last_mouse = event.position().toPoint()

    def mouseMoveEvent(self, event) -> None:
        if self._last_mouse is None:
            return
        here = event.position().toPoint()
        self.center_x -= (here.x() - self._last_mouse.x()) / self.pixels_per_unit
        self.center_y += (here.y() - self._last_mouse.y()) / self.pixels_per_unit
        self._last_mouse = here
        self.update()

    def mouseReleaseEvent(self, _event) -> None:
        self._last_mouse = None

    def wheelEvent(self, event) -> None:
        """Zoom, keeping the UV under the pointer where it is."""
        factor = ZOOM_STEP if event.angleDelta().y() > 0 else 1 / ZOOM_STEP
        at = event.position()
        u = self.center_x + (at.x() - self.width() / 2) / self.pixels_per_unit
        v = self.center_y - (at.y() - self.height() / 2) / self.pixels_per_unit
        self.pixels_per_unit = max(1.0, min(2_000_000.0, self.pixels_per_unit * factor))
        self.center_x = u - (at.x() - self.width() / 2) / self.pixels_per_unit
        self.center_y = v + (at.y() - self.height() / 2) / self.pixels_per_unit
        self.update()

    def keyPressEvent(self, event) -> None:
        if shortcuts.matches("viewer3d.reset", event):
            self.fit()
        else:
            super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if not self._fitted:  # (a layout that arrived before the window had a size)
            self.fit()

    # ------------------ OpenGL ------------------
    def initializeGL(self) -> None:
        self._program = QOpenGLShaderProgram(self)
        self._program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, _VERT_SHADER)
        self._program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, _FRAG_SHADER)
        self._program.link()
        self._uniforms = {name: self._program.uniformLocation(name) for name in ("uScale", "uCenter", "uColor")}
        self._edge_vao = self._cell_vao = None  # (they went with the old context, if there was one)
        self._buffers = []
        self._needs_upload = True

    def _new_buffer(self, target: int, data: np.ndarray) -> int:
        buffer = GL.glGenBuffers(1)
        self._buffers.append(buffer)
        GL.glBindBuffer(target, buffer)
        GL.glBufferData(target, data.nbytes, data, GL.GL_STATIC_DRAW)
        return buffer

    def _upload(self) -> None:
        """The layout to the GPU, then its arrays are let go: a sculpt's are hundreds of MB."""
        self._needs_upload = False
        if self._buffers:
            GL.glDeleteBuffers(len(self._buffers), self._buffers)
            self._buffers = []
        for vao in (self._edge_vao, self._cell_vao):
            if vao is not None:
                GL.glDeleteVertexArrays(1, [vao])
        self._edge_vao = self._cell_vao = None
        layout = self.layout_shown
        if layout is None:
            return

        if layout.uvs is not None and layout.edges is not None:
            self._edge_vao = GL.glGenVertexArrays(1)
            GL.glBindVertexArray(self._edge_vao)
            self._new_buffer(GL.GL_ARRAY_BUFFER, np.ascontiguousarray(layout.uvs, dtype=np.float32))
            GL.glEnableVertexAttribArray(0)
            GL.glVertexAttribPointer(0, 2, GL.GL_FLOAT, GL.GL_FALSE, 0, ctypes.c_void_p(0))
            self._new_buffer(GL.GL_ELEMENT_ARRAY_BUFFER, np.ascontiguousarray(layout.edges, dtype=np.uint32))
            GL.glBindVertexArray(0)
            layout.uvs = layout.edges = None

        fills, lines = cell_geometry(layout.cells)
        self._cell_fill_count, self._cell_line_count = len(fills), len(lines)
        self._cell_vao = GL.glGenVertexArrays(1)
        GL.glBindVertexArray(self._cell_vao)
        self._new_buffer(GL.GL_ARRAY_BUFFER, np.concatenate([fills, lines]))
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 2, GL.GL_FLOAT, GL.GL_FALSE, 0, ctypes.c_void_p(0))
        GL.glBindVertexArray(0)

    def paintGL(self) -> None:
        if self._needs_upload and self._program is not None:
            self._upload()
        GL.glClearColor(0.08, 0.09, 0.11, 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        layout = self.layout_shown
        width, height = max(self.width(), 1), max(self.height(), 1)
        if layout is not None and self._program is not None and self._cell_vao is not None:
            GL.glDisable(GL.GL_DEPTH_TEST)
            GL.glEnable(GL.GL_BLEND)
            # (the window's own alpha stays solid: Qt treats it as how see-through the pixel is)
            GL.glBlendFuncSeparate(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA, GL.GL_ONE, GL.GL_ONE_MINUS_SRC_ALPHA)
            self._program.bind()
            u = self._uniforms
            GL.glUniform2f(u["uScale"], self.pixels_per_unit * 2 / width, self.pixels_per_unit * 2 / height)
            GL.glUniform2f(u["uCenter"], self.center_x, self.center_y)
            GL.glBindVertexArray(self._cell_vao)
            GL.glUniform4f(u["uColor"], 0.14, 0.16, 0.20, 1.0)
            GL.glDrawArrays(GL.GL_TRIANGLES, 0, self._cell_fill_count)
            GL.glUniform4f(u["uColor"], 0.40, 0.44, 0.52, 1.0)
            GL.glDrawArrays(GL.GL_LINES, self._cell_fill_count, self._cell_line_count)
            if self._edge_vao is not None:
                GL.glBindVertexArray(self._edge_vao)
                GL.glUniform4f(u["uColor"], 0.38, 0.68, 1.0, edge_opacity(layout.mean_edge * self.pixels_per_unit))
                GL.glDrawElements(GL.GL_LINES, layout.edge_count * 2, GL.GL_UNSIGNED_INT, ctypes.c_void_p(0))
            GL.glBindVertexArray(0)
            self._program.release()
        self._draw_overlay(width, height)

    def _draw_overlay(self, width: int, height: int) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        layout = self.layout_shown
        if layout is None:
            painter.setPen(QColor(209, 213, 219))
            painter.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
            painter.drawText(QRectF(24, 0, width - 48, height), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.status_text)
            painter.end()
            return

        side = self.pixels_per_unit
        if side >= MIN_LABEL_PIXELS:
            painter.setPen(QColor(209, 213, 219, 200))
            painter.setFont(QFont("Segoe UI", 9 if side < 120 else 11, QFont.Weight.DemiBold))
            for cell_u, cell_v in layout.cells.tolist():
                corner = self.to_screen(cell_u, cell_v)
                if -side < corner.x() < width and 0 < corner.y() < height + side:
                    painter.drawText(QPointF(corner.x() + 6, corner.y() - 6), cell_label(cell_u, cell_v))

        badge = f"UV  {layout.polygons:,} polygons  Tiles: {tiles_text(layout)}"
        painter.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
        box = painter.fontMetrics().horizontalAdvance(badge) + 28
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(20, 22, 28, 190))
        painter.drawRoundedRect(12, 12, box, 26, 5, 5)
        painter.setPen(QColor(209, 213, 219))
        painter.drawText(QRectF(12, 12, box, 26), Qt.AlignmentFlag.AlignCenter, badge)

        painter.setPen(QColor(156, 163, 175, 170))
        painter.setFont(QFont("Segoe UI", 8))
        hint = "Drag: pan   Scroll: zoom   R: fit   U: back to the model"
        painter.drawText(QRectF(0, height - 26, width - 12, 20), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, hint)
        painter.end()


def cell_label(u: int, v: int) -> str:
    """A square's number, or where it is if UDIM has no number for it."""
    number = udim_number(u, v)
    return str(number) if number is not None else f"{u}, {v}"


def cell_geometry(cells: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The squares in use and the 0-1 square, as (triangle corners, line ends): each N x 2 float32."""
    known = {(int(u), int(v)) for u, v in cells}
    known.add((0, 0))
    squares = np.array(sorted(known), dtype=np.float32)
    corners = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=np.float32)
    at = squares[:, None, :] + corners[None, :, :]  # N x 4 x 2
    fills = at[:, [0, 1, 2, 0, 2, 3], :].reshape(-1, 2)
    lines = at[:, [0, 1, 1, 2, 2, 3, 3, 0], :].reshape(-1, 2)
    return fills, lines
