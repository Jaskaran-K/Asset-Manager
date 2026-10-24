"""Core package for Asset Manager."""
from .scanner import (
    AssetItem,
    AssetScanner,
    scan_folder,
    MODEL_EXTENSIONS,
    TEXTURE_EXTENSIONS,
    DOCUMENT_EXTENSIONS,
    ALL_SUPPORTED_EXTENSIONS,
)
from .settings import (
    get_scan_folders,
    set_scan_folders,
    is_scan_folder,
    get_file_extensions,
    set_file_extensions,
    get_categories,
    set_categories,
)
from .version import APP_NAME, APP_VERSION
from .thumbnail_manager import ThumbnailManager

__all__ = [
    "AssetItem",
    "AssetScanner",
    "scan_folder",
    "MODEL_EXTENSIONS",
    "TEXTURE_EXTENSIONS",
    "DOCUMENT_EXTENSIONS",
    "ALL_SUPPORTED_EXTENSIONS",
    "get_scan_folders",
    "set_scan_folders",
    "is_scan_folder",
    "get_file_extensions",
    "set_file_extensions",
    "get_categories",
    "set_categories",
    "APP_NAME",
    "APP_VERSION",
    "ThumbnailManager",
]
