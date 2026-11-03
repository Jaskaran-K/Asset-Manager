"""Core asset scanner module for 3D models, textures, documents, and video files.

Scans local directories for:
- 3D Models: .fbx, .obj, .gltf, .glb, .stl, .dae, .blend, .3ds, .ply
- Textures & Images: .png, .jpg, .jpeg, .tiff, .tif, .tga, .bmp, .webp, .exr, .psd, .hdr, .dds
- Documents: .pdf
- Videos: .mp4, .mov, .mkv, .avi, .webm, .m4v
- Archives: .zip (their files are listed by core.archives)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Optional, Sequence

if TYPE_CHECKING:
    from .archives import ArchiveMember


# Supported file formats organized by type
MODEL_EXTENSIONS: set[str] = {
    ".fbx",
    ".obj",
    ".gltf",
    ".glb",
    ".stl",
    ".dae",
    ".blend",
    ".3ds",
    ".ply",
}

TEXTURE_EXTENSIONS: set[str] = {
    ".png",
    ".jpg",
    ".jpeg",
    ".tiff",
    ".tif",
    ".tga",
    ".bmp",
    ".webp",
    ".exr",
    ".psd",
    ".hdr",
    ".dds",
}

DOCUMENT_EXTENSIONS: set[str] = {
    ".pdf",
}

# Played in-app with QMediaPlayer (add more containers here)
VIDEO_EXTENSIONS: set[str] = {
    ".mp4",
    ".mov",
    ".mkv",
    ".avi",
    ".webm",
    ".m4v",
}

# Browsed like folders: their files are listed, searched and shown (core.archives; RAR and 7z
# through the user's 7-Zip)
ARCHIVE_EXTENSIONS: frozenset[str] = frozenset({".zip", ".rar", ".7z"})

_VOLUME = re.compile(r"^(.*)\.part(\d+)\.rar$", re.IGNORECASE)  # name.part1.rar, name.part2.rar, ...


def volume_number(name: str) -> int:
    """A multi-part RAR's part number (name.part2.rar: 2), or 0 for any other file."""
    match = _VOLUME.match(name)
    return int(match.group(2)) if match else 0


def archive_parts(path: Path | str) -> list[Path]:
    """The files an archive is made of: a multi-part RAR's parts (from its first), else just itself."""
    path = Path(path)
    match = _VOLUME.match(path.name)
    if not match:
        return [path]
    stem = match.group(1).lower()
    try:
        with os.scandir(path.parent) as listing:
            parts = [
                Path(e.path) for e in listing
                if (m := _VOLUME.match(e.name)) and m.group(1).lower() == stem and e.is_file()
            ]
    except OSError:
        return [path]
    return sorted(parts, key=lambda p: volume_number(p.name)) or [path]


def archive_stamp(path: Path | str) -> tuple[int, float]:
    """(size, modified time) of an archive, all its parts together (raises OSError)."""
    infos = [os.stat(part) for part in archive_parts(path)]
    return sum(i.st_size for i in infos), max(i.st_mtime for i in infos)

ALL_SUPPORTED_EXTENSIONS: set[str] = (
    MODEL_EXTENSIONS | TEXTURE_EXTENSIONS | DOCUMENT_EXTENSIONS | VIDEO_EXTENSIONS | ARCHIVE_EXTENSIONS
)

DEFAULT_EXCLUDED_DIRS: set[str] = {
    ".git",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".idea",
    ".vscode",
    "build",
    "dist",
    "__macosx",  # macOS zip-extraction metadata
}


