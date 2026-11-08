"""New Profile: a name, and whether it starts as a copy of a profile or empty (core.profiles)."""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core import profiles
from core.profiles import Profile, ProfileError


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
        layout = QVBoxLayout(self)
        intro = QLabel(
            "A profile has its own library folders, file types, categories and collections. "
            "Tags, favorites and ratings are shared by every profile."
        )
        intro.setWordWrap(True)
        intro.setObjectName("mutedLabel")
        layout.addWidget(intro)

        layout.addWidget(QLabel("Name"))
        self.txt_name = QLineEdit()
        self.txt_name.setPlaceholderText("ZBrush, Substance Painter, Client Project...")
        self.txt_name.setMaxLength(profiles.MAX_NAME_LENGTH)
        self.txt_name.textChanged.connect(self._validate)
        layout.addWidget(self.txt_name)

        layout.addWidget(QLabel("Start with"))
        row = QHBoxLayout()
        self.btn_copy = QPushButton(f"A Copy of {copy_from.name}")
        self.btn_copy.setToolTip(f"The folders, file types and categories of the {copy_from.name} profile, to change from there")
        self.btn_empty = QPushButton("Empty")
        self.btn_empty.setToolTip("No library folders or categories yet (the sidebar has All Assets and Favorites), with the default file types")
        group = QButtonGroup(self)
        group.setExclusive(True)
        for button in (self.btn_copy, self.btn_empty):
            button.setCheckable(True)
            group.addButton(button)
            row.addWidget(button)
        row.addStretch(1)
        self.btn_copy.setChecked(True)
        layout.addLayout(row)

        self.lbl_error = QLabel()
        self.lbl_error.setObjectName("footerWarning")
        self.lbl_error.setWordWrap(True)
        layout.addWidget(self.lbl_error)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Create")
        ok.setToolTip("Make the profile")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setToolTip("Don't make a profile")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
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
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(valid)
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
