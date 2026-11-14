"""AI Search, More Like This and tag suggestions (SigLIP vectors from core.ai_helper), and Remove Background's
pictures (BiRefNet's or BEN2's masks).

Every picture in the library gets a vector, read in the background once (again only when the file changes):
pictures from the file, models / videos / PDFs / app files from their cached thumbnail (once they have one).
A search's words get a vector too; the closer a file's vector, the better it matches.

Where results stop: SigLIP's scores sit close together (a made-up word's best match scores about what a real
search with few matches does), so no fixed score tells "nothing like it" apart. What's shown is everything
within TEXT_BAND of the best match (LIKE_BAND for More Like This, where scores run higher), at least
MIN_RESULTS and at most MAX_RESULTS, best first. On the user's library that kept 14-1,100 files per search.

Tag suggestions come from the most similar files already tagged (the tag names alone guessed 49-55% right on
the user's tags; similar tagged files 99.6%, flattered by tags that follow folders).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

import numpy as np
from PIL import Image

from .ai_helper import CUTOUT_SIDE, PICTURE_SIDE, VECTOR_SIZE

TEXT_BAND = 0.05
LIKE_BAND = 0.12
MIN_RESULTS = 24
MAX_RESULTS = 500
SUGGEST_NEIGHBOURS = 10  # tagged files that vote on a file's tags
SUGGEST_SHARE = 0.3  # of the votes a tag needs to be suggested
SUGGEST_MAX = 5
MID_GREY = 0.5  # (transparent parts are seen on it, as the grid shows them)
_ROWS = 8192  # vectors compared at a time (float16 is kept: a float32 copy of 50,000 would be 150 MB)


# ----------------------------------------------------------------------
# Pictures for the models
# ----------------------------------------------------------------------
def _square(rgb: np.ndarray, opacity: Optional[np.ndarray], side: int) -> np.ndarray:
    """rgb float 0..1 (h, w, 3) as a side x side uint8 square (squashed: SigLIP and BEN2 both see it so)."""
    if opacity is not None:
        rgb = rgb * opacity[..., None] + MID_GREY * (1 - opacity[..., None])
    picture = Image.fromarray(np.clip(rgb * 255.0 + 0.5, 0, 255).astype(np.uint8))
    return np.asarray(picture.resize((side, side), Image.Resampling.BICUBIC), np.uint8)


def picture_pixels(path: str) -> np.ndarray:
    """A picture file, or one inside an archive ("archive|member"), as SigLIP takes it (raises if it can't be
    read)."""
    from .picture_look import read_pixels

    rgb, opacity, _precise = read_pixels(str(path), PICTURE_SIDE)
    return _square(rgb, opacity, PICTURE_SIDE)


def thumbnail_pixels(path: Path | str) -> np.ndarray:
    """A cached thumbnail (a model's rendered picture, a video's frame) as SigLIP takes it."""
    with Image.open(path) as image:
        rgba = np.asarray(image.convert("RGBA"), np.float32) / 255.0
    return _square(rgba[..., :3], rgba[..., 3], PICTURE_SIDE)


def cutout_input(picture: Image.Image) -> np.ndarray:
    """What BEN2 is given: the picture squashed to CUTOUT_SIDE (transparent parts on mid grey)."""
    rgba = np.asarray(picture.convert("RGBA").resize((CUTOUT_SIDE, CUTOUT_SIDE), Image.Resampling.BICUBIC), np.float32) / 255.0
    return _square(rgba[..., :3], rgba[..., 3], CUTOUT_SIDE)


def apply_mask(picture: Image.Image, mask: np.ndarray) -> Image.Image:
    """The picture with BEN2's mask as its transparency (at the picture's size; transparency it had stays)."""
    rgba = picture.convert("RGBA")
    alpha = Image.fromarray(np.asarray(mask, np.uint8), "L").resize(rgba.size, Image.Resampling.BILINEAR)
    old = np.asarray(rgba.getchannel("A"), np.uint16)
    combined = (old * np.asarray(alpha, np.uint16) + 127) // 255
    rgba.putalpha(Image.fromarray(combined.astype(np.uint8), "L"))
    return rgba


def cut_out(picture: Image.Image, model: str = "birefnet") -> Image.Image:
    """Remove a picture's background with a cut-out model (ai_tools.CUTOUT_MODELS; through the helper; raises
    ai_helper.AIFailed)."""
    from . import ai_helper

    return apply_mask(picture, ai_helper.cutout_mask(cutout_input(picture), model))


# ----------------------------------------------------------------------
# Stored vectors
# ----------------------------------------------------------------------
def vector_bytes(vector: np.ndarray) -> bytes:
    return np.asarray(vector, np.float16).tobytes()


def vector_from(data: bytes) -> Optional[np.ndarray]:
    vector = np.frombuffer(data, np.float16) if data else None
    return vector if vector is not None and vector.size == VECTOR_SIZE else None


class VectorIndex:
    """The library's vectors in one matrix (float16), to compare a search with all of them at once."""

    def __init__(self, vectors: Optional[Mapping[int, np.ndarray]] = None):
        self._ids = np.zeros(0, np.int64)
        self._matrix = np.zeros((0, VECTOR_SIZE), np.float16)
        self._row: dict[int, int] = {}
        if vectors:
            self.update(vectors)

    def __len__(self) -> int:
        return len(self._row)

    def __contains__(self, asset_id: int) -> bool:
        return asset_id in self._row

    def get(self, asset_id: int) -> Optional[np.ndarray]:
        row = self._row.get(asset_id)
        return None if row is None else self._matrix[row]

    def update(self, vectors: Mapping[int, np.ndarray]) -> None:
        new = []
        for asset_id, vector in vectors.items():
            row = self._row.get(asset_id)
            if row is None:
                new.append((asset_id, vector))
            else:
                self._matrix[row] = vector
        if new:
            start = len(self._ids)
            self._ids = np.concatenate([self._ids, np.array([i for i, _v in new], np.int64)])
            self._matrix = np.concatenate([self._matrix, np.stack([np.asarray(v, np.float16) for _i, v in new])])
            self._row.update({asset_id: start + n for n, (asset_id, _v) in enumerate(new)})

    def scores(self, query: np.ndarray, allowed: Optional[Iterable[int]] = None) -> tuple[np.ndarray, np.ndarray]:
        """(ids, cosine scores) of every vector (or only the allowed ids), against a unit query vector."""
        q = np.asarray(query, np.float32)
        scores = np.empty(len(self._ids), np.float32)
        for start in range(0, len(self._ids), _ROWS):
            scores[start:start + _ROWS] = self._matrix[start:start + _ROWS].astype(np.float32) @ q
        ids = self._ids
        if allowed is not None:
            keep = np.isin(ids, np.fromiter(allowed, np.int64))
            ids, scores = ids[keep], scores[keep]
        return ids, scores


