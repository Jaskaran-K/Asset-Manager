"""Core asset scanner module for 3D models, textures, documents, and video files.

Scans local directories for:
- 3D Models: .fbx, .obj, .gltf, .glb, .stl, .dae, .blend, .3ds, .ply
- Textures & Images: .png, .jpg, .jpeg, .tiff, .tif, .tga, .bmp, .webp, .exr, .psd, .hdr, .dds
- Documents: .pdf
- Videos: .mp4, .mov, .mkv, .avi, .webm, .m4v
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence


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

ALL_SUPPORTED_EXTENSIONS: set[str] = (
    MODEL_EXTENSIONS | TEXTURE_EXTENSIONS | DOCUMENT_EXTENSIONS | VIDEO_EXTENSIONS
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
    asset_group: str  # "3D Models", "Textures", "Documents", "Videos"
    category_name: str  # e.g. "FBX", "OBJ", "PNG", "TIFF", "PDF"

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

    def scan(
        self, folder_path: str | Path, recursive: bool = True
    ) -> list[AssetItem]:
        """Scan a folder and return a list of AssetItem objects found.

        Args:
            folder_path: The root directory to scan.
            recursive: If True, recursively scan subdirectories.

        Returns:
            A list of AssetItem objects sorted by name.
        """
        root = Path(folder_path).resolve()
        if not root.exists() or not root.is_dir():
            return []

        assets: list[AssetItem] = []

        if recursive:
            iterator = root.rglob("*")
        else:
            iterator = root.glob("*")

        for item_path in iterator:
            try:
                # Skip excluded directories (e.g. .git, .venv, node_modules)
                if any(part.lower() in self.excluded_dirs for part in item_path.parts):
                    continue

                if not item_path.is_file():
                    continue

                # Filter out compiler build artifacts like foo.cpp.obj
                lower_name = item_path.name.lower()
                if lower_name.endswith((".cpp.obj", ".c.obj", ".cc.obj", ".cxx.obj")):
                    continue

                # Skip macOS AppleDouble metadata files (e.g. "._texture.psd")
                if lower_name.startswith("._"):
                    continue

                ext = item_path.suffix.lower()
                if ext in self.supported_extensions:
                    stat = item_path.stat()
                    group = determine_asset_group(ext)
                    cat_name = determine_category_name(ext)

                    asset = AssetItem(
                        path=item_path,
                        name=item_path.name,
                        extension=ext,
                        size_bytes=stat.st_size,
                        modified_timestamp=stat.st_mtime,
                        asset_group=group,
                        category_name=cat_name,
                    )
                    assets.append(asset)
            except (PermissionError, FileNotFoundError, OSError):
                # Skip inaccessible or transient files safely
                continue

        # Sort alphabetically by file name
        assets.sort(key=lambda a: a.name.lower())
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
