"""Update checking and installation from the project's GitHub repository.

Where updates come from:
1. The latest GitHub release, if one is published (tag like "v0.2.0"). Its source
   zip is downloaded and installed.
2. Otherwise the default branch: the version is read from core/version.py on that
   branch, and the branch's source zip is installed.

Running from source, installing copies the new files over the app folder (skipping
the virtual environment and caches), after backing up every file it replaces to
.update_backup/<old version>/. Local files that aren't in the update are left as is.

Running as the packaged app, only published releases count (both builds are made
by .github/workflows/release.yml):
- Portable AssetManager.exe: the release's AssetManager.exe is downloaded and swapped
  in for the running executable, which is kept as AssetManager.exe.old until the next launch.
- Installed (folder build): the release's AssetManager-Setup.exe is downloaded, and
  restart_application() runs it silently; it closes the app, updates it in place and
  starts it again.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .version import APP_NAME, APP_VERSION, GITHUB_REPO, GITHUB_URL

# True when running as a PyInstaller-built executable
IS_FROZEN = bool(getattr(sys, "frozen", False))
EXECUTABLE_PATH = Path(sys.executable).resolve()
# Names of the files attached to each GitHub release by the release workflow
RELEASE_EXE_NAME = "AssetManager.exe"
RELEASE_SETUP_NAME = "AssetManager-Setup.exe"
# The folder build keeps its libraries in _internal\ next to the exe; the portable exe unpacks them to temp
IS_INSTALLED = IS_FROZEN and (EXECUTABLE_PATH.parent / "_internal").is_dir()
# Installer downloaded by install_update(), run by restart_application()
_pending_installer: Optional[Path] = None

APP_ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR_NAME = ".update_backup"
API_ROOT = f"https://api.github.com/repos/{GITHUB_REPO}"

# Top-level folders/files in the app directory an update never touches
_PROTECTED_NAMES = {".venv", "venv", ".git", BACKUP_DIR_NAME, "__pycache__"}
_REQUEST_TIMEOUT = 20


class UpdateError(Exception):
    """Raised with a user-facing message when checking or installing fails."""


@dataclass
class UpdateInfo:
    latest_version: str
    current_version: str
    source: str  # "release" or "branch"
    name: str
    notes: str
    page_url: str
    download_url: str
    # Published by GitHub for release assets; used to reject incomplete or damaged downloads
    download_size: int = 0
    download_sha256: str = ""

    @property
    def is_newer(self) -> bool:
        return parse_version(self.latest_version) > parse_version(self.current_version)


def parse_version(text: str) -> tuple[int, ...]:
    """Parse "v1.2.3" / "1.2" / "1.2.3-beta" into a comparable tuple of ints."""
    numbers = re.findall(r"\d+", text.split("-")[0].split("+")[0])
    parts = [int(n) for n in numbers[:4]] or [0]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def _request(url: str, accept: str = "application/vnd.github+json") -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"{APP_NAME}-Updater"})
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise UpdateError("GitHub's rate limit was reached. Please try again in a little while.") from exc
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(
            "Couldn't reach GitHub. Check your internet connection and try again."
        ) from exc


def _get_json(url: str) -> Optional[dict]:
    """GET a GitHub API URL; returns None on 404."""
    try:
        return json.loads(_request(url))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise UpdateError(f"GitHub returned an error ({exc.code}).") from exc


def check_for_update() -> UpdateInfo:
    """Look up the newest available version on GitHub."""
    release = _get_json(f"{API_ROOT}/releases/latest")
    if release and release.get("tag_name"):
        download_url = release["zipball_url"]
        download_size, download_sha256 = 0, ""
        if IS_FROZEN:
            wanted = RELEASE_SETUP_NAME if IS_INSTALLED else RELEASE_EXE_NAME
            asset = next((a for a in release.get("assets", []) if a.get("name") == wanted), None)
            if asset is None:
                raise UpdateError(
                    f"Release {release['tag_name']} doesn't have {wanted} attached yet. "
                    "If it was just published, it may still be building; try again in a few minutes."
                )
            download_url = asset["browser_download_url"]
            download_size = int(asset.get("size") or 0)
            digest = asset.get("digest") or ""
            if digest.startswith("sha256:"):
                download_sha256 = digest[len("sha256:"):].lower()
        return UpdateInfo(
            latest_version=release["tag_name"].lstrip("vV"),
            current_version=APP_VERSION,
            source="release",
            name=release.get("name") or release["tag_name"],
            notes=(release.get("body") or "").strip(),
            page_url=release.get("html_url") or f"{GITHUB_URL}/releases",
            download_url=download_url,
            download_size=download_size,
            download_sha256=download_sha256,
        )

    if IS_FROZEN:
        # A source branch can't update a packaged executable
        raise UpdateError("No releases have been published yet.")

    repo = _get_json(API_ROOT)
    if repo is None:
        raise UpdateError(f"The repository {GITHUB_REPO} couldn't be found.")
    branch = repo.get("default_branch", "main")
    try:
        version_source = _request(
            f"https://raw.githubusercontent.com/{GITHUB_REPO}/{branch}/core/version.py",
            accept="text/plain",
        ).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError(
                "No update has been published yet: the repository has no releases and "
                "doesn't contain the app's code."
            ) from exc
        raise UpdateError(f"GitHub returned an error ({exc.code}).") from exc

    match = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', version_source)
    if not match:
        raise UpdateError("Couldn't read the version number from the repository.")
    return UpdateInfo(
        latest_version=match.group(1),
        current_version=APP_VERSION,
        source="branch",
        name=f"{branch} branch",
        notes="",
        page_url=f"{GITHUB_URL}/commits/{branch}",
        download_url=f"{API_ROOT}/zipball/{branch}",
    )


def _is_protected(relative: Path) -> bool:
    return any(part in _PROTECTED_NAMES for part in relative.parts) or relative.suffix == ".pyc"


def install_update(
    info: UpdateInfo,
    progress: Callable[[str], None] = lambda _msg: None,
) -> bool:
    """Download and install an update. Returns True if Python packages were also installed."""
    try:
        from .tag_repository import get_repository

        repo = get_repository()
        repo.backup_database(f"before-update-to-{info.latest_version}")
        repo.close_thread_connection()  # runs on the updater's worker thread
    except Exception:  # pylint: disable=broad-except
        pass  # a failed backup shouldn't block the update; the upgrade step backs up again anyway
    if IS_INSTALLED:
        _download_installer(info, progress)
        return False
    if IS_FROZEN:
        _install_executable_update(info, progress)
        return False
    return _install_source_update(info, progress)


def _check_windows_program(path: Path) -> None:
    with open(path, "rb") as f:
        is_exe = f.read(2) == b"MZ"
    if not is_exe or path.stat().st_size < 1024 * 1024:
        path.unlink(missing_ok=True)
        raise UpdateError("The downloaded file isn't a valid Windows program, so nothing was changed.")


def _download_installer(info: UpdateInfo, progress: Callable[[str], None]) -> None:
    """Installed app: download (and check) the new installer; restart_application() runs it."""
    global _pending_installer
    folder = Path(tempfile.gettempdir()) / "AssetManagerUpdate"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"AssetManager-Setup-{info.latest_version}.exe"
    progress("Downloading update...")
    _download_to_file(info.download_url, target, progress, info.download_size, info.download_sha256)
    progress("Checking downloaded file...")
    _check_windows_program(target)
    _pending_installer = target
    progress("Update downloaded. Restart to install it.")


def _download_to_file(
    url: str,
    target: Path,
    progress: Callable[[str], None],
    expected_size: int = 0,
    expected_sha256: str = "",
) -> None:
    """Download url to target, deleting it again unless it arrived whole and unchanged."""
    req = urllib.request.Request(
        url, headers={"Accept": "application/octet-stream", "User-Agent": f"{APP_NAME}-Updater"}
    )
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as response, open(target, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0) or expected_size
            done = 0
            while chunk := response.read(1024 * 256):
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if total:
                    progress(f"Downloading update... {done * 100 // total}% of {total / (1024 * 1024):.0f} MB")
            out.flush()
            os.fsync(out.fileno())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        target.unlink(missing_ok=True)
        raise UpdateError("The download failed. Check your internet connection and try again.") from exc

    if (total and done != total) or (expected_size and done != expected_size):
        target.unlink(missing_ok=True)
        raise UpdateError("The download was incomplete, so nothing was changed. Please try again.")
    if expected_sha256 and digest.hexdigest() != expected_sha256:
        target.unlink(missing_ok=True)
        raise UpdateError("The downloaded file is damaged (checksum mismatch), so nothing was changed. Please try again.")


def _free_old_path(exe: Path) -> Path:
    """Where to move the running exe: .old, or .old.N if an earlier .old is still locked."""
    candidates = [exe.with_name(f"{exe.name}.old")] + [exe.with_name(f"{exe.name}.old.{n}") for n in range(1, 10)]
    for candidate in candidates:
        try:
            candidate.unlink(missing_ok=True)
            return candidate
        except OSError:
            continue  # held open by a copy of the old version that's still running
    raise UpdateError(
        f"Couldn't replace the running program: old copies of {exe.name} are still in use. "
        "Close every Asset Manager window and try again."
    )


def _install_executable_update(info: UpdateInfo, progress: Callable[[str], None]) -> None:
    """Swap the running .exe for the release's .exe (Windows allows renaming a running exe)."""
    exe = EXECUTABLE_PATH
    downloaded = exe.with_name(exe.name + ".download")

    progress("Downloading update...")
    try:
        downloaded.unlink(missing_ok=True)  # left over from an interrupted attempt
    except OSError as exc:
        raise UpdateError(f"Couldn't write next to {exe.name}: {exc}") from exc
    _download_to_file(info.download_url, downloaded, progress, info.download_size, info.download_sha256)

    progress("Checking downloaded file...")
    _check_windows_program(downloaded)

    progress("Installing update...")
    try:
        old = _free_old_path(exe)
        exe.rename(old)
    except OSError as exc:
        downloaded.unlink(missing_ok=True)
        raise UpdateError(f"Couldn't replace the running program: {exc}") from exc
    except UpdateError:
        downloaded.unlink(missing_ok=True)
        raise
    try:
        downloaded.rename(exe)
    except OSError as exc:
        old.rename(exe)
        raise UpdateError(f"Installing failed and the previous version was restored: {exc}") from exc
    progress("Update installed.")