def pick(ids: np.ndarray, scores: np.ndarray, band: float, minimum: int = MIN_RESULTS, maximum: int = MAX_RESULTS,
         first: Optional[int] = None) -> list[int]:
    """The ids to show, best first: within band of the best score, at least minimum, at most maximum. first:
    a file shown first whatever its score (More Like This: the file itself), left out of "the best"."""
    order = np.argsort(-scores, kind="stable")
    ids, scores = ids[order], scores[order]
    if first is not None:
        keep = ids != first
        ids, scores = ids[keep], scores[keep]
    count = 0
    if len(scores):
        count = int(np.searchsorted(-scores, -(scores[0] - band), side="right"))
        count = min(max(count, minimum), maximum, len(scores))
    shown = [int(i) for i in ids[:count]]
    return ([first] if first is not None else []) + shown


def ranking(shown: Sequence[int]) -> dict[int, float]:
    """{id: place} for core.asset_filter (smaller first)."""
    return {asset_id: float(n) for n, asset_id in enumerate(shown)}


def search(index: VectorIndex, query: np.ndarray, allowed: Optional[Iterable[int]] = None) -> dict[int, float]:
    """AI Search: {id: place} of the files that match a search's vector."""
    ids, scores = index.scores(query, allowed)
    return ranking(pick(ids, scores, TEXT_BAND))