@dataclass
class AssetItem:
    """Represents a discovered asset file (3D model, texture, document, or video)."""

    path: Path
    name: str
    extension: str
    size_bytes: int
    modified_timestamp: float
    asset_group: str  # "3D Models", "Textures", "Documents", "Videos", "Archives"
    category_name: str  # e.g. "FBX", "OBJ", "PNG", "TIFF", "PDF"
    # A file inside an archive: path is then virtual (core.archives), not a file on disk
    archive: Optional["ArchiveMember"] = None

    @property
    def formatted_size(self) -> str:
        """Returns the file size formatted in human-readable units."""
        bytes_val = float(self.size_bytes)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if bytes_val < 1024.0 or unit == "TB":
                return f"{bytes_val:.1f} {unit}" if unit != "B" else f"{int(bytes_val)} B"
            bytes_val /= 1024.0
        return f"{self.size_bytes} B"

    @property
    def formatted_modified_time(self) -> str:
        """Returns the last modified time as a formatted string."""
        return datetime.fromtimestamp(self.modified_timestamp).strftime("%Y-%m-%d %H:%M")

    @property
    def is_3d_model(self) -> bool:
        """Check if asset is a 3D model."""
        return self.asset_group == "3D Models"

    @property
    def is_texture(self) -> bool:
        """Check if asset is an image or texture."""
        return self.asset_group == "Textures"

    @property
    def is_document(self) -> bool:
        """Check if asset is a document (e.g. PDF)."""
        return self.asset_group == "Documents"

    @property
    def is_video(self) -> bool:
        """Check if asset is a video file."""
        return self.asset_group == "Videos"

    @property
    def is_archive(self) -> bool:
        """A zip (browsed like a folder)."""
        return self.asset_group == "Archives"

    @property
    def in_archive(self) -> bool:
        """Inside an archive: read through core.archives, never by its path."""
        return self.archive is not None


def asset_sort_key(asset: AssetItem) -> tuple[str, str]:
    """By name, then by folder: the same order every time for same-named files."""
    return asset.name.lower(), str(asset.path).lower()


def determine_asset_group(extension: str) -> str:
    """Classify file extension into an asset group."""
    ext = extension.lower()
    if ext in MODEL_EXTENSIONS:
        return "3D Models"
    if ext in TEXTURE_EXTENSIONS:
        return "Textures"
    if ext in DOCUMENT_EXTENSIONS:
        return "Documents"
    if ext in VIDEO_EXTENSIONS:
        return "Videos"
    if ext in ARCHIVE_EXTENSIONS:
        return "Archives"
    return "Other"


def determine_category_name(extension: str) -> str:
    """Normalize file extension into a display category name."""
    ext = extension.lower()
    mapping = {
        ".fbx": "FBX",
        ".obj": "OBJ",
        ".gltf": "glTF",
        ".glb": "glTF",
        ".stl": "STL",
        ".dae": "DAE",
        ".blend": "Blend",
        ".3ds": "3DS",
        ".ply": "PLY",
        ".png": "PNG",
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".tiff": "TIFF",
        ".tif": "TIFF",
        ".tga": "TGA",
        ".bmp": "BMP",
        ".webp": "WebP",
        ".exr": "EXR",
        ".psd": "PSD",
        ".hdr": "HDR",
        ".dds": "DDS",
        ".pdf": "PDF",
        ".mp4": "MP4",
        ".mov": "MOV",
        ".mkv": "MKV",
        ".avi": "AVI",
        ".webm": "WebM",
        ".m4v": "M4V",
        ".zip": "ZIP",
        ".rar": "RAR",
        ".7z": "7Z",
    }
    return mapping.get(ext, ext.lstrip(".").upper())


