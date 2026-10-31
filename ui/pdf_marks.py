"""Your marks on a PDF's pages, in the PDF viewer: highlights over text, pen drawings and typed
notes. They're kept by core.pdf_notes (as the dicts to_dict makes), never written into the PDF.

Each mark is a child of its page's item (ui.pdf_viewer.PageItem), in the page's coordinates
(PDF points from the top-left corner), so it moves and zooms with the page. Changes go on the
viewer's undo stack as MarksCommand: the dicts of the marks before and after.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPainterPathStroker, QPen, QTextOption, QUndoCommand
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPathItem, QGraphicsTextItem, QStyle, QStyleOptionGraphicsItem, QWidget

from ui.themes import theme_color

HIGHLIGHT_ALPHA = 110  # a highlight's colour over the page (multiplied: the ink shows through)
NOTE_WIDTH = 170.0  # points
NOTE_FONT_PT = 11.0
NOTE_PADDING = 6.0
INK_HIT = 4.0  # points either side of a drawn line that still count as on it
MAX_POINTS = 20000
MAX_NOTE_TEXT = 10000
MAX_BOXES = 2000


def new_id() -> str:
    return uuid.uuid4().hex


def _chosen_outline(painter: QPainter, rect: QRectF) -> None:
    """The look of a mark picked with the Hand tool (to move or delete)."""
    pen = QPen(theme_color("accent"), 1.5, Qt.PenStyle.DashLine)
    pen.setCosmetic(True)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(rect.adjusted(-2, -2, 2, 2))


class HighlightMark(QGraphicsItem):
    """Highlighter over some text: one box per line, in the page's coordinates."""

    kind = "highlight"

    def __init__(self, mark_id: str, boxes: list[QRectF], color: str, text: str = ""):
        super().__init__()
        self.mark_id, self.boxes, self.color, self.text = mark_id, boxes, color, text
        self.chosen = False

    def boundingRect(self) -> QRectF:
        area = QRectF()
        for box in self.boxes:
            area = area.united(box)
        return area.adjusted(-3, -3, 3, 3)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        for box in self.boxes:
            path.addRect(box)
        return path

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: Optional[QWidget] = None) -> None:
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        tint = QColor(self.color)
        tint.setAlpha(HIGHLIGHT_ALPHA)
        painter.setBrush(tint)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
        for box in self.boxes:
            painter.drawRect(box)
        painter.restore()
        if self.chosen:
            _chosen_outline(painter, self.boundingRect().adjusted(3, 3, -3, -3))

    def to_dict(self, page: int) -> dict[str, Any]:
        return {
            "id": self.mark_id, "kind": self.kind, "page": page, "color": self.color, "text": self.text,
            "boxes": [[round(b.x(), 2), round(b.y(), 2), round(b.width(), 2), round(b.height(), 2)] for b in self.boxes],
        }


class InkMark(QGraphicsPathItem):
    """A line drawn with the pen: its points in the page's coordinates (plus where it's been moved)."""

    kind = "ink"

    def __init__(self, mark_id: str, points: list[QPointF], color: str, width: float):
        super().__init__()
        self.mark_id, self.color, self.width = mark_id, color, width
        self.chosen = False
        self.points: list[QPointF] = []
        pen = QPen(QColor(color), width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        self.setPen(pen)
        self.set_points(points)

    def set_points(self, points: list[QPointF]) -> None:
        """Through its points, smoothly (a curve through each pair's middle)."""
        self.points = list(points)
        path = QPainterPath(self.points[0] if self.points else QPointF())
        if len(self.points) == 1:
            path.lineTo(self.points[0])  # (a dot)
        for before, point in zip(self.points, self.points[1:]):
            path.quadTo(before, (before + point) / 2.0)
        if len(self.points) > 1:
            path.lineTo(self.points[-1])
        self.setPath(path)

    def shape(self) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(self.width + 2 * INK_HIT)
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        return stroker.createStroke(self.path())

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: Optional[QWidget] = None) -> None:
        plain = QStyleOptionGraphicsItem(option)
        plain.state &= ~QStyle.StateFlag.State_Selected
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        super().paint(painter, plain, widget)
        if self.chosen:
            _chosen_outline(painter, self.path().boundingRect())

    def to_dict(self, page: int) -> dict[str, Any]:
        moved = self.pos()
        return {
            "id": self.mark_id, "kind": self.kind, "page": page, "color": self.color, "width": round(self.width, 3),
            "points": [[round(p.x() + moved.x(), 2), round(p.y() + moved.y(), 2)] for p in self.points],
        }


