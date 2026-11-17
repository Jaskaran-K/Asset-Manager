"""New Artbook pages from templates: the built-in ones (made here, with real board items: a page header, columns
of to-dos, tables), and yours (core.artbook_templates: a page saved with right-click > Save as Template).

NewPageDialog is the New Page window (the New Page button, Pages' right-click New Page): a name, a preview of each
template (Blank first), then Create Page. SaveTemplateDialog asks a template's name, and whether to keep the page's
pictures and untick its to-dos.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core import artbook_store as store
from core import artbook_templates as templates
from ui.themes import theme_color

BUILT_INS = (  # (id, name, what's on it)
    ("project", "Project board", "A title, To do / Doing / Done columns of to-dos, and a time and status table"),
    ("mood", "Mood board", "A title and columns for shapes, colours and materials, to drop pictures in"),
    ("character", "Character sheet", "Front, side and back views, proportions, and the parts with their status"),
    ("review", "Review notes", "A column per session: what was said, and what to fix"),
)
PREVIEW = QSize(260, 160)
ROLE = Qt.ItemDataRole.UserRole  # a tile: (kind "blank" / "built_in" / "yours", its id)
GAP = 48.0


# ----------------------------------------------------------------------
# The built-in templates, as board items
# ----------------------------------------------------------------------
class _Builder:
    """Board items for a new page, laid out left to right under its header."""

    def __init__(self, name: str, about: str):
        from ui.artbook_board import NoteItem

        self.items: list = []
        self.z = 1.0
        title = NoteItem(uuid.uuid4().hex)
        title.set_content(("", None, "title"))
        title.set_title_text(name)
        title.set_about(about)
        title.fit_title()
        self._add(title, 0.0, 0.0)
        self.top = title.boundingRect().height() + GAP

    def _add(self, item, x: float, y: float):
        item.setPos(x, y)
        item.setZValue(self.z)
        self.z += 1.0
        self.items.append(item)
        return item

    @staticmethod
    def note(text: str, style: str = "card"):
        from ui.artbook_board import NoteItem

        note = NoteItem(uuid.uuid4().hex)
        if style != "card":
            note.set_content(("", None, style))
        note.setPlainText(text)
        note.lay_out()
        return note

    @staticmethod
    def todo(title: str, rows: tuple[str, ...]):
        """A to-do: its title, then a box to tick for each row."""
        from ui.artbook_board import NoteItem, set_paragraph

        note = NoteItem(uuid.uuid4().hex)
        document = note.document()
        cursor = QTextCursor(document)
        cursor.insertText(title)
        for row in rows:
            cursor.insertBlock()
            cursor.insertText(row)
        set_paragraph(QTextCursor(document.firstBlock()), "h2")
        lines = QTextCursor(document.findBlockByNumber(1))
        lines.setPosition(document.characterCount() - 1, QTextCursor.MoveMode.KeepAnchor)
        set_paragraph(lines, "check")
        note.lay_out()
        return note

    def column(self, title: str, x: float, cards: list) -> None:
        from ui.artbook_board import ColumnItem

        column = self._add(ColumnItem(uuid.uuid4().hex, title), x, self.top)
        for card in cards:
            self._add(card, x, self.top)
            column.members.append(card.item_id)
        return column

    def table(self, title: str, columns: list[tuple[str, str, float]], rows: list[list[str]], x: float, y: float) -> None:
        from ui.artbook_board import TableItem

        table = TableItem.from_dict({
            "id": uuid.uuid4().hex, "title": title, "header": True,
            "columns": [{"name": n, "kind": k, "width": w, "align": ""} for n, k, w in columns], "rows": rows,
        })
        self._add(table, x, y)

    def place(self, item, x: float, y: float) -> None:
        self._add(item, x, y)

    def dicts(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.items]


def built_in_items(kind: str, name: str) -> list[dict[str, Any]]:
    """A built-in template's items (as saved), for a page named name."""
    from ui.artbook_board import COLUMN_WIDTH

    step = COLUMN_WIDTH + GAP
    if kind == "project":
        page = _Builder(name, "What it is, and when it's due")
        page.column("To do", 0, [page.todo("Blockout", ("Primary shapes", "Proportions check")),
                                 page.todo("Details", ("Secondary forms", "Tertiary details"))])
        page.column("Doing", step, [page.todo("Now", ("First step",))])
        page.column("Done", 2 * step, [])
        page.table("Time", [("Task", "text", 200.0), ("Time", "number", 90.0), ("Status", "status", 130.0)],
                   [["Blockout", "", "To do"], ["Details", "", "To do"], ["Textures", "", "To do"], ["Presentation", "", "To do"]],
                   3 * step, page.top)
    elif kind == "mood":
        page = _Builder(name, "The feel, in a few words")
        for n, title in enumerate(("Shapes", "Colours", "Materials")):
            page.column(title, n * step, [page.note(f"Drop {title.lower()} here", "plain")])
        page.place(page.note("Keywords: "), 3 * step, page.top)
    elif kind == "character":
        page = _Builder(name, "Who they are, in one line")
        for n, title in enumerate(("Front", "Side", "Back")):
            page.column(title, n * step, [page.note(f"Drop the {title.lower()} view here", "plain")])
        page.place(page.note("Proportions: how many heads tall, shoulder width, hands"), 3 * step, page.top)
        page.table("Parts", [("Part", "text", 160.0), ("Status", "status", 130.0)],
                   [["Head", "To do"], ["Torso", "To do"], ["Arms", "To do"], ["Legs", "To do"], ["Props", "To do"]],
                   3 * step, page.top + 160)
    elif kind == "review":
        page = _Builder(name, "Feedback, session by session")
        for n in range(3):
            page.column(f"Session {n + 1}", n * step, [page.note("What was said"), page.todo("To fix", ("First fix",))])
    else:
        raise ValueError(f"Not a template: {kind}")
    return page.dicts()


