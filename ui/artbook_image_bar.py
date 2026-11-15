"""The bar under the selected images on the Artbook board (a trial: everything here is also in the right-click
menu, which keeps all of it).

One click for what's otherwise up to four menus deep: Crop, Flip Horizontally / Vertically; Adjust (Grayscale,
Pixelated, Values, Guides, Opacity); Arrange (Auto-Arrange, Align, Distribute, Stack, Match Size; for two or more);
Group / Ungroup; Upscale and Remove Background (for images they work on); and More (Reset, Bring to Front / Send to
Back, Caption, Revert to Original, File, Export, Delete). The dropdowns' entries are the right-click menu's own
(BoardView.image_adjust_entries and the like), made each time they open, so they show what's true now.

Like the note bar, nothing here takes the keyboard focus. The board (ui.artbook_board) places it and shows it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QMenu, QToolButton

from ui import shortcuts
from ui.controls import DropButton, bar_separator, ghost_button, soft_shadow

if TYPE_CHECKING:
    from ui.artbook_board import BoardView


def _keys(shortcut_id: str) -> str:
    return shortcuts.get(shortcut_id).keys


class ImageBar(QFrame):
    """See the module docstring. sync() shows what applies to the selection."""

    def __init__(self, board: "BoardView"):
        super().__init__(board)
        self.board = board
        self.setObjectName("imageBar")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        soft_shadow(self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(3)

        self.btn_crop = self._button("crop", f"Crop: drag the image's handles ({_keys('artbook.crop')}; Enter keeps it)", board.start_crop)
        self.btn_flip_h = self._button("flip_h", f"Flip Horizontally ({_keys('artbook.flip_h')})", lambda: board.flip(True))
        self.btn_flip_v = self._button("flip_v", f"Flip Vertically ({_keys('artbook.flip_v')})", lambda: board.flip(False))
        for button in (self.btn_crop, self.btn_flip_h, self.btn_flip_v):
            layout.addWidget(button)
        layout.addWidget(bar_separator(18))

        self.btn_adjust = self._dropdown(
            DropButton("palette", "Adjust", "Adjust: Grayscale, Pixelated, Values, Guides and Opacity"), board.image_adjust_entries,
        )
        layout.addWidget(self.btn_adjust)
        self.btn_arrange = self._dropdown(
            DropButton("align_objects", "Arrange", "Arrange: Auto-Arrange, Align, Distribute, Stack and Match Size"),
            board.image_arrange_entries,
        )
        layout.addWidget(self.btn_arrange)
        self.btn_group = ghost_button(None, f"Group ({_keys('artbook.group')}): selected, moved and arranged as one", "Group")
        self.btn_group.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_group.clicked.connect(self._group_clicked)
        layout.addWidget(self.btn_group)

        self._ai_separator = bar_separator(18)
        layout.addWidget(self._ai_separator)
        self.btn_upscale = self._button("upscale", "Upscale: make the selected images bigger and sharper (AI)", board.upscale_selection)
        self.btn_cutout = self._button("cutout", "Remove Background: cut the subject out (AI)", lambda: board.upscale_selection(cutout=True))
        layout.addWidget(self.btn_upscale)
        layout.addWidget(self.btn_cutout)
        layout.addWidget(bar_separator(18))

        self.btn_more = ghost_button("more", "More: Reset, Bring to Front / Send to Back, Caption, File, Export and Delete")
        self.btn_more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_more.setMenu(QMenu(self.btn_more))
        self._dropdown(self.btn_more, board.image_more_entries)
        layout.addWidget(self.btn_more)
        self.adjustSize()
        self.hide()

    # ------------------ building ------------------
    def _button(self, icon: str, tooltip: str, handler: Callable[[], object]) -> QToolButton:
        button = ghost_button(icon, tooltip)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(lambda _checked=False: self._run(handler))
        return button

    def _dropdown(self, button: QToolButton, entries: Callable[[QMenu], list]) -> QToolButton:
        """Fill button's menu with entries(menu) as it opens (the right-click menu's own entries)."""
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        menu = button.menu()
        menu.setToolTipsVisible(True)

        def fill() -> None:
            menu.clear()
            self.board._fill_menu(menu, entries(menu))

        menu.aboutToShow.connect(fill)
        # (its actions carry shortcut hints: let them go once it's closed, after the one picked has run)
        menu.aboutToHide.connect(lambda: QTimer.singleShot(0, self._after_menu))
        return button

    def _after_menu(self) -> None:
        for button in (self.btn_adjust, self.btn_arrange, self.btn_more):
            if not button.menu().isVisible():
                button.menu().clear()
        self.sync()

    # ------------------ actions ------------------
    def _run(self, handler: Callable[[], object]) -> None:
        handler()
        self.sync()

    def _group_clicked(self) -> None:
        if self._one_group():
            self.board.ungroup_selection()
        else:
            self.board.group_selection()
        self.sync()

    def _one_group(self) -> bool:
        items = self.board.selected()
        groups = {item.group for item in items}
        return len(groups) == 1 and None not in groups and "" not in groups

    # ------------------ showing ------------------
    def sync(self) -> None:
        """Show what applies to the selection: Crop for one image, Arrange and Group for two or more, Ungroup for a
        group, Upscale and Remove Background for images they work on."""
        board = self.board
        items, images = board.selected(), board.selected_images()
        self.btn_crop.setVisible(len(items) == 1 and len(images) == 1)
        self.btn_arrange.setVisible(len(items) >= 2)
        one_group = self._one_group()
        self.btn_group.setVisible(len(items) >= 2 or one_group)
        self.btn_group.setText("Ungroup" if one_group else "Group")
        self.btn_group.setToolTip(f"Ungroup ({_keys('artbook.ungroup')}): everything stays put" if one_group
                                  else f"Group ({_keys('artbook.group')}): selected, moved and arranged as one")
        ai = bool(board.upscale_candidates())
        for widget in (self._ai_separator, self.btn_upscale, self.btn_cutout):
            widget.setVisible(ai)
        self.resize(self.sizeHint())

    def sizeHint(self) -> QSize:
        return self.layout().sizeHint()
