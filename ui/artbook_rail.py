"""The Artbook's tool rail: a slim floating bar down the board's left edge with what you can add to a page.

Click a tool to add it in the middle of the view, or drag it onto the board to put it where you let go (the
board takes the drop: BoardView.add_tool). Pictures opens a file picker; Line and Draw switch their modes on and off.
Right-click the board > Add Here has the same tools, placed where you clicked.

Each kind of card adds its own entry to TOOLS when it arrives, so the rail only ever shows tools that work. Like
the board's other bars, nothing here takes the keyboard focus: the board keeps its keys. While the Pages sidebar
floats over the board, the rail moves out from under it (cover).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QMimeData, QPoint, QRectF, QSize, Qt
from PySide6.QtGui import QDrag, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QFrame, QWidget

from ui import shortcuts
from ui.controls import FloatingBar, GhostButton
from ui.duotone import set_icon
from ui.themes import theme_color

if TYPE_CHECKING:
    from ui.artbook_board import BoardView

TOOL_MIME = "application/x-asset-manager-board-tool"  # (a drag from the rail: the tool's id)
GAP = 12  # pixels between the rail and the board's left edge (or the sidebar over it)


@dataclass(frozen=True)
class Tool:
    tool_id: str
    icon: str
    name: str
    about: str
    shortcut_id: Optional[str] = None
    draggable: bool = True  # (dragged onto the board, it's added where you let go)
    checkable: bool = False  # (a mode: Draw)


TOOLS: list[Optional[Tool]] = [
    Tool("note", "sticky_note", "Note", "a card of text", "artbook.note"),
    Tool("heading", "heading", "Heading", "a big title, with an accent line", "artbook.heading"),
    Tool("todo", "todo", "To-do", "a title and a checklist", "artbook.todo"),
    Tool("column", "column", "Column", "a titled column that cards stack in", "artbook.column"),
    Tool("table", "table", "Table", "rows and columns: text, numbers with a total, status chips", "artbook.table"),
    None,
    Tool("pictures", "import", "Pictures", "pick image files to add", draggable=False),
    Tool("line", "line", "Line", "drag from a card to another to join them with an arrow", "artbook.line", draggable=False, checkable=True),
    Tool("draw", "edit", "Draw", "freehand strokes on the board", "artbook.draw", draggable=False, checkable=True),
]


def tool_tip(tool: Tool) -> str:
    key = f" ({shortcuts.get(tool.shortcut_id).keys})" if tool.shortcut_id else ""
    how = ": click to add one in the middle, or drag it onto the board" if tool.draggable else ""
    return f"{tool.name}{key}: {tool.about}{how}"


def drag_picture(tool: Tool) -> QPixmap:
    """What's under the mouse while a tool is dragged: a small card with the tool's name, in the theme's colours."""
    ratio = QApplication.instance().devicePixelRatio() if QApplication.instance() else 1.0
    size = QSize(150, 44)
    pixmap = QPixmap(size * ratio)
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    box = QRectF(1, 1, size.width() - 2, size.height() - 2)
    fill = theme_color("raised")
    fill.setAlphaF(0.92)
    pen = QPen(theme_color("accent_border"), 1.5, Qt.PenStyle.DashLine)
    painter.setPen(pen)
    painter.setBrush(fill)
    painter.drawRoundedRect(box, 10, 10)
    font = QFont(QApplication.font())
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(theme_color("text_strong"))
    painter.drawText(box.adjusted(14, 0, -10, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, tool.name)
    painter.end()
    return pixmap


class RailButton(GhostButton):
    """A tool on the rail: a click adds it; a drag (past the system's drag distance) carries it to the board."""

    def __init__(self, tool: Tool, parent: QWidget):
        super().__init__(parent)
        self.tool = tool
        self.setObjectName("ghost")
        set_icon(self, tool.icon, size=20)
        self.setIconSize(QSize(20, 20))
        self.setToolTip(tool_tip(tool))
        self.setCheckable(tool.checkable)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._press: Optional[QPoint] = None

    def mousePressEvent(self, event) -> None:
        self._press = event.position().toPoint() if event.button() == Qt.MouseButton.LeftButton else None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if (self._press is not None and self.tool.draggable and event.buttons() & Qt.MouseButton.LeftButton
                and (event.position().toPoint() - self._press).manhattanLength() >= QApplication.startDragDistance()):
            self._press = None
            self.setDown(False)  # (a drag isn't a click)
            self.drag()
            return
        super().mouseMoveEvent(event)

    def drag(self) -> Qt.DropAction:
        mime = QMimeData()
        mime.setData(TOOL_MIME, self.tool.tool_id.encode("ascii"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        picture = drag_picture(self.tool)
        drag.setPixmap(picture)
        drag.setHotSpot(QPoint(20, int(picture.height() / picture.devicePixelRatio() / 2)))
        return drag.exec(Qt.DropAction.CopyAction)


class ToolRail(FloatingBar):
    """See the module docstring. buttons: {tool id: RailButton}."""

    def __init__(self, board: "BoardView"):
        super().__init__(board, gap=GAP, object_name="floatingBar", vertical=True)
        self.board = board
        self.cover: Optional[QWidget] = None
        self.row.setContentsMargins(5, 7, 5, 7)
        self.buttons: dict[str, RailButton] = {}
        for tool in TOOLS:
            if tool is None:
                line = QFrame(self.frame)
                line.setObjectName("barSep")
                line.setFixedHeight(1)
                self.row.addSpacing(3)
                self.row.addWidget(line)
                self.row.addSpacing(3)
                continue
            button = RailButton(tool, self.frame)
            button.clicked.connect(self._on_clicked)
            self.row.addWidget(button)
            self.buttons[tool.tool_id] = button

    def _on_clicked(self) -> None:
        button = self.sender()
        if isinstance(button, RailButton):
            self.board.add_tool(button.tool.tool_id)

    def set_cover(self, widget: QWidget) -> None:
        """widget may float over the board's left edge (the Pages sidebar): keep the rail to its right."""
        self.cover = widget
        self.follow(widget)

    def inset(self) -> int:
        """How far the board's left edge is covered (0 when nothing's over it)."""
        cover = self.cover
        if cover is None or not cover.isVisible():
            return 0
        right = cover.mapToGlobal(QPoint(cover.width(), 0)).x() - self.host.mapToGlobal(QPoint(0, 0)).x()
        return max(0, min(right, self.host.width() // 2))

    def place(self) -> None:
        self.resize(self.natural_size())
        margins = self.layout().contentsMargins()  # (room for the shadow: more of it below)
        frame_height = self.height() - margins.top() - margins.bottom()
        x = self.inset() + self.gap - margins.left()
        y = (self.host.height() - frame_height) // 2 - margins.top()  # (the bar itself half way down)
        self.move(QPoint(x, max(0, y)))

    def sync(self) -> None:
        """Show what's on now (Draw, Line)."""
        for tool_id, on in (("draw", self.board.drawing()), ("line", self.board.line_tool_on())):
            button = self.buttons.get(tool_id)
            if button is not None:
                button.setChecked(on)
