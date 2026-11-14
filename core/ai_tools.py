"""The AI tools' downloads: Remove Background (BiRefNet, or BEN2 as a second try) and AI Search (SigLIP),
each downloaded the first time it's asked for, not part of the app; ONNX Runtime runs them.

ONNX Runtime (Microsoft, MIT) with DirectML, which runs on any DirectX 12 graphics card (NVIDIA, AMD, Intel)
and falls back to the processor, and Hugging Face's tokenizers (Apache 2.0) come from PyPI, built for the app's
Python. The models come from Hugging Face at a fixed commit: BiRefNet (Zheng Peng et al., MIT; the ONNX export
by onnx-community), BEN2 Base (Prama LLC, MIT) and SigLIP base patch16-224 (Google, Apache 2.0; the ONNX
export by Xenova). Every file is checked against the hash below, and nothing is put in place unless it all
worked. They run in a helper program (core.ai_helper).

On the user's pictures (an RTX 4070): BiRefNet was best or equal best on all 21 tried (grass, hair, a scene's
house), 0.97 s a picture (7.6 s on the processor); BEN2, 0.29 s (4.8 s), often right where BiRefNet slips, so
it's the second try. BiRefNet lite was weaker on fine detail; the matting models made solid things see-through
(and the 2K one hung the graphics driver). SigLIP found what was asked for in 81% of the top 8 (CLIP: 57-58%).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Optional, Sequence

from .archives import Cancelled
from .render_engine import Download

PYTHON = (3, 14)  # the Python the runtime's files are built for
RUNTIME_VERSION = "1.24.4"
RUNTIME = (
    Download(
        "onnxruntime_directml-1.24.4-cp314-cp314-win_amd64.whl",
        "https://files.pythonhosted.org/packages/b7/04/816932a3ade867a687e406716ca76e0774c6b921545b45818e3ebfcc54ce/onnxruntime_directml-1.24.4-cp314-cp314-win_amd64.whl",
        25571098, "51d86bb949488e572b00422f344990a4a81d982416d73b6c0e4ced2bcd423d19",
    ),
    Download(
        "tokenizers-0.23.3-cp310-abi3-win_amd64.whl",
        "https://files.pythonhosted.org/packages/d1/6c/24f074c9a0efb98e61b20aafe6b2641922d5db24e447d5d6daffd9e17555/tokenizers-0.23.3-cp310-abi3-win_amd64.whl",
        2872101, "1f0823bb00c5fdc98e487354d54dd55a03848d61a1a0bf29a68c77f24f3b26c3",
    ),
)
_RUNTIME_KEEP = ("onnxruntime/", "tokenizers/")  # (from the wheels: the packages, not their records)
_RUNTIME_LEFT_OUT = ("onnxruntime/tools/", "onnxruntime/transformers/", "onnxruntime/quantization/", "onnxruntime/datasets/",
                     "onnxruntime/backend/")

_BIREFNET = "https://huggingface.co/onnx-community/BiRefNet-ONNX/resolve/534d3c82d3bb8b2f0867db6dfbc3a525b8e42f67/"
_BEN2 = "https://huggingface.co/PramaLLC/BEN2/resolve/e48a20765fb421d19dcdb0bf3cc61e802ca5ec8f/"
_SIGLIP = "https://huggingface.co/Xenova/siglip-base-patch16-224/resolve/4649052661e53c7000355844105f8a1792088239/"


@dataclass(frozen=True)
class Tool:
    key: str
    name: str  # what the user knows it as
    model: str  # the model's name and maker
    version: str  # changes when the files do (a new folder; vectors made by another version are made again)
    files: tuple[Download, ...]
    about: str = ""  # (Remove Background's models: what each is good at, for the choice)
    normalise: bool = False  # (a cut-out model: it takes ImageNet-normalised pixels, not 0..1)

    @property
    def download_bytes(self) -> int:
        return sum(d.size for d in self.files)


TOOLS = {
    "birefnet": Tool("birefnet", "Remove Background", "BiRefNet (Zheng Peng et al.)", "birefnet-534d3c8", (
        Download("model_fp16.onnx", _BIREFNET + "onnx/model_fp16.onnx", 489666272,
                 "3654c741eb80bd926ada8fed1713b506ccf8d30eb1f6487e87eb9f234f33df09"),
    ), "The best on most pictures: fine detail like grass and hair, and a scene's main subject", True),
    "ben2": Tool("ben2", "Remove Background", "BEN2 (Prama LLC)", "ben2-e48a207", (
        Download("BEN2_Base.onnx", _BEN2 + "BEN2_Base.onnx", 222932053,
                 "22cea62108ff53b7ccc20f7a008bf30494228d84b1687f29ecbe76936a998101"),
    ), "A second try: it often gets right what BiRefNet misses, and it's faster"),
    "search": Tool("search", "AI Search", "SigLIP (Google)", "siglip-b16-4649052", (
        Download("vision_model_fp16.onnx", _SIGLIP + "onnx/vision_model_fp16.onnx", 186142673,
                 "7287a8e9cbf4f2eedd66e8dc5cf584be03108f4e4a24331879ac6ef7ac7c878f"),
        Download("text_model_fp16.onnx", _SIGLIP + "onnx/text_model_fp16.onnx", 220867426,
                 "ebffbfd71ae11b3f9df5233095815953f44b02ca241c317722edd8c49a9de0f6"),
        Download("tokenizer.json", _SIGLIP + "tokenizer.json", 2398744,
                 "4a17c975210be5ab4c36b47d8dae4eefb866dbfb1e676e394aad85dc30a3ae08"),
    )),
}
CUTOUT_MODELS = ("birefnet", "ben2")  # (Remove Background's choice; the first is the default)
RUNTIME_BYTES = sum(d.size for d in RUNTIME)
_MARKER = "installed.json"
_CHUNK = 1024 * 1024
_TIMEOUT = 60

Progress = Callable[[int, int], None]
ROOT_ENV = "ASSETMANAGER_AI_ROOT"  # (the helper is told where the tools are: the app's folder may be elsewhere)


class ToolError(Exception):
    """It couldn't be installed (the message says why, for the user)."""


