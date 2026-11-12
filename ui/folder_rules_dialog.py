"""Folder rules (core.folder_rules): the dialog that makes or changes one, and Settings > Folder Rules."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.category_rules import fold
from core.folder_rules import FolderRule
from core.tag_repository import MAX_TAG_NAME_LENGTH, TagRepository
from ui.duotone import set_icon

_RULE_ROLE = Qt.ItemDataRole.UserRole


def split_tags(text: str) -> tuple[str, ...]:
    """"WIP, Kitbash ,wip" -> ("WIP", "Kitbash"): names, spaces tidied, repeats dropped."""
    names: dict[str, str] = {}
    for part in text.split(","):
        name = " ".join(part.split())
        if name and fold(name) not in names:
            names[fold(name)] = name
    return tuple(names.values())


class FolderRuleDialog(QDialog):
    """Make or change one rule: its folder, its tags, subfolders or not, and (asked) tag the files
    already in the folder too."""

    def __init__(self, repo: TagRepository, rule: Optional[FolderRule] = None, folder: str = "",
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.repo = repo
        self.setWindowTitle("Change Folder Rule" if rule else "New Folder Rule")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        hint = QLabel(
            "Files added to the folder from now on get these tags: new files, and files copied or moved in "
            "(also while the app is closed: they're tagged at the next scan). A tag you take off a file stays off."
        )
        hint.setObjectName("pageHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addWidget(QLabel("Folder"))
        row = QHBoxLayout()
        self.txt_folder = QLineEdit(rule.folder if rule else folder)
        self.txt_folder.setPlaceholderText("The folder the rule watches")
        self.txt_folder.textChanged.connect(self._update)
        row.addWidget(self.txt_folder, 1)
        browse = QPushButton("Browse...")
        browse.setToolTip("Choose the folder")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        layout.addLayout(row)

        layout.addWidget(QLabel("Tags (separate them with commas)"))
        row = QHBoxLayout()
        self.txt_tags = QLineEdit(", ".join(rule.tags) if rule else "")
        self.txt_tags.setPlaceholderText("WIP, Kitbash")
        self.txt_tags.textChanged.connect(self._update)
        row.addWidget(self.txt_tags, 1)
        self.btn_pick = QToolButton()
        set_icon(self.btn_pick, "tag", size=16)
        self.btn_pick.setToolTip("Add one of your tags (a name you type that isn't a tag yet becomes one)")
        self.btn_pick.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_pick.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")
        menu = QMenu(self.btn_pick)
        names = [t.name for t in repo.list_tags(kind="custom")]
        for name in names:
            menu.addAction(name).triggered.connect(lambda _checked=False, n=name: self._add_tag(n))
        if not names:
            menu.addAction("No tags yet: type a name").setEnabled(False)
        self.btn_pick.setMenu(menu)
        row.addWidget(self.btn_pick)
        layout.addLayout(row)

        self.chk_subfolders = QCheckBox("Its subfolders too", self)
        self.chk_subfolders.setToolTip("Files added anywhere inside the folder, not only directly in it")
        self.chk_subfolders.setChecked(rule.subfolders if rule else True)
        self.chk_subfolders.toggled.connect(self._update)
        layout.addWidget(self.chk_subfolders)

        self.chk_existing = QCheckBox("Also tag the files already in it", self)
        self.chk_existing.setToolTip("Give the files the library already knows in this folder the tags now")
        self.chk_existing.setChecked(rule is None)
        layout.addWidget(self.chk_existing)

        self.lbl_problem = QLabel("")
        self.lbl_problem.setObjectName("footerWarning")
        self.lbl_problem.setWordWrap(True)
        layout.addWidget(self.lbl_problem)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setToolTip("Close without changing the rule")
        cancel.clicked.connect(self.reject)
        self.btn_ok = QPushButton("Save Rule")
        self.btn_ok.setObjectName("primaryBtn")
        self.btn_ok.setToolTip("Keep the rule")
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(self.btn_ok)
        layout.addLayout(buttons)
        self._file_types = {fold(t.name) for t in repo.list_tags(kind="default")}
        self._update()

    def rule(self) -> FolderRule:
        return FolderRule(self.txt_folder.text().strip(), split_tags(self.txt_tags.text()), self.chk_subfolders.isChecked())

    def tag_existing(self) -> bool:
        return self.chk_existing.isChecked()

    def problem(self) -> str:
        rule = self.rule()
        if not rule.folder:
            return "Choose a folder."
        if not Path(rule.folder).is_absolute():
            return "The folder needs its full path (Browse... picks one)."
        if not rule.tags:
            return "Give it at least one tag."
        for name in rule.tags:
            if len(name) > MAX_TAG_NAME_LENGTH:
                return f"Tag names can be at most {MAX_TAG_NAME_LENGTH} characters."
            if fold(name) in self._file_types:
                return f'"{name}" is a file type tag: files get it by themselves.'
        return ""

    def _update(self, *_args) -> None:
        problem = self.problem()
        typed = bool(self.txt_folder.text().strip() and self.txt_tags.text().strip())
        self.lbl_problem.setText(problem if typed else "")
        self.btn_ok.setEnabled(not problem)
        if not problem:
            count = self.repo.files_in_folder_rule(self.rule())
            self.chk_existing.setText(f"Also tag the {count:,} file{'s' if count != 1 else ''} already in it")

    def _add_tag(self, name: str) -> None:
        tags = split_tags(self.txt_tags.text())
        if fold(name) not in {fold(t) for t in tags}:
            self.txt_tags.setText(", ".join((*tags, name)))

    def _browse(self) -> None:
        start = self.txt_folder.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Folder for the Rule", start)
        if chosen:
            self.txt_folder.setText(str(Path(chosen)))


def edit_rule(repo: TagRepository, rules: Sequence[FolderRule], parent: QWidget, rule: Optional[FolderRule] = None,
              folder: str = "") -> Optional[tuple[list[FolderRule], FolderRule, bool]]:
    """Ask for a new rule (or a change to rule); None if cancelled, else (all the rules now, the rule,
    whether to tag the files already in its folder). A new rule for a folder that has one already
    replaces it, as changing it would."""
    if rule is None and folder:
        rule = next((r for r in rules if r.folder_key == FolderRule(folder, ()).folder_key), None)
    dialog = FolderRuleDialog(repo, rule, folder, parent)
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        new = dialog.rule()
        kept = [r for r in rules if r != rule and r.folder_key != new.folder_key]
        return [*kept, new], new, dialog.tag_existing()
    finally:
        dialog.deleteLater()


class FolderRulesPage(QWidget):
    """Settings > Folder Rules: the rules, kept when Settings is saved (apply())."""

    def __init__(self, repo: TagRepository, rules: Sequence[FolderRule], parent: Optional[QWidget] = None):
        from ui.settings_dialog import _page_header  # (it imports this module)

        super().__init__(parent)
        self.repo = repo
        self._rules = list(rules)
        self._original = list(rules)
        self._tag_now: list[FolderRule] = []  # rules whose folder's files are tagged on Save
        self.tagged = 0  # files tagged on Save
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.addWidget(_page_header(
            "Folder Rules",
            "Files added to a folder get its rule's tags: new files, and files copied or moved in. "
            "Rules work in every profile.",
        ))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Folder", "Tags", "Subfolders"])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 300)
        self.tree.setColumnWidth(1, 200)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.itemDoubleClicked.connect(self._on_edit)
        layout.addWidget(self.tree, 1)
        buttons = QHBoxLayout()
        self.btn_add = QPushButton("+ Add Rule...")
        self.btn_add.setObjectName("primaryBtn")
        self.btn_add.setToolTip("Make a rule: files added to a folder get tags")
        self.btn_add.clicked.connect(self._on_add)
        self.btn_edit = QPushButton("Change...")
        self.btn_edit.setToolTip("Change the selected rule (or double-click it)")
        self.btn_edit.clicked.connect(self._on_edit)
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.setToolTip("Remove the selected rules (tags they gave stay on the files)")
        self.btn_remove.clicked.connect(self._on_remove)
        for button in (self.btn_add, self.btn_edit, self.btn_remove):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self._fill()

    def rules(self) -> list[FolderRule]:
        return list(self._rules)

    def changed(self) -> bool:
        return self._rules != self._original or bool(self._tag_now)

    def apply(self) -> int:
        """Save the rules, and tag the files already in the folders asked for. Returns how many files got tags."""
        from core import folder_rules

        folder_rules.save(self._rules)
        self.tagged = sum(self.repo.apply_folder_rule(rule) for rule in self._tag_now if rule in self._rules)
        self._original, self._tag_now = list(self._rules), []
        return self.tagged

    def rename_tag(self, old: str, new: str) -> None:
        self._rules = [
            FolderRule(r.folder, tuple(dict.fromkeys(new if fold(t) == fold(old) else t for t in r.tags)), r.subfolders)
            for r in self._rules
        ]
        self._fill()

    def remove_tag(self, name: str) -> None:
        rules = [FolderRule(r.folder, tuple(t for t in r.tags if fold(t) != fold(name)), r.subfolders) for r in self._rules]
        self._rules = [r for r in rules if r.tags]
        self._fill()

    def _fill(self) -> None:
        self.tree.clear()
        for rule in self._rules:
            item = QTreeWidgetItem(self.tree, [rule.folder, ", ".join(rule.tags), "Yes" if rule.subfolders else "No"])
            item.setData(0, _RULE_ROLE, rule)
            item.setToolTip(0, rule.folder if Path(rule.folder).is_dir() else f"{rule.folder}\n(not found)")
        if not self._rules:
            item = QTreeWidgetItem(self.tree, ["No rules yet. Add one, or right-click a file > Folder Rule..."])
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            item.setFirstColumnSpanned(True)
        self._update_buttons()

    def _selected(self) -> list[FolderRule]:
        return [item.data(0, _RULE_ROLE) for item in self.tree.selectedItems() if item.data(0, _RULE_ROLE) is not None]

    def _update_buttons(self) -> None:
        selected = self._selected()
        self.btn_edit.setEnabled(len(selected) == 1)
        self.btn_remove.setEnabled(bool(selected))

    def _ask(self, rule: Optional[FolderRule]) -> None:
        result = edit_rule(self.repo, self._rules, self, rule)
        if result is None:
            return
        self._rules, new, tag_existing = result
        self._tag_now = [r for r in self._tag_now if r in self._rules]
        if tag_existing:
            self._tag_now.append(new)
        self._fill()

    def _on_add(self) -> None:
        self._ask(None)

    def _on_edit(self, *_args) -> None:
        selected = self._selected()
        if len(selected) == 1:
            self._ask(selected[0])

    def _on_remove(self) -> None:
        selected = self._selected()
        if not selected:
            return
        answer = QMessageBox.question(
            self, "Remove Folder Rules",
            f"Remove {len(selected)} rule{'s' if len(selected) != 1 else ''}? Tags they already gave stay on the files.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._rules = [r for r in self._rules if r not in selected]
            self._fill()
