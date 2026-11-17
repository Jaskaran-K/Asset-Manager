"""Exporting an Artbook page as a picture or a PDF: the Export dialog (Export Options..., Ctrl+Shift+E) and the words
for the quick Export menu's settings line (ui.artbook's top bar: each format in one click, with these settings).

The dialog: a preview (what's loaded, quickly: the export itself uses the sharp pictures), then the format (PNG, JPG,
PDF), what (the whole page, the selection, what's in view), the size, the background, and whether the page's header,
the comments' pins and (PDF) clickable cards go in. Its choices are remembered (core.settings: get_artbook_export).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.settings import get_artbook_export, set_artbook_export
from ui import shortcuts
from ui.controls import segmented

if TYPE_CHECKING:
    from ui.artbook_board import BoardView

PREVIEW_SIDE = 460
FORMATS = (("png", "PNG"), ("jpg", "JPG"), ("pdf", "PDF"))
SIZES = (("1", "1×"), ("2", "2×"), ("4", "4×"), ("full", "Pictures' full size"))
BACKGROUNDS = (("board", "Board colour"), ("white", "White"), ("clear", "See-through"))
AREA_WORDS = {"page": "Whole page", "selection": "The selection", "view": "What's in view", "auto": "The selection, or the whole page"}
SIZE_WORDS = {"1": "1×", "2": "2×", "4": "4×", "full": "pictures' full size"}
BACKGROUND_WORDS = {"board": "board colour", "white": "white", "clear": "see-through"}


def settings_text(options: Optional[dict] = None) -> str:
    """The export settings in a line ("Whole page · 2× · board colour · with the title")."""
    options = options or get_artbook_export()
    parts = [AREA_WORDS[options["area"]], SIZE_WORDS[options["size"]], BACKGROUND_WORDS[options["background"]],
             "with the title" if options["title"] else "no title"]
    if options["pins"]:
        parts.append("comment pins")
    return " · ".join(parts)


class ExportDialog(QDialog):
    """See the module docstring. options() once accepted (or copied): what was chosen."""

    def __init__(self, board: "BoardView", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.board = board
        self.setWindowTitle("Export")
        self.action = ""  # "export" / "copy", as closed
        saved = get_artbook_export()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(24)
        left = QVBoxLayout()
        self.preview = QLabel()
        self.preview.setObjectName("exportPreview")
        self.preview.setFixedSize(QSize(PREVIEW_SIDE + 20, PREVIEW_SIDE + 20))
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setFrameShape(QFrame.Shape.StyledPanel)
        left.addWidget(self.preview)
        self.size_note = QLabel()
        self.size_note.setObjectName("mutedLabel")
        self.size_note.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        left.addWidget(self.size_note)
        left.addStretch(1)
        layout.addLayout(left)
        right = QVBoxLayout()
        right.setSpacing(6)
        self.formats = self._choice(right, "FORMAT", FORMATS, saved["format"])
        right.addWidget(self._head("WHAT"))
        self.areas = QButtonGroup(self)
        count = len(board.selected())
        self.rb_page = self._radio(right, "The whole page", "page")
        self.rb_selection = self._radio(right, f"The selection ({count} thing{'s' if count != 1 else ''})" if count else "The selection (none)", "selection")
        self.rb_view = self._radio(right, "What's in view", "view")
        self.rb_selection.setEnabled(bool(count))
        area = saved["area"] if saved["area"] != "auto" else ("selection" if count else "page")
        if area == "selection" and not count:
            area = "page"
        {"page": self.rb_page, "selection": self.rb_selection, "view": self.rb_view}[area].setChecked(True)
        self.sizes = self._choice(right, "SIZE", SIZES, saved["size"])
        self.backgrounds = self._choice(right, "BACKGROUND", BACKGROUNDS, saved["background"])
        right.addWidget(self._head("ALSO"))
        self.cb_title = QCheckBox("The page title (its header)", self)
        self.cb_title.setToolTip("The page's header: its name, the line about it, what's on it")
        self.cb_title.setChecked(saved["title"])
        self.cb_title.setEnabled(board.title_note() is not None)
        self.cb_pins = QCheckBox("Comment pins (numbered, with a list of them)", self)
        self.cb_pins.setToolTip("The comments' pins on it, numbered, and what they say under it (a PDF: on a second page)")
        self.cb_pins.setChecked(saved["pins"])
        self.cb_pins.setEnabled(bool(board.comment_threads()))
        self.cb_links = QCheckBox("PDF: link and file cards stay clickable", self)
        self.cb_links.setToolTip("Clicking a link card in the PDF opens its page; a file card, its file")
        self.cb_links.setChecked(saved["links"])
        for box in (self.cb_title, self.cb_pins, self.cb_links):
            right.addWidget(box)
            box.toggled.connect(self._changed)
        right.addStretch(1)
        buttons = QHBoxLayout()
        self.btn_copy = QPushButton("Copy as Image")
        self.btn_copy.setToolTip(f"Copy it, to paste in another app ({shortcuts.get('artbook.copy_image').keys})")
        self.btn_copy.clicked.connect(self._on_copy)
        buttons.addWidget(self.btn_copy)
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setToolTip("Don't export")
        cancel.clicked.connect(self.reject)
        self.btn_export = QPushButton("Export...")
        self.btn_export.setObjectName("primaryBtn")
        self.btn_export.setToolTip("Choose where to save it")
        self.btn_export.setDefault(True)
        self.btn_export.clicked.connect(self._on_export)
        buttons.addWidget(cancel)
        buttons.addWidget(self.btn_export)
        right.addLayout(buttons)
        layout.addLayout(right, 1)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(60)
        self.timer.timeout.connect(self.refresh)
        self._sync()
        self.refresh()

    # ------------------ building ------------------
    @staticmethod
    def _head(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("imageListTitle")
        return label

    def _choice(self, layout: QVBoxLayout, head: str, choices, current: str) -> QButtonGroup:
        layout.addWidget(self._head(head))
        track, group = segmented([(value, text, f"{head.title()}: {text}") for value, text in choices], current, self)
        track.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        for button in group.buttons():
            button.clicked.connect(self._changed)
        row = QHBoxLayout()
        row.addWidget(track)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addSpacing(6)
        return group

    def _radio(self, layout: QVBoxLayout, text: str, value: str) -> QRadioButton:
        radio = QRadioButton(text, self)
        radio.setProperty("value", value)
        self.areas.addButton(radio)
        radio.toggled.connect(self._changed)
        layout.addWidget(radio)
        return radio

    # ------------------ choices ------------------
    @staticmethod
    def _value(group: QButtonGroup) -> str:
        button = group.checkedButton()
        return button.property("value") if button is not None else ""

    def options(self) -> dict:
        return {"format": self._value(self.formats), "area": self._value(self.areas), "size": self._value(self.sizes),
                "background": self._value(self.backgrounds), "title": self.cb_title.isChecked(),
                "pins": self.cb_pins.isChecked(), "links": self.cb_links.isChecked()}

    def set_choice(self, group: QButtonGroup, value: str) -> None:
        for button in group.buttons():
            if button.property("value") == value:
                button.setChecked(True)
        self._changed()

    def _changed(self, *_args) -> None:
        if not hasattr(self, "timer"):
            return  # (still being built: its first choices are being set)
        self._sync()
        self.timer.start()

    def _sync(self) -> None:
        """A JPG can't be see-through (it gets white); clickable cards are a PDF's."""
        fmt = self._value(self.formats)
        clear = next(b for b in self.backgrounds.buttons() if b.property("value") == "clear")
        clear.setEnabled(fmt != "jpg")
        if fmt == "jpg" and clear.isChecked():
            next(b for b in self.backgrounds.buttons() if b.property("value") == "white").setChecked(True)
        self.cb_links.setEnabled(fmt == "pdf")
        self.btn_export.setText(f"Export {dict(FORMATS)[fmt]}..." if fmt else "Export...")

    def refresh(self) -> None:
        """The preview (what's loaded: quick), and how big the export comes out."""
        options = self.options()
        image = self.board.export_image({}, options["area"], options["size"], options["title"], options["background"],
                                        options["pins"], max_side=PREVIEW_SIDE)
        if image is None:
            self.preview.setText("Nothing to export here")
            self.size_note.setText("")
            return
        pixmap = QPixmap.fromImage(image)
        self.preview.setPixmap(pixmap)
        plan = self.board.export_plan(options["area"], options["size"], options["title"])
        if plan is None:
            return
        _items, bounds, density, _requests = plan
        if options["format"] == "pdf":
            self.size_note.setText(f"A PDF page, {bounds.width() / 72:.1f} × {bounds.height() / 72:.1f} in: its text stays sharp")
        else:
            width, height = round(bounds.width() * density), round(bounds.height() * density)
            self.size_note.setText(f"{width} × {height} px")

    def _on_export(self) -> None:
        self.action = "export"
        set_artbook_export(**self.options())
        self.accept()

    def _on_copy(self) -> None:
        self.action = "copy"
        set_artbook_export(**{k: v for k, v in self.options().items() if k != "format"})
        self.accept()