def create_built_in(kind: str, name: str, folder_id: Optional[str] = None) -> dict[str, Any]:
    """A new page from a built-in template (UI thread: it makes board items); returns it."""
    page = store.create_page(name, folder_id)
    page["items"] = built_in_items(kind, page["name"])
    store.save_page_now(page)
    return page


# ----------------------------------------------------------------------
# Previews
# ----------------------------------------------------------------------
def _framed(image: Optional[QImage]) -> QPixmap:
    """A preview on the board's colour, fitted into PREVIEW."""
    pixmap = QPixmap(PREVIEW)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.setPen(QPen(theme_color("border"), 1))
    painter.setBrush(theme_color("grid_bg"))
    painter.drawRoundedRect(pixmap.rect().adjusted(1, 1, -1, -1), 10, 10)
    if image is not None and not image.isNull():
        fitted = image.scaled(PREVIEW - QSize(20, 20), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        painter.drawImage((PREVIEW.width() - fitted.width()) // 2, (PREVIEW.height() - fitted.height()) // 2, fitted)
    else:  # (Blank: a plus)
        pen = QPen(theme_color("text_faint"), 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        c = pixmap.rect().center()
        painter.drawLine(c.x() - 14, c.y(), c.x() + 14, c.y())
        painter.drawLine(c.x(), c.y() - 14, c.x(), c.y() + 14)
    painter.end()
    return pixmap


def preview(items: list[dict[str, Any]], name: str) -> Optional[QImage]:
    """What a page with these items looks like (a board made out of sight for it)."""
    from ui.artbook_board import BoardView

    board = BoardView()
    try:
        board.load_page({"id": f"preview-{uuid.uuid4().hex}", "name": name, "items": items})
        return board.thumbnail(PREVIEW.width() * 2, PREVIEW.height() * 2)
    finally:
        board.deleteLater()


# ----------------------------------------------------------------------
# The New Page window
# ----------------------------------------------------------------------
class NewPageDialog(QDialog):
    """See the module docstring. choice() and name() once accepted."""

    def __init__(self, parent: Optional[QWidget], folder_name: Optional[str] = None):
        super().__init__(parent)
        self.setWindowTitle("New Page")
        self.setMinimumSize(900, 600)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        row = QHBoxLayout()
        row.addWidget(QLabel("Name"))
        self.edit = QLineEdit(store.unique_page_name())
        self.edit.setMaxLength(store.MAX_NAME)
        self.edit.setToolTip("The new page's name (its header shows it)")
        self.edit.selectAll()
        row.addWidget(self.edit, 1)
        where = QLabel(f"in {folder_name}" if folder_name else "")
        where.setObjectName("mutedLabel")
        row.addWidget(where)
        layout.addLayout(row)
        self.list = QListWidget()
        self.list.setObjectName("templateList")
        self.list.setViewMode(QListView.ViewMode.IconMode)
        self.list.setIconSize(PREVIEW)
        self.list.setGridSize(QSize(PREVIEW.width() + 24, PREVIEW.height() + 70))
        self.list.setMovement(QListView.Movement.Static)
        self.list.setResizeMode(QListView.ResizeMode.Adjust)
        self.list.setWordWrap(True)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._on_menu)
        self.list.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self.list, 1)
        hint = QLabel("Save any page as a template: right-click it in Pages > Save as Template. Right-click one of yours to "
                      "rename or delete it.")
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setToolTip("Don't make a page")
        cancel.clicked.connect(self.reject)
        self.btn_create = QPushButton("Create Page")
        self.btn_create.setObjectName("primaryBtn")
        self.btn_create.setToolTip("Make the page from the template picked (Enter)")
        self.btn_create.setDefault(True)
        self.btn_create.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(self.btn_create)
        layout.addLayout(buttons)
        self._fill()
        self.edit.setFocus()

    def _tile(self, kind: str, tile_id: str, name: str, about: str, image: Optional[QImage]) -> None:
        tile = QListWidgetItem(QIcon(_framed(image)), f"{name}\n{about}")
        tile.setData(ROLE, (kind, tile_id))
        tile.setToolTip(f"{name}: {about}")
        tile.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.list.addItem(tile)

    def _fill(self) -> None:
        self.list.clear()
        self._tile("blank", "", "Blank", "An empty page, with its header", None)
        for kind, name, about in BUILT_INS:
            self._tile("built_in", kind, name, about, preview(built_in_items(kind, name), name))
        for entry in templates.list_templates():
            data = templates.thumbnail(entry["id"])
            image = QImage.fromData(data) if data else None
            about = f"Yours: from “{entry['from']}”" if entry["from"] else "Yours"
            self._tile("yours", entry["id"], entry["name"], about, image)
        self.list.setCurrentRow(0)

    def choice(self) -> tuple[str, str]:
        tile = self.list.currentItem()
        return tile.data(ROLE) if tile is not None else ("blank", "")

    def name(self) -> str:
        return " ".join(self.edit.text().split()) or store.unique_page_name()

    def _on_menu(self, pos) -> None:
        tile = self.list.itemAt(pos)
        if tile is None or tile.data(ROLE)[0] != "yours":
            return
        template_id = tile.data(ROLE)[1]
        menu = QMenu(self)
        menu.addAction("Rename...").triggered.connect(lambda: self._rename(template_id, tile.text().split("\n")[0]))
        menu.addAction("Delete...").triggered.connect(lambda: self._delete(template_id, tile.text().split("\n")[0]))
        menu.exec(self.list.viewport().mapToGlobal(pos))
        menu.deleteLater()

    def _rename(self, template_id: str, name: str) -> None:
        new, ok = QInputDialog.getText(self, "Rename Template", "Name:", text=name)
        if ok and new.strip():
            try:
                templates.rename_template(template_id, new)
            except (store.PageError, OSError, ValueError) as exc:
                QMessageBox.warning(self, "Rename Template", str(exc))
            self._fill()

    def _delete(self, template_id: str, name: str) -> None:
        answer = QMessageBox.question(self, "Delete Template", f"Delete the template “{name}”? Pages made from it stay.")
        if answer == QMessageBox.StandardButton.Yes:
            templates.delete_template(template_id)
            self._fill()


def ask_new_page(parent: Optional[QWidget], folder_name: Optional[str] = None) -> Optional[tuple[str, str, str]]:
    """The New Page window: (kind, template id, name) picked, or None (cancelled)."""
    dialog = NewPageDialog(parent, folder_name)
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        kind, template_id = dialog.choice()
        return kind, template_id, dialog.name()
    finally:
        dialog.deleteLater()


# ----------------------------------------------------------------------
# Save as Template
# ----------------------------------------------------------------------
class SaveTemplateDialog(QDialog):
    def __init__(self, parent: Optional[QWidget], page_name: str):
        super().__init__(parent)
        self.setWindowTitle("Save as Template")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(QLabel("The page as a starting point for new ones (New Page shows it)."))
        row = QHBoxLayout()
        row.addWidget(QLabel("Name"))
        self.edit = QLineEdit(f"{page_name} layout")
        self.edit.setMaxLength(store.MAX_NAME)
        self.edit.selectAll()
        row.addWidget(self.edit, 1)
        layout.addLayout(row)
        self.keep_pictures = QCheckBox("Keep its pictures", self)
        self.keep_pictures.setToolTip("Unticked: the pictures are left out (its notes, to-dos, tables and cards stay)")
        self.keep_pictures.setChecked(True)
        self.untick = QCheckBox("Untick its to-dos", self)
        self.untick.setToolTip("Every to-do starts open on a new page")
        self.untick.setChecked(True)
        layout.addWidget(self.keep_pictures)
        layout.addWidget(self.untick)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setToolTip("Don't save a template")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save Template")
        save.setObjectName("primaryBtn")
        save.setToolTip("Save it: New Page lists it with the built-in templates")
        save.setDefault(True)
        save.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)


def ask_save_template(parent: Optional[QWidget], page_name: str) -> Optional[tuple[str, bool, bool]]:
    """(name, keep pictures, untick to-dos), or None (cancelled)."""
    dialog = SaveTemplateDialog(parent, page_name)
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.edit.text().strip():
            return None
        return dialog.edit.text(), dialog.keep_pictures.isChecked(), dialog.untick.isChecked()
    finally:
        dialog.deleteLater()
