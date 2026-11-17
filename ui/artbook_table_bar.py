"""The bar that floats above the selected table on the Artbook board: add a row or a column, the outlined cell's
column (its kind: text, numbers with a total, status chips; add, move, delete) and row (add, move, delete), the
column's alignment (left, center, right), the header row and the title on or off, and delete. The board
(ui.artbook_board) makes the changes, one undo step each.

Like the note bar, nothing here takes the keyboard focus (typing in a cell carries on).

Also here: StatusDialog (Edit Statuses...), a table's statuses in one place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLineEdit, QMenu, QToolButton, QVBoxLayout, QWidget

from ui import shortcuts
from ui.artbook_note_bar import swatch_icon
from ui.controls import DialogFrame, DropButton, GhostButton, soft_shadow
from ui.duotone import set_icon

if TYPE_CHECKING:
    from ui.artbook_board import BoardView

KINDS = {"text": "Text", "number": "Number", "status": "Status"}
ALIGNS = (("left", "align_left", "Left"), ("center", "align_center", "Center"), ("right", "align_right", "Right"))
MAX_STATUS_NAME = 200


class TableBar(QFrame):
    """See the module docstring. sync() shows the table's settings."""

    def __init__(self, board: "BoardView"):
        super().__init__(board)
        self.board = board
        self.setObjectName("noteBar")  # (the note bar's look)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(3)
        soft_shadow(self)

        self.btn_row = self._text_button("Row", "Add a row under the last (or hover the table: the + under it)",
                                         lambda: board.add_table_row())
        set_icon(self.btn_row, "add", size=16)
        self.btn_row.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        layout.addWidget(self.btn_row)
        self.btn_column = self._text_button("Column", "Add a column past the last (or hover the table: the + beside it)",
                                            lambda: board.add_table_column())
        set_icon(self.btn_column, "add", size=16)
        self.btn_column.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        layout.addWidget(self.btn_column)
        layout.addWidget(self._divider())

        self.btn_kind = DropButton(None, "Column: Text",
                                   "The outlined cell's column: text, numbers (with a total) or status chips; add, move or delete it")
        self.btn_kind.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_kind.menu().aboutToShow.connect(self._fill_column_menu)
        layout.addWidget(self.btn_kind)
        self.btn_rows = DropButton(None, "Row", "The outlined cell's row: add one above or below, move it, delete it")
        self.btn_rows.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_rows.menu().aboutToShow.connect(self._fill_row_menu)
        layout.addWidget(self.btn_rows)
        layout.addWidget(self._divider())

        self.align_buttons: dict[str, GhostButton] = {}
        for align, icon, name in ALIGNS:
            button = GhostButton()
            set_icon(button, icon, size=16)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setCheckable(True)
            button.setFixedSize(30, 28)
            button.setToolTip(f"Align {name}: the outlined cell's column")
            button.setProperty("align", align)
            button.clicked.connect(self._on_align_clicked)
            layout.addWidget(button)
            self.align_buttons[align] = button
        layout.addWidget(self._divider())

        self.btn_header = self._text_button("Header", "The header row: the columns' names (on / off)", lambda: board.toggle_table_header())
        self.btn_header.setCheckable(True)
        layout.addWidget(self.btn_header)
        self.btn_title = self._text_button("Title", "A title over the table (on / off)", lambda: board.toggle_table_title())
        self.btn_title.setCheckable(True)
        layout.addWidget(self.btn_title)
        layout.addWidget(self._divider())
        self.btn_delete = GhostButton()
        set_icon(self.btn_delete, "close", size=16)
        self.btn_delete.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_delete.setFixedSize(30, 28)
        self.btn_delete.setToolTip(f"Delete the table ({shortcuts.get('artbook.delete').keys})")
        self.btn_delete.clicked.connect(self._delete)
        layout.addWidget(self.btn_delete)
        self.adjustSize()
        self.hide()

    @staticmethod
    def _divider() -> QFrame:
        line = QFrame()
        line.setObjectName("noteBarDivider")
        line.setFixedSize(1, 18)
        return line

    @staticmethod
    def _text_button(text: str, tooltip: str, handler) -> QToolButton:
        button = GhostButton()
        button.setText(text)
        button.setToolTip(tooltip)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setFixedHeight(28)
        button.clicked.connect(handler)
        return button

    def _on_align_clicked(self) -> None:
        self.board.set_table_column_align(self.sender().property("align"))
        self._sync_now()

    def _sync_now(self) -> None:
        if self.board.table_bar_target() is not None:
            self.sync(self.board.table_formats())

    def _fill_column_menu(self) -> None:
        self._fill(self.btn_kind, "column")

    def _fill_row_menu(self) -> None:
        self._fill(self.btn_rows, "row")

    def _fill(self, button: DropButton, which: str) -> None:
        """The menu, as it is now (the outlined cell changes as you click about)."""
        menu = button.menu()
        menu.clear()
        table = self.board.table_bar_target()
        if table is not None:
            entries = self.board.table_column_entries(table) if which == "column" else self.board.table_row_entries(table)
            self.board._fill_menu(menu, entries)

    def _delete(self) -> None:
        self.board.finish_renaming()
        self.board.delete_selection()

    def sync(self, formats: dict[str, Any]) -> None:
        """Show the table's settings (from BoardView.table_formats)."""
        self.btn_kind.setText(f"Column: {KINDS.get(formats['kind'], 'Text')}")
        checks = [(self.btn_header, formats["header"]), (self.btn_title, formats["title"])]
        checks += [(button, align == formats["align"]) for align, button in self.align_buttons.items()]
        for button, on in checks:
            button.blockSignals(True)
            button.setChecked(on)
            button.blockSignals(False)
        for button in (self.btn_row, self.btn_column, self.btn_kind, self.btn_rows, self.btn_header, self.btn_title,
                       self.btn_delete, *self.align_buttons.values()):
            button.setEnabled(not formats["locked"])
        self.resize(self.sizeHint())

    def sizeHint(self) -> QSize:
        return self.layout().sizeHint()