def cleanup_after_update() -> None:
    """Remove previous executables and partial downloads left by an update (call at startup)."""
    if not IS_FROZEN:
        return
    shutil.rmtree(Path(tempfile.gettempdir()) / "AssetManagerUpdate", ignore_errors=True)  # used installers
    exe = EXECUTABLE_PATH
    leftovers = [exe.with_name(f"{exe.name}.download")] + list(exe.parent.glob(f"{exe.name}.old*"))
    for leftover in leftovers:
        try:
            leftover.unlink(missing_ok=True)
        except OSError:
            pass  # the old process may still be shutting down; try again next launch


def backup_description() -> str:
    """Where the previous version is kept after installing an update."""
    if IS_INSTALLED:
        return "Restarting runs the installer, which updates the app in place. Your settings and library are kept."
    if IS_FROZEN:
        return f"The previous version is kept as {EXECUTABLE_PATH.name}.old until the next launch."
    return f"The previous version was backed up to {BACKUP_DIR_NAME}\\v{APP_VERSION}."


def _install_source_update(info: UpdateInfo, progress: Callable[[str], None]) -> bool:
    progress("Downloading update...")
    try:
        archive_bytes = _request(info.download_url, accept="application/vnd.github+json")
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"Download failed: GitHub returned an error ({exc.code}).") from exc

    progress("Checking downloaded files...")
    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_bytes))
    except zipfile.BadZipFile as exc:
        raise UpdateError("The downloaded update is not a valid zip file.") from exc

    # GitHub source zips wrap everything in a single "<owner>-<repo>-<sha>/" folder
    files: dict[Path, zipfile.ZipInfo] = {}
    for entry in archive.infolist():
        if entry.is_dir():
            continue
        parts = Path(entry.filename).parts
        if len(parts) < 2:
            continue
        relative = Path(*parts[1:])
        if relative.is_absolute() or ".." in relative.parts:
            raise UpdateError("The update contains unsafe file paths and was not installed.")
        if not _is_protected(relative):
            files[relative] = entry

    # Refuse anything that doesn't look like this application
    required = {Path("main.py"), Path("core/version.py")}
    if not required.issubset(files):
        raise UpdateError(
            "The downloaded update doesn't contain the application's files, so nothing was changed."
        )

    old_requirements = (APP_ROOT / "requirements.txt").read_bytes() if (APP_ROOT / "requirements.txt").exists() else b""
    new_requirements = archive.read(files[Path("requirements.txt")]) if Path("requirements.txt") in files else old_requirements

    progress("Backing up current version...")
    backup_root = APP_ROOT / BACKUP_DIR_NAME / f"v{APP_VERSION}"
    if backup_root.exists():
        shutil.rmtree(backup_root)
    for relative in files:
        current = APP_ROOT / relative
        if current.is_file():
            target = backup_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(current, target)

    progress("Installing new files...")
    try:
        for relative, entry in files.items():
            target = APP_ROOT / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    except OSError as exc:
        # Put back whatever was already overwritten
        restore_backup(backup_root)
        raise UpdateError(f"Installing failed and the previous version was restored: {exc}") from exc

    requirements_changed = new_requirements.strip() != old_requirements.strip()
    if requirements_changed:
        progress("Installing new Python packages...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(APP_ROOT / "requirements.txt")],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
            raise UpdateError(
                "The update was installed, but its Python packages couldn't be installed:\n"
                + "\n".join(tail)
                + "\n\nRun: .venv\\Scripts\\python.exe -m pip install -r requirements.txt"
            )

    progress("Update installed.")
    return requirements_changed


def restore_backup(backup_root: Path) -> None:
    """Copy backed-up files back into the app folder."""
    if not backup_root.is_dir():
        return
    for file in backup_root.rglob("*"):
        if file.is_file():
            target = APP_ROOT / file.relative_to(backup_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)


def restart_application() -> None:
    """Launch a fresh copy of the app (or the downloaded installer, which starts it); the caller should then quit."""
    if _pending_installer is not None:
        # The installer closes this app if it's still running, updates it in place and starts it again
        subprocess.Popen(
            [str(_pending_installer), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/DIR={EXECUTABLE_PATH.parent}"],
            cwd=str(_pending_installer.parent),
        )
        return
    if IS_FROZEN:
        # Make the new process unpack its own files instead of reusing this one's
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
        subprocess.Popen([str(EXECUTABLE_PATH)], cwd=str(EXECUTABLE_PATH.parent), env=env)
    else:
        subprocess.Popen([sys.executable, str(APP_ROOT / "main.py")], cwd=str(APP_ROOT))