def root() -> Path:
    told = os.environ.get(ROOT_ENV)
    if told:
        return Path(told)
    from . import video_tools  # (read when used, so tests can redirect it)

    return video_tools.CACHE_ROOT / "ai"


def runtime_folder() -> Path:
    """(Short: Windows won't load a DLL from a path of over 260 characters.)"""
    return root() / f"runtime-{RUNTIME_VERSION}"


def model_folder(key: str) -> Path:
    return root() / TOOLS[key].version


def supported() -> bool:
    return sys.platform == "win32" and sys.version_info[:2] == PYTHON


def runtime_installed() -> bool:
    return supported() and (runtime_folder() / _MARKER).is_file()


def is_installed(key: str) -> bool:
    """The tool's model and the runtime are both there."""
    return runtime_installed() and (model_folder(key) / _MARKER).is_file()


def needed_bytes(key: str) -> int:
    """What using the tool would download now."""
    total = 0 if runtime_installed() else RUNTIME_BYTES
    return total + (0 if (model_folder(key) / _MARKER).is_file() else TOOLS[key].download_bytes)


def add_to_path() -> None:
    """So that "import onnxruntime" and "import tokenizers" find them (the helper only)."""
    folder = str(runtime_folder())
    if folder not in sys.path:
        sys.path.insert(0, folder)


def remove(key: str) -> None:
    """Delete a tool's model ("runtime": the runtime, which the models need)."""
    shutil.rmtree(runtime_folder() if key == "runtime" else model_folder(key), ignore_errors=True)


def folder_of(key: str) -> Path:
    return runtime_folder() if key == "runtime" else model_folder(key)


def _download(item: Download, target: Path, done_before: int, total: int, progress: Optional[Progress],
              should_stop: Optional[Callable[[], bool]]) -> None:
    digest = hashlib.sha256()
    request = urllib.request.Request(item.url, headers={"User-Agent": "AssetManager"})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as reply, open(target, "wb") as out:
            got = 0
            while True:
                if should_stop is not None and should_stop():
                    raise Cancelled()
                chunk = reply.read(_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                got += len(chunk)
                if progress is not None:
                    progress(done_before + got, total)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise ToolError(f"The download failed: {getattr(exc, 'reason', exc)}") from exc
    if got != item.size or digest.hexdigest() != item.sha256:  # (nothing that isn't exactly this file gets used)
        raise ToolError(f"{item.name} didn't download correctly. Try again.")


def _unpack(wheel: Path, folder: Path) -> None:
    with zipfile.ZipFile(wheel) as z:
        for info in z.infolist():
            name = PurePosixPath(info.filename)
            if info.is_dir() or not info.filename.startswith(_RUNTIME_KEEP) or info.filename.startswith(_RUNTIME_LEFT_OUT):
                continue
            if name.is_absolute() or ".." in name.parts:
                raise ToolError(f"{wheel.name} holds a file it shouldn't: {info.filename}")
            target = folder.joinpath(*name.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as source, open(target, "wb") as out:
                shutil.copyfileobj(source, out, _CHUNK)


def install(keys: Sequence[str], progress: Optional[Progress] = None, should_stop: Optional[Callable[[], bool]] = None) -> None:
    """Download what these tools need (the runtime once). Raises ToolError, Cancelled."""
    if not supported():
        raise ToolError("The AI tools aren't available for this version of the app.")
    parts: list[tuple[str, tuple[Download, ...]]] = []
    if not runtime_installed():
        parts.append(("runtime", RUNTIME))
    for key in dict.fromkeys(keys):
        if not (model_folder(key) / _MARKER).is_file():
            parts.append((key, TOOLS[key].files))
    total = sum(d.size for _key, files in parts for d in files)
    done = 0
    working = root() / "downloading"
    try:
        for key, files in parts:
            shutil.rmtree(working, ignore_errors=True)
            downloads, contents = working / "downloads", working / "contents"
            downloads.mkdir(parents=True)
            contents.mkdir()
            for item in files:
                _download(item, downloads / item.name, done, total, progress, should_stop)
                done += item.size
            if should_stop is not None and should_stop():
                raise Cancelled()
            if key == "runtime":
                for item in files:
                    _unpack(downloads / item.name, contents)
                version = RUNTIME_VERSION
            else:
                for item in files:
                    (downloads / item.name).replace(contents / item.name)
                version = TOOLS[key].version
            (contents / _MARKER).write_text(json.dumps({"version": version}), encoding="utf-8")
            target = folder_of(key)
            shutil.rmtree(target, ignore_errors=True)
            contents.rename(target)
    except OSError as exc:
        raise ToolError(f"It couldn't be installed: {exc.strerror or exc}") from exc
    finally:
        shutil.rmtree(working, ignore_errors=True)