def more_like(index: VectorIndex, asset_id: int, allowed: Optional[Iterable[int]] = None) -> Optional[dict[int, float]]:
    """More Like This: {id: place} of the file (first) and the files most like it; None if it has no vector."""
    vector = index.get(asset_id)
    if vector is None:
        return None
    ids, scores = index.scores(vector.astype(np.float32), allowed)
    return ranking(pick(ids, scores, LIKE_BAND, first=asset_id))


# ----------------------------------------------------------------------
# Tag suggestions
# ----------------------------------------------------------------------
def suggest_tags(index: VectorIndex, files: Sequence[int], tags_of: Mapping[int, Sequence[str]],
                 neighbours: int = SUGGEST_NEIGHBOURS, share: float = SUGGEST_SHARE, limit: int = SUGGEST_MAX) -> list[str]:
    """Tags for these files, from the most similar files already tagged (tags_of: {id: custom tag names}).
    A tag every one of the files already has isn't suggested."""
    tagged = [i for i, names in tags_of.items() if names and i in index and i not in set(files)]
    targets = [i for i in files if i in index]
    if not tagged or not targets:
        return []
    totals: dict[str, float] = {}
    for target in targets:
        ids, scores = index.scores(index.get(target).astype(np.float32), tagged)
        order = np.argsort(-scores)[:neighbours]
        weights = np.maximum(scores[order], 0.0)
        if weights.sum() <= 0:
            continue
        votes: dict[str, float] = {}
        for asset_id, weight in zip(ids[order], weights):
            for name in tags_of[int(asset_id)]:
                votes[name] = votes.get(name, 0.0) + float(weight)
        for name, vote in votes.items():
            totals[name] = totals.get(name, 0.0) + vote / float(weights.sum())
    have_all = set.intersection(*(set(tags_of.get(i, ())) for i in files)) if files else set()
    shares = {name: total / len(targets) for name, total in totals.items() if name not in have_all}
    return [name for name, value in sorted(shares.items(), key=lambda kv: (-kv[1], kv[0].casefold())) if value >= share][:limit]


# ----------------------------------------------------------------------
# Remove Background in the library: name_cutout.png beside each picture
# ----------------------------------------------------------------------
def cutout_target(asset) -> Path:
    """Where a picture's cut-out goes: name_cutout.png beside it (beside its archive, for one inside one),
    numbered if that's taken. Never an existing file."""
    folder = Path(asset.archive.archive).parent if asset.in_archive else Path(asset.path).parent
    stem = Path(asset.name).stem
    target, n = folder / f"{stem}_cutout.png", 2
    while target.exists():
        target, n = folder / f"{stem}_cutout ({n}).png", n + 1
    return target


def _pil_of(pixels) -> Image.Image:
    """core.batch_convert's Pixels as an 8-bit picture."""
    values = pixels.values
    if pixels.deep:
        values = np.clip(values * 255.0 + 0.5, 0, 255).astype(np.uint8)
    mode = {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}[values.shape[2]]
    return Image.fromarray(np.ascontiguousarray(values if mode != "L" else values[..., 0]), mode)


def make_cutout(asset, target: Path, model: str = "birefnet") -> Path:
    """Save a picture's cut-out as a PNG at target (raises OSError / ValueError / ai_helper.AIFailed)."""
    import io
    import os

    from .batch_convert import _read_asset

    result = cut_out(_pil_of(_read_asset(asset)), model)
    buffer = io.BytesIO()
    result.save(buffer, "PNG", compress_level=6)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    partial.write_bytes(buffer.getvalue())
    os.replace(partial, target)
    return target
