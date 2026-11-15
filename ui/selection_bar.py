"""The selection bar: the selected files' main actions, floating at the bottom of the grid while any are selected
(ui.controls.FloatingBar). "3 selected | Preview  Tag  Favorite  Send to Artbook  Find Similar  ...  x".

The More button opens the files' whole right-click menu; x clears the selection. What doesn't apply to the
selection (Find Similar with several files, Send to Artbook without pictures) is greyed out, with a tooltip.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMenu, QToolButton, QWidget

from ui.controls import FloatingBar, bar_separator, ghost_button

TEXT_ICON = 16


@dataclass(frozen=True)
class SelectionActions:
    """What the bar's buttons do (the main window's methods)."""

    preview: Callable[[], None]
    tag: Callable[[], None]
    favorite: Callable[[], None]
    fill_send_menu: Callable[[QMenu], None]  # fills Send to Artbook's menu as it opens
    find_similar: Callable[[], None]
    more_menu: Callable[[], QMenu]  # the selection's whole right-click menu
    clear: Callable[[], None]


class SelectionBar(FloatingBar):
    def __init__(self, host: QWidget, actions: SelectionActions):
        super().__init__(host, gap=16)
        self.actions = actions
        row = self.row
        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("floatingCount")
        row.addWidget(self.lbl_count)
        row.addWidget(bar_separator(22))
        self.btn_preview = ghost_button("eye", "Preview the selected file (Space)", "Preview", TEXT_ICON)
        self.btn_preview.clicked.connect(actions.preview)
        self.btn_tag = ghost_button("tag", "Add or remove tags on the selected files (Ctrl+T)", "Tag", TEXT_ICON)
        self.btn_tag.clicked.connect(actions.tag)
        self.btn_favorite = ghost_button("star", "Add the selected files to Favorites, or take them out (F)", "Favorite", TEXT_ICON)
        self.btn_favorite.clicked.connect(actions.favorite)
        self.btn_send = ghost_button("view_artbook", "Put the selected pictures on an Artbook page", "Send to Artbook", TEXT_ICON)
        self.btn_send.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.send_menu = QMenu(self.btn_send)
        self.send_menu.setToolTipsVisible(True)
        self.send_menu.aboutToShow.connect(self._fill_send_menu)
        self.btn_send.setMenu(self.send_menu)
        self.btn_similar = ghost_button("similar", "Show this picture and its look-alikes (resized or re-saved copies)", "Find Similar", TEXT_ICON)
        self.btn_similar.clicked.connect(actions.find_similar)
        self.btn_more = ghost_button("more", "More: everything in the right-click menu")
        self.btn_more.clicked.connect(self._show_more)
        for button in (self.btn_preview, self.btn_tag, self.btn_favorite, self.btn_send, self.btn_similar, self.btn_more):
            row.addWidget(button)
        row.addWidget(bar_separator(22))
        self.btn_clear = ghost_button("close", "Clear the selection", size=14, tone="text")
        self.btn_clear.clicked.connect(actions.clear)
        row.addWidget(self.btn_clear)

    def place(self) -> None:
        """At the bottom centre; with icons only when its words don't fit (a narrow side of Split View)."""
        worded = (self.btn_preview, self.btn_tag, self.btn_favorite, self.btn_send, self.btn_similar)
        for button in worded:
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        if self.natural_size().width() > self.host.width() - 8:
            for button in worded:
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        super().place()

    def show_selection(self, count: int, can_similar: bool, can_send: bool) -> None:
        """Show the bar for count selected files (0 hides it)."""
        if not count:
            self.conceal()
            return
        self.lbl_count.setText(f"{count} selected")
        self.btn_similar.setEnabled(can_similar)
        self.btn_similar.setToolTip(
            "Show this picture and its look-alikes (resized or re-saved copies)" if can_similar
            else "Find Similar works on one picture at a time"
        )
        self.btn_send.setEnabled(can_send)
        self.btn_send.setToolTip(
            "Put the selected pictures on an Artbook page" if can_send else "Only pictures go on Artbook pages"
        )
        self.reveal()

    def _fill_send_menu(self) -> None:
        self.send_menu.clear()
        self.actions.fill_send_menu(self.send_menu)

    def _show_more(self) -> None:
        menu = self.actions.more_menu()
        menu.exec(self.btn_more.mapToGlobal(self.btn_more.rect().bottomLeft()))
        menu.deleteLater()
