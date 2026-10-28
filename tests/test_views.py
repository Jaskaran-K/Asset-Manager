"""Tree row highlights, smooth wheel scrolling and animated trees (ui.views)."""

from __future__ import annotations

import time

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel, QWheelEvent
from PySide6.QtWidgets import QApplication, QListView, QPlainTextEdit, QTreeWidget, QTreeWidgetItem

from ui.animations import set_motion_mode
from ui.themes import apply_theme, build_stylesheet
from ui.views import RowHighlightDelegate, install_view_effects


@pytest.fixture(autouse=True)
def effects(qapp):
    apply_theme("dark", qapp)
    install_view_effects(qapp)
    set_motion_mode("on")
    yield
    set_motion_mode("on")


def pump(qapp, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)


def wheel(widget, notches=-1, modifiers=Qt.KeyboardModifier.NoModifier):
    """One mouse-wheel notch (negative = scroll down)."""
    pos = QPointF(widget.width() / 2, widget.height() / 2)
    event = QWheelEvent(
        pos, widget.mapToGlobal(pos), QPoint(0, 0), QPoint(0, 120 * notches),
        Qt.MouseButton.NoButton, modifiers, Qt.ScrollPhase.NoScrollPhase, False,
    )
    QApplication.sendEvent(widget, event)
    return event


def long_list(qapp):
    view = QListView()
    model = QStandardItemModel(view)
    for i in range(300):
        model.appendRow(QStandardItem(f"row {i}"))
    view.setModel(model)
    view.resize(200, 400)
    view.show()
    qapp.processEvents()
    return view


def test_stylesheet_paints_no_tree_item_backgrounds():
    # Those also land in the indentation left of the row (the stray square); the delegate draws them
    for theme in ("dark", "light", "dark_purple"):
        sheet = build_stylesheet(theme)
        assert "QTreeWidget::item:hover" not in sheet
        assert "QTreeWidget::item:selected" not in sheet


def test_trees_get_the_row_highlight_and_animate(qapp):
    tree = QTreeWidget()
    tree.show()
    qapp.processEvents()
    assert isinstance(tree.itemDelegate(), RowHighlightDelegate)
    assert tree.isAnimated()
    assert tree.verticalScrollMode() == QTreeWidget.ScrollMode.ScrollPerPixel
    set_motion_mode("off")
    assert not tree.isAnimated()


def test_selection_highlight_stays_out_of_the_indentation(qapp):
    tree = QTreeWidget()
    tree.setHeaderHidden(True)
    tree.setIndentation(24)
    tree.resize(220, 160)
    parent = QTreeWidgetItem(tree, ["Parent"])
    child = QTreeWidgetItem(parent, ["Child"])
    QTreeWidgetItem(parent, ["Other"])
    parent.setExpanded(True)
    tree.setCurrentItem(child)
    tree.show()
    qapp.processEvents()
    image = tree.viewport().grab().toImage()
    rect = tree.visualItemRect(child)
    y = rect.center().y()
    indentation = image.pixelColor(rect.left() - 10, y)
    background = image.pixelColor(rect.left() - 10, tree.visualItemRect(parent.child(1)).center().y())
    highlight = image.pixelColor(rect.left() + 20, y)
    assert indentation == background  # no square beside the selected row
    assert highlight != background  # but the row itself is highlighted


def test_wheel_glides_instead_of_jumping(qapp):
    view = long_list(qapp)
    bar = view.verticalScrollBar()
    assert wheel(view.viewport()).isAccepted()
    assert bar.value() < 30  # still on its way
    pump(qapp, 0.5)
    assert bar.value() >= 100
    # Notches in quick succession add up
    start = bar.value()
    wheel(view.viewport())
    wheel(view.viewport())
    pump(qapp, 0.5)
    assert bar.value() >= start + 200


def test_wheel_at_the_end_is_left_to_the_parent(qapp):
    view = long_list(qapp)
    bar = view.verticalScrollBar()
    bar.setValue(bar.maximum())
    event = wheel(view.viewport(), notches=-1)
    pump(qapp, 0.1)
    assert bar.value() == bar.maximum()
    assert not event.isAccepted()  # propagates, e.g. to a scroll area around the list


def test_motion_off_scrolls_the_usual_way(qapp):
    set_motion_mode("off")
    view = long_list(qapp)
    wheel(view.viewport())
    assert view.verticalScrollBar().value() > 0  # straight there, no animation


def test_text_editors_and_ctrl_wheel_are_left_alone(qapp):
    editor = QPlainTextEdit("\n".join(f"line {i}" for i in range(500)))
    editor.resize(200, 200)
    editor.show()
    qapp.processEvents()
    wheel(editor.viewport())
    assert editor.verticalScrollBar().value() > 0  # Qt's own line scrolling, done at once
    view = long_list(qapp)
    wheel(view.viewport(), modifiers=Qt.KeyboardModifier.ControlModifier)
    assert view.verticalScrollBar().value() > 0  # Ctrl+wheel: Qt's own handling (a page at once), no glide


def test_hovered_row_is_highlighted(qapp):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QHoverEvent

    tree = QTreeWidget()
    tree.setHeaderHidden(True)
    tree.resize(220, 160)
    first, second = QTreeWidgetItem(tree, ["First"]), QTreeWidgetItem(tree, ["Second"])
    tree.show()
    qapp.processEvents()
    viewport = tree.viewport()
    assert viewport.testAttribute(Qt.WidgetAttribute.WA_Hover)  # without it Qt never reports the hovered row
    rect = tree.visualItemRect(second)
    pos = QPointF(rect.center())
    QApplication.sendEvent(viewport, QHoverEvent(QEvent.Type.HoverEnter, pos, viewport.mapToGlobal(pos), QPointF(-1, -1)))
    QApplication.sendEvent(viewport, QHoverEvent(QEvent.Type.HoverMove, pos, viewport.mapToGlobal(pos), pos))
    qapp.processEvents()
    image = viewport.grab().toImage()
    x = rect.right() - 12  # past the text, on the row's own background
    hovered = image.pixelColor(x, rect.center().y())
    plain = image.pixelColor(x, tree.visualItemRect(first).center().y())
    assert hovered != plain


def test_rename_editor_gets_the_rows_full_height(qapp):
    # The row's padding used to squeeze the editor until its text was cut off (Settings > Categories too)
    from PySide6.QtWidgets import QLineEdit

    tree = QTreeWidget()
    tree.setHeaderHidden(True)
    item = QTreeWidgetItem(tree, ["glTF"])
    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
    tree.resize(300, 200)
    tree.show()
    qapp.processEvents()
    tree.editItem(item, 0)
    qapp.processEvents()
    editor = tree.viewport().findChild(QLineEdit)
    row = tree.visualItemRect(item)
    assert editor.objectName() == "inlineEditor"
    assert editor.height() == row.height() - 4
    assert editor.height() >= editor.fontMetrics().height() + 4  # the text fits
    assert editor.geometry().right() >= row.right() - 2 * RowHighlightDelegate.INSET
    tree.close()
