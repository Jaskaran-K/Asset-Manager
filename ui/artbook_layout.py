"""Layout maths for the Artbook board: auto-arrange, align, distribute and same size.

Pure functions on (id, bounds) pairs, where bounds is an image's box on the board as it
shows (rotation included). They return how far to move each image (or what to scale it
to); ui.artbook_board applies that as one undoable step.
"""

from __future__ import annotations

import math
from statistics import median
from typing import Hashable, Sequence

from PySide6.QtCore import QPointF, QRectF

Boxes = Sequence[tuple[Hashable, QRectF]]

ARRANGE_ASPECT = 1.6  # arranged rows aim for a block about this much wider than tall
GAP_SHARE = 0.03  # space between arranged images: this share of a typical image's height


def gap_for(boxes: Boxes) -> float:
    return max(4.0, median(box.height() for _id, box in boxes) * GAP_SHARE)


def reading_order(boxes: Boxes) -> list[Hashable]:
    """Ids in the order you'd read the images: rows top to bottom, each row left to right.

    An image joins the current row if its top is above the middle of the row's first image,
    so slightly uneven rows still read as rows."""
    remaining = sorted(boxes, key=lambda pair: (pair[1].top(), pair[1].left()))
    order: list[Hashable] = []
    while remaining:
        first = remaining[0][1]
        row = [pair for pair in remaining if pair[1].top() < first.center().y()]
        order += [item_id for item_id, _box in sorted(row, key=lambda pair: pair[1].left())]
        in_row = {item_id for item_id, _box in row}
        remaining = [pair for pair in remaining if pair[0] not in in_row]
    return order


def arrange(boxes: Boxes, gap: float | None = None) -> dict[Hashable, QPointF]:
    """Auto-arrange: rows of about equal width, in reading order, sizes kept.

    Returns each image's move. The block starts at the top-left of where the images were."""
    if not boxes:
        return {}
    gap = gap_for(boxes) if gap is None else gap
    by_id = dict(boxes)
    area = sum((box.width() + gap) * (box.height() + gap) for box in by_id.values())
    widest = max(box.width() for box in by_id.values())
    row_width = max(widest, math.sqrt(area * ARRANGE_ASPECT))
    left = min(box.left() for box in by_id.values())
    top = min(box.top() for box in by_id.values())

    moves: dict[Hashable, QPointF] = {}
    x, y, row_height = left, top, 0.0
    for item_id in reading_order(boxes):
        box = by_id[item_id]
        if x > left and x - left + box.width() > row_width:
            x, y, row_height = left, y + row_height + gap, 0.0  # next row
        moves[item_id] = QPointF(x - box.left(), y - box.top())
        x += box.width() + gap
        row_height = max(row_height, box.height())
    return moves


ALIGN_EDGES = ("left", "center", "right", "top", "middle", "bottom")


def align(boxes: Boxes, edge: str) -> dict[Hashable, QPointF]:
    """Line images up on one edge (or centre line) of the box around them all."""
    if edge not in ALIGN_EDGES:
        raise ValueError(edge)
    around = QRectF()
    for _id, box in boxes:
        around = around.united(box)
    moves = {}
    for item_id, box in boxes:
        if edge == "left":
            moves[item_id] = QPointF(around.left() - box.left(), 0)
        elif edge == "center":
            moves[item_id] = QPointF(around.center().x() - box.center().x(), 0)
        elif edge == "right":
            moves[item_id] = QPointF(around.right() - box.right(), 0)
        elif edge == "top":
            moves[item_id] = QPointF(0, around.top() - box.top())
        elif edge == "middle":
            moves[item_id] = QPointF(0, around.center().y() - box.center().y())
        else:
            moves[item_id] = QPointF(0, around.bottom() - box.bottom())
    return moves


def distribute(boxes: Boxes, horizontal: bool) -> dict[Hashable, QPointF]:
    """Equal gaps between images, the outermost two staying where they are."""
    if len(boxes) < 3:
        return {item_id: QPointF() for item_id, _box in boxes}
    start = (lambda box: box.left()) if horizontal else (lambda box: box.top())
    length = (lambda box: box.width()) if horizontal else (lambda box: box.height())
    ordered = sorted(boxes, key=lambda pair: start(pair[1]))
    first, last = ordered[0][1], ordered[-1][1]
    span = start(last) + length(last) - start(first)
    gap = (span - sum(length(box) for _id, box in ordered)) / (len(ordered) - 1)
    moves, position = {}, start(first)
    for item_id, box in ordered:
        shift = position - start(box)
        moves[item_id] = QPointF(shift, 0) if horizontal else QPointF(0, shift)
        position += length(box) + gap
    return moves


def same_size(sizes: Sequence[tuple[Hashable, float]]) -> float:
    """The shared size for Same Height / Same Width: the middle one (median) of the sizes."""
    return median(size for _id, size in sizes)
