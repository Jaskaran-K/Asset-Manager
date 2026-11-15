"""New Profile: a name, and whether it starts as a copy of a profile or empty (core.profiles)."""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QWidget

from core import profiles
from core.profiles import Profile, ProfileError
from ui.controls import DialogFrame, section_heading, segmented


class NewProfileDialog(QDialog):
    """Asks for a new profile's name and what it starts with. taken: the profiles its name must
    differ from (default: the saved ones)."""

    def __init__(
        self,
        copy_from: Profile,
        parent: Optional[QWidget] = None,
        taken: Optional[list[Profile]] = None,
        title: str = "New Profile",
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        self._taken = taken
        self.copy_from = copy_from
        frame = DialogFrame(self, title, "A profile has its own library folders, file types, categories and collections. "
                                         "Tags, favorites and ratings are shared by every profile.")
        layout = frame.body
        layout.addWidget(section_heading("Name"))
        self.txt_name = QLineEdit()
        self.txt_name.setPlaceholderText("ZBrush, Substance Painter, Client Project...")
        self.txt_name.setMaxLength(profiles.MAX_NAME_LENGTH)
        self.txt_name.setToolTip("The profile's name, shown on the profile button")
        self.txt_name.textChanged.connect(self._validate)
        layout.addWidget(self.txt_name)

        layout.addWidget(section_heading("Start with"))
        track, group = segmented([
            ("copy", f"A Copy of {copy_from.name}",
             f"The folders, file types and categories of the {copy_from.name} profile, to change from there"),
            ("empty", "Empty", "No library folders or categories yet (the sidebar has All Assets and Favorites), with the "
                               "default file types"),
        ], "copy", self)
        self.btn_copy, self.btn_empty = group.buttons()
        row = QHBoxLayout()
        row.addWidget(track)
        row.addStretch(1)
        layout.addLayout(row)

        self.lbl_error = QLabel()
        self.lbl_error.setObjectName("footerWarning")
        self.lbl_error.setWordWrap(True)
        layout.addWidget(self.lbl_error)
        self.btn_create = frame.add_footer("Create", "Make the profile", cancel_tip="Don't make a profile").btn_primary
        self._validate()

    def name(self) -> str:
        return " ".join(self.txt_name.text().split())

    def copy_of(self) -> Optional[str]:
        """The profile to copy, or None to start empty."""
        return self.copy_from.id if self.btn_copy.isChecked() else None

    def _validate(self) -> bool:
        error = ""
        if self.name():
            try:
                profiles.validate_name(self.name(), taken=self._taken)
            except ProfileError as exc:
                error = str(exc)
        self.lbl_error.setText(error)
        self.lbl_error.setVisible(bool(error))
        valid = bool(self.name()) and not error
        self.btn_create.setEnabled(valid)
        return valid

    def accept(self) -> None:
        if self._validate():
            super().accept()


def ask_new_profile(parent: QWidget) -> Optional[Profile]:
    """Ask for a new profile and make it (not switched to): the profile, or None if cancelled."""
    dialog = NewProfileDialog(profiles.current_profile(), parent)
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        try:
            return profiles.create_profile(dialog.name(), dialog.copy_of())
        except ProfileError as exc:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.warning(parent, "Can't Make Profile", str(exc))
            return None
    finally:
        dialog.deleteLater()