class StatusDialog(QDialog):
    """Edit Statuses...: every status a table's status cells can have, to rename, recolour, add and remove at once.
    statuses() is what it was left as: (the name it had, None for a new one; its name; its colour) each."""

    def __init__(self, statuses: list[tuple[str, str]], palette: tuple[tuple[str, str], ...], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Statuses")
        self.setMinimumWidth(440)
        self.palette_colors = palette
        frame = DialogFrame(self, "Statuses", "What this table's status cells can say. Renaming one renames it in "
                                              "every cell; a removed one leaves its cells empty.")
        self.list_box = QVBoxLayout()
        self.list_box.setSpacing(6)
        frame.body.addLayout(self.list_box)
        self.rows: list[dict[str, Any]] = []
        for name, color in statuses:
            self.add_row(name, color, original=name)
        self.btn_add = GhostButton(self)
        self.btn_add.setText("Add Status")
        set_icon(self.btn_add, "add", size=16)
        self.btn_add.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_add.setToolTip("A new status, at the end of the list")
        self.btn_add.clicked.connect(self._on_add)
        frame.body.addWidget(self.btn_add, 0, Qt.AlignmentFlag.AlignLeft)
        frame.add_footer("Save", "Keep these statuses (one undo step)", cancel_tip="Leave the statuses as they were")

    def add_row(self, name: str = "", color: Optional[str] = None, original: Optional[str] = None) -> dict[str, Any]:
        """A status's line: its colour (a click picks another), its name, and an x to remove it."""
        if color is None:
            used = {row["color"] for row in self.rows}
            color = next((c for _n, c in self.palette_colors if c not in used), self.palette_colors[0][1])
        line = QWidget(self)
        layout = QHBoxLayout(line)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        swatch = QToolButton(line)
        swatch.setToolTip("Its colour: click to pick another")
        swatch.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        swatch.setIconSize(QSize(18, 18))
        swatch.setMenu(QMenu(swatch))
        edit = QLineEdit(name, line)
        edit.setPlaceholderText("Status name")
        edit.setMaxLength(MAX_STATUS_NAME)
        remove = GhostButton(line)
        set_icon(remove, "close", size=14)
        remove.setFixedSize(28, 28)
        remove.setToolTip("Remove this status (its cells are left empty)")
        row = {"widget": line, "edit": edit, "swatch": swatch, "color": color, "original": original}
        for label, value in self.palette_colors:
            action = swatch.menu().addAction(swatch_icon(value), label)
            action.setData(value)
            action.triggered.connect(self._on_color_picked)
        swatch.setProperty("row", len(self.rows))
        remove.clicked.connect(self._on_remove)
        layout.addWidget(swatch)
        layout.addWidget(edit, 1)
        layout.addWidget(remove)
        self.list_box.addWidget(line)
        self.rows.append(row)
        self._show_color(row)
        return row

    def _row_of(self, widget: QWidget) -> Optional[dict[str, Any]]:
        return next((row for row in self.rows if row["widget"] is widget or row["widget"].isAncestorOf(widget)), None)

    def _show_color(self, row: dict[str, Any]) -> None:
        row["swatch"].setIcon(swatch_icon(row["color"]))
        for action in row["swatch"].menu().actions():
            action.setCheckable(True)
            action.setChecked(action.data() == row["color"])

    def _on_color_picked(self) -> None:
        action = self.sender()
        row = self._row_of(action.parent().parent())  # (the menu's button)
        if row is not None:
            row["color"] = action.data()
            self._show_color(row)

    def _on_remove(self) -> None:
        row = self._row_of(self.sender())
        if row is not None:
            self.remove_row(row)

    def remove_row(self, row: dict[str, Any]) -> None:
        self.rows.remove(row)
        row["widget"].hide()
        row["widget"].deleteLater()

    def _on_add(self) -> None:
        row = self.add_row()
        row["edit"].setFocus()

    def statuses(self) -> list[tuple[Optional[str], str, str]]:
        return [(row["original"], row["edit"].text(), row["color"]) for row in self.rows]