class NoteMark(QGraphicsTextItem):
    """A typed note: plain text on a coloured card, its top-left corner at pos."""

    kind = "note"

    def __init__(self, mark_id: str, text: str, color: str):
        super().__init__()
        self.mark_id, self.color = mark_id, color
        self.chosen = False
        self.on_edit_finished: Optional[Callable[["NoteMark"], None]] = None
        font = QFont()
        font.setPointSizeF(NOTE_FONT_PT)
        self.setFont(font)
        self.setDefaultTextColor(QColor("#1f2328"))
        self.document().setDocumentMargin(NOTE_PADDING)
        option = self.document().defaultTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.document().setDefaultTextOption(option)
        self.setTextWidth(NOTE_WIDTH)
        self.setPlainText(text)

    def editing(self) -> bool:
        return bool(self.textInteractionFlags() & Qt.TextInteractionFlag.TextEditorInteraction)

    def start_editing(self) -> None:
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        cursor = self.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.setTextCursor(cursor)

    def stop_editing(self) -> None:
        cursor = self.textCursor()
        cursor.clearSelection()
        self.setTextCursor(cursor)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.clearFocus()

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        if self.editing() and self.on_edit_finished is not None:
            self.on_edit_finished(self)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: Optional[QWidget] = None) -> None:
        card = self.boundingRect()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(0, 0, 0, 60), 0))
        painter.setBrush(QColor(self.color))
        painter.drawRoundedRect(card, 3, 3)
        painter.restore()
        plain = QStyleOptionGraphicsItem(option)
        plain.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_HasFocus)
        super().paint(painter, plain, widget)
        if self.chosen:
            _chosen_outline(painter, card)

    def to_dict(self, page: int) -> dict[str, Any]:
        return {
            "id": self.mark_id, "kind": self.kind, "page": page, "color": self.color,
            "x": round(self.pos().x(), 2), "y": round(self.pos().y(), 2), "text": self.toPlainText()[:MAX_NOTE_TEXT],
        }


Mark = HighlightMark | InkMark | NoteMark
MARK_TYPES = (HighlightMark, InkMark, NoteMark)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and abs(number) < 1e7 else default  # (not NaN, not absurd)


def _color(value: Any, default: str) -> str:
    return value if isinstance(value, str) and QColor(value).isValid() else default


def from_dict(data: dict[str, Any]) -> Optional[Mark]:
    """A mark from what to_dict made (None if it's damaged). Its page is data["page"]."""
    if not isinstance(data, dict) or not isinstance(data.get("page"), int):
        return None
    mark_id = data.get("id") if isinstance(data.get("id"), str) and data.get("id") else new_id()
    kind = data.get("kind")
    if kind == "highlight":
        boxes = []
        for box in (data.get("boxes") or [])[:MAX_BOXES]:
            if isinstance(box, list) and len(box) == 4:
                rect = QRectF(*(_number(v) for v in box))
                if rect.width() > 0 and rect.height() > 0:
                    boxes.append(rect)
        text = data.get("text")
        return HighlightMark(mark_id, boxes, _color(data.get("color"), "#ffd400"), text if isinstance(text, str) else "") if boxes else None
    if kind == "ink":
        points = [QPointF(_number(p[0]), _number(p[1])) for p in (data.get("points") or [])[:MAX_POINTS] if isinstance(p, list) and len(p) == 2]
        width = min(200.0, max(0.1, _number(data.get("width"), 2.0)))
        return InkMark(mark_id, points, _color(data.get("color"), "#e5484d"), width) if points else None
    if kind == "note":
        text = data.get("text")
        note = NoteMark(mark_id, text[:MAX_NOTE_TEXT] if isinstance(text, str) else "", _color(data.get("color"), "#fff3a3"))
        note.setPos(_number(data.get("x")), _number(data.get("y")))
        return note
    return None


class MarksCommand(QUndoCommand):
    """Marks changed: before / after are their dicts (to_dict), matched by id. Redo puts the after
    ones in place of the before ones; undo the other way round. apply(remove_ids, add_dicts)."""

    def __init__(self, apply: Callable[[list[str], list[dict]], None], before: list[dict], after: list[dict], text: str):
        super().__init__(text)
        self.apply, self.before, self.after = apply, before, after

    def redo(self) -> None:
        self.apply([d["id"] for d in self.before], self.after)

    def undo(self) -> None:
        self.apply([d["id"] for d in self.after], self.before)