class AssetScanner:
    """Scanner for discovering 3D models, textures, and documents in local directories."""

    def __init__(
        self,
        supported_extensions: Iterable[str] | None = None,
        excluded_dirs: Iterable[str] | None = None,
    ):
        if supported_extensions is None:
            self.supported_extensions = {ext.lower() for ext in ALL_SUPPORTED_EXTENSIONS}
        else:
            self.supported_extensions = {
                ext.lower() if ext.startswith(".") else f".{ext.lower()}"
                for ext in supported_extensions
            }

        if excluded_dirs is None:
            self.excluded_dirs = {d.lower() for d in DEFAULT_EXCLUDED_DIRS}
        else:
            self.excluded_dirs = {d.lower() for d in excluded_dirs}

    def is_excluded_dir(self, path: Path) -> bool:
        return path.name.lower() in self.excluded_dirs

    def scan(
        self,
        folder_path: str | Path,
        recursive: bool = True,
        directories: set[Path] | None = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> list[AssetItem]:
        """Scan a folder and return a list of AssetItem objects found.

        Args:
            folder_path: The root directory to scan.
            recursive: If True, recursively scan subdirectories.
            directories: If given, every folder walked (the root included) is added to it,
                for watching the library for changes.
            should_stop: Checked before each folder; when it returns True the scan stops and
                returns what it has (a newer scan replaced this one).

        Returns:
            A list of AssetItem objects sorted by name (then path).
        """
        root = Path(folder_path).resolve()
        if not root.is_dir():
            return []

        assets: list[AssetItem] = []
        if directories is not None:
            directories.add(root)

        # os.scandir: one directory listing gives each entry's type, size and date on Windows,
        # where rglob plus is_dir / is_file / stat asked the disk again for every file (13-17x
        # slower on a real library). Excluded folders (.git, node_modules) are never entered.
        # Like rglob, linked folders aren't followed (is_dir(follow_symlinks=False)).
        pending = [str(root)]
        while pending:
            if should_stop is not None and should_stop():
                break
            folder = pending.pop()
            try:
                with os.scandir(folder) as listing:
                    entries = list(listing)
            except OSError:
                continue  # inaccessible or gone meanwhile
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if recursive and entry.name.lower() not in self.excluded_dirs:
                            pending.append(entry.path)
                            if directories is not None:
                                directories.add(Path(entry.path))
                        continue
                    lower_name = entry.name.lower()
                    # Filter out compiler build artifacts like foo.cpp.obj
                    if lower_name.endswith((".cpp.obj", ".c.obj", ".cc.obj", ".cxx.obj")):
                        continue
                    # Skip macOS AppleDouble metadata files (e.g. "._texture.psd")
                    if lower_name.startswith("._"):
                        continue
                    ext = os.path.splitext(lower_name)[1]
                    if ext not in self.supported_extensions or not entry.is_file():
                        continue
                    info = entry.stat()
                    size, mtime = info.st_size, info.st_mtime
                    part = volume_number(entry.name) if ext == ".rar" else 0
                    if part > 1:
                        continue  # (a multi-part RAR is one card: its first part, with all their size)
                    if part == 1:
                        size, mtime = archive_stamp(entry.path)
                    assets.append(AssetItem(
                        path=Path(entry.path),
                        name=entry.name,
                        extension=ext,
                        size_bytes=size,
                        modified_timestamp=mtime,
                        asset_group=determine_asset_group(ext),
                        category_name=determine_category_name(ext),
                    ))
                except OSError:
                    # Skip inaccessible or transient files safely
                    continue

        assets.sort(key=asset_sort_key)
        return assets

    @staticmethod
    def filter_by_category(
        assets: Sequence[AssetItem], category: str
    ) -> list[AssetItem]:
        """Filter a list of assets by category name (e.g. 'FBX', 'PNG', 'PDF', etc.)."""
        if category in ("All", "All Assets", "*"):
            return list(assets)
        return [asset for asset in assets if asset.category_name.lower() == category.lower()]

    @staticmethod
    def filter_by_group(
        assets: Sequence[AssetItem], group: str
    ) -> list[AssetItem]:
        """Filter assets by asset group ('3D Models', 'Textures', 'Documents')."""
        if group in ("All", "All Assets", "*"):
            return list(assets)
        return [asset for asset in assets if asset.asset_group.lower() == group.lower()]


def scan_folder(
    folder_path: str | Path,
    recursive: bool = True,
    extensions: Iterable[str] | None = None,
) -> list[AssetItem]:
    """Convenience helper to scan a folder for assets."""
    scanner = AssetScanner(supported_extensions=extensions)
    return scanner.scan(folder_path=folder_path, recursive=recursive)


if __name__ == "__main__":
    import sys

    target_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    print(f"Scanning '{target_dir}' for 3D models, textures, and documents...\n")

    found_assets = scan_folder(target_dir)
    if not found_assets:
        print("No matching asset files found.")
    else:
        print(f"Found {len(found_assets)} asset(s):")
        for idx, item in enumerate(found_assets, start=1):
            print(
                f" {idx:2d}. [{item.asset_group:11s} | {item.category_name:5s}] "
                f"{item.name:<32} {item.formatted_size:>10}  ({item.path})"
            )
