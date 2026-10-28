"""Toolbars that go compact when their panel gets narrow (e.g. a side of Split View).

CompactToolbar: when its panel is narrower than the toolbar needs at full size, the toolbar's
buttons drop their text (icon only; tooltips stay) and optional extras hide; when there's room
again, everything comes back. The panel is never made narrower than the compact toolbar, and
the toolbar's full width no longer holds the panel open.

The panel's owner calls update() on resize. (A plain object, not an event filter: Qt would
call a filter while the panel is being destroyed, after Python has freed the filter's state.)
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractButton, QLayout, QSizePolicy, QToolButton, QWidget


class CompactToolbar:
    def __init__(
        self,
        panel: QWidget,
        bar: QWidget,
        buttons: Sequence[tuple[QAbstractButton, str]],
        hide: Sequence[QWidget] = (),
        on_change: Optional[Callable[[bool], None]] = None,
    ):
        self.panel, self.bar = panel, bar
        self.buttons, self.hide = list(buttons), list(hide)
        self.on_change = on_change
        self.compact = False
        self._text_width: Optional[int] = None
        bar.layout().setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        bar.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def set_compact(self, compact: bool) -> None:
        self.compact = compact
        for button, text in self.buttons:
            button.setText("" if compact else text)
            if isinstance(button, QToolButton):
                button.setToolButtonStyle(
                    Qt.ToolButtonStyle.ToolButtonIconOnly if compact else Qt.ToolButtonStyle.ToolButtonTextBesideIcon
                )
        for widget in self.hide:
            widget.setVisible(not compact)
        if self.on_change is not None:
            self.on_change(compact)

    def update(self) -> None:
        """Re-check: call on the panel's resize, and when the toolbar's contents change."""
        if self._text_width is None:
            # How much the full toolbar needs over the compact one
            self.set_compact(True)
            compact_width = self.bar.minimumSizeHint().width()
            self.set_compact(False)
            self._text_width = self.bar.minimumSizeHint().width() - compact_width
        full_width = self.bar.minimumSizeHint().width() + (self._text_width if self.compact else 0)
        self.panel.setMinimumWidth(full_width - self._text_width)
        compact = self.panel.width() < full_width
        if compact != self.compact:
            self.set_compact(compact)
