"""The Tools menu's library maintenance: find duplicates, export / import tags, and back up or
restore the tag database; plus the automatic daily backup, and the offer of a backup when the
database turns out to be damaged. The main window shows the results (its status line, a rescan)
by listening to the signals."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QMenu, QMessageBox, QWidget

from core import crash_log
from core.scanner import AssetItem
from core.tag_repository import TagError, TagRepository
from ui.duplicates_dialog import DuplicatesDialog
from ui.tag_widgets import run_in_background, wait_for_database_jobs


class LibraryTools(QObject):
    status = Signal(str)  # a message for the status line
    tags_imported = Signal()  # tags, favorites, ratings and collections may all have changed
    restored = Signal()  # the database was replaced by a backup: everything should be read again
    duplicates_removed = Signal()  # files went to the Recycle Bin

    def __init__(self, repo: TagRepository, assets: Callable[[], list[AssetItem]], window: QWidget):
        super().__init__(window)
        self.repo = repo
        self._assets = assets  # the files shown (duplicates are looked for among them)
        self._window = window  # (parent of the dialogs)
        self._offered_restore = False  # (a damaged database is mentioned once per session)

    def add_to(self, menu: QMenu) -> None:
        entries = (
            ("Find Duplicates...", "Find identical files in the files shown, and recycle extra copies", self.find_duplicates),
            None,
            ("Export Tags...", "Save your custom tags and the files they're on to a .json file", self.export_tags),
            ("Import Tags...", "Add tags from an exported .json file (nothing is removed)", self.import_tags),
            None,
            ("Back Up Library Now", "Save a copy of the tag database (tags, favorites, ratings)", self.back_up_now),
            ("Restore Library Backup...", "Go back to an earlier copy of the tag database", self.restore_backup),
            ("Open Backups Folder", "Backups are also made automatically once a day, and before upgrades and updates", self.open_backups),
        )
        for entry in entries:
            if entry is None:
                menu.addSeparator()
                continue
            text, tip, handler = entry
            action = menu.addAction(text)
            action.setToolTip(tip)
            action.triggered.connect(handler)

    def find_duplicates(self) -> None:
        assets = self._assets()
        if not assets:
            QMessageBox.information(self._window, "Find Duplicates", "There are no files to compare. Scan a folder first.")
            return
        dialog = DuplicatesDialog(self.repo, assets, self._window)
        dialog.exec()
        dialog.deleteLater()
        if dialog.removed:
            self.status.emit(f"Moved {dialog.removed} duplicate file{'s' if dialog.removed != 1 else ''} to the Recycle Bin.")
            self.duplicates_removed.emit()

    def export_tags(self) -> None:
        default = Path.home() / "Documents" / "asset-manager-tags.json"
        target, _ = QFileDialog.getSaveFileName(self._window, "Export Tags", str(default), "Tag export (*.json)")
        if not target:
            return
        try:
            files = self.repo.export_tags(target)
        except OSError as exc:
            QMessageBox.warning(self._window, "Export Failed", f"The file couldn't be saved:\n{exc}")
            return
        self.status.emit(f"Exported tags for {files} file{'s' if files != 1 else ''} to {target}")

    def import_tags(self) -> None:
        source, _ = QFileDialog.getOpenFileName(
            self._window, "Import Tags", str(Path.home() / "Documents"), "Tag export (*.json)"
        )
        if not source:
            return
        try:
            result = self.repo.import_tags(source)
        except TagError as exc:
            QMessageBox.warning(self._window, "Import Failed", str(exc))
            return
        self.tags_imported.emit()
        message = (
            f"Tagged {result['files']} file{'s' if result['files'] != 1 else ''}; "
            f"created {result['tags_created']} new tag{'s' if result['tags_created'] != 1 else ''}."
        )
        if result["skipped_tags"]:
            message += f"\n\n{result['skipped_tags']} tag(s) were skipped: a file type tag now has that name."
        QMessageBox.information(self._window, "Tags Imported", message)

    def back_up_now(self) -> None:
        try:
            backup = self.repo.backup_database("manual")
        except Exception as exc:  # pylint: disable=broad-except
            QMessageBox.warning(self._window, "Backup Failed", str(exc))
            return
        self.status.emit(f"Library backed up to {backup}")

    def restore_backup(self) -> None:
        folder = self.repo.backup_dir()
        source, _ = QFileDialog.getOpenFileName(self._window, "Restore Library Backup", str(folder), "Library backup (*.db)")
        if not source:
            return
        answer = QMessageBox.question(
            self._window, "Restore Backup",
            f"Replace your current tags, favorites and ratings with the backup\n{Path(source).name}?\n\n"
            "A backup of the current state is made first, so this can be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._restore(source)

    def _restore(self, source: Path | str) -> None:
        wait_for_database_jobs()  # (none may have the database open while it's replaced; a rescan follows)
        try:
            self.repo.restore_database(source)
        except TagError as exc:
            QMessageBox.warning(self._window, "Restore Failed", str(exc))
            return
        self.restored.emit()
        self.status.emit("Backup restored. Rescanning the library...")

    # ------------------ the automatic backup, and a damaged database ------------------
    def back_up_daily(self) -> None:
        """Back up the database if the last automatic backup is a day old (in the background;
        only a healthy database is backed up: core.tag_repository.backup_if_due)."""
        run_in_background(self.repo, lambda repo: repo.backup_if_due(), self._on_daily_backup, self._on_daily_backup_failed, slow=True)

    @Slot(object)
    def _on_daily_backup(self, _backup: Any) -> None:
        pass  # (quietly: it's in the backups folder)

    @Slot(str)
    def _on_daily_backup_failed(self, message: str) -> None:
        crash_log.log_error(f"The daily library backup wasn't made: {message}")

    def offer_restore(self, error: str) -> None:
        """The database is damaged (error says how): offer its newest good backup, once per session."""
        if self._offered_restore:
            return
        self._offered_restore = True
        crash_log.log_error(f"Library database damaged: {error}")
        backup = self.repo.newest_good_backup()
        problem = (f"The tag database is damaged ({error}), so tags, favorites and ratings are unavailable. "
                   "Your files still show.")
        if backup is None:
            QMessageBox.warning(self._window, "Library Database Damaged", f"{problem}\n\nThere's no backup to restore it from.")
            return
        when = datetime.fromtimestamp(backup.stat().st_mtime).strftime("%d %b %Y at %H:%M")
        answer = QMessageBox.question(
            self._window, "Library Database Damaged",
            f"{problem}\n\nRestore the backup from {when}? Changes made since then are lost. "
            "A copy of the damaged database is kept in the backups folder.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._restore(backup)

    def open_backups(self) -> None:
        folder = self.repo.backup_dir()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
