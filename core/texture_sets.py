"""Texture sets: a material's maps (Body_Base_Color.png, Body_Normal.png, Body_Roughness.png ...) found
by their names, shown as one card in the grid (ui), previewed on a sphere (ui.material_view) and sent
to Blender or Maya as a material (core.app_helpers).

A set is the images in one folder (or one folder inside an archive) whose names differ only by a map
word: "Body_Base_Color" and "Body_Normal" are the set "Body". The last map word in a name says what
the map is ("Rock_Color_Normal" is a normal map). UDIM tiles ("Head_BaseColor.1001", ".1002") are one
set, the tiles kept apart in it. A set has two kinds of map or more (or one kind in several tiles).

Measured on the user's library: of 8,477 images, 757 are in 192 sets (95 of them inside archives),
found in 69 ms. 34 sets have two maps of one kind (a Substance export's Normal (DirectX) and
Normal_OpenGL; Ambient_occlusion and Mixed_AO): the preview and materials use the preferred one.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .scanner import AssetItem

# Kinds of map, in the order they're listed, and the words that name them (lower case)
ROLE_WORDS: dict[str, tuple[str, ...]] = {
    "color": ("basecolor", "base_color", "basecolour", "base_colour", "albedo", "diffuse", "diff", "color", "colour",
              "col", "alb", "bc"),
    "normal": ("normal", "normals", "nrm", "nor", "norm", "nml", "nrml", "normalgl", "normaldx", "normal_opengl",
               "normal_directx", "normal_gl", "normal_dx"),
    "roughness": ("roughness", "rough", "rgh"),
    "gloss": ("glossiness", "gloss"),
    "metal": ("metallic", "metalness", "metal", "mtl"),
    "ao": ("ambient_occlusion", "ambientocclusion", "mixed_ao", "occlusion", "ao"),
    "height": ("heightmap", "height", "displacement", "displace", "disp", "bump"),
    "opacity": ("opacity", "transparency", "alpha", "mask"),
    "emissive": ("emissive", "emission", "emit", "glow"),
    "specular": ("specular", "spec", "reflection", "refl"),
    "packed": ("occlusionroughnessmetallic", "orm", "arm", "rma", "mra"),
    "other": ("curvature", "cavity", "translucency", "thickness", "position", "idmap", "matid", "sss", "id"),
}
ROLES = tuple(ROLE_WORDS)
ROLE_NAMES = {
    "color": "Base Color", "normal": "Normal", "roughness": "Roughness", "gloss": "Glossiness", "metal": "Metallic",
    "ao": "Ambient Occlusion", "height": "Height", "opacity": "Opacity", "emissive": "Emissive", "specular": "Specular",
    "packed": "Packed (AO, Roughness, Metallic)", "other": "Other",
}
# Data maps: their pixels are numbers, not colours (no sRGB conversion in the viewer, "Non-Color" / "Raw")
DATA_ROLES = frozenset({"normal", "roughness", "gloss", "metal", "ao", "height", "opacity", "packed", "specular", "other"})
# A packed map's channels (red, green, blue), by the word that named it
PACKED_CHANNELS = {
    "orm": ("ao", "roughness", "metal"), "arm": ("ao", "roughness", "metal"),
    "occlusionroughnessmetallic": ("ao", "roughness", "metal"),
    "rma": ("roughness", "metal", "ao"), "mra": ("metal", "roughness", "ao"),
}

_WORD_ROLE = {word: role for role, words in ROLE_WORDS.items() for word in words}
_WORDS = sorted(_WORD_ROLE, key=len, reverse=True)
# A map word: after the start, a separator (_ - . space) or a lower-to-upper case change ("RockNormal"), and
# before the end, a separator, a digit ("Normal2", ".1001") or a capital ("NormalOpenGL")
_MAP_WORD = re.compile(  # (the case changes are checked with case mattering: "Colorado" isn't a colour map)
    r"(?i)(?:^|[_\-. ]|(?-i:(?<=[a-z0-9])(?=[A-Z])))(" + "|".join(re.escape(w) for w in _WORDS) + r")(?=$|[_\-. 0-9]|(?-i:[A-Z]))"
)
_UDIM = re.compile(r"[._ -]?(1\d{3})$")  # a UDIM tile at the end of the name: ".1001"
_OPENGL = re.compile(r"(?i)(open_?gl|(?<![a-z])gl(?![a-z]))")
_DIRECTX = re.compile(r"(?i)(direct_?x|(?<![a-z])dx(?![a-z]))")
_MIXED = re.compile(r"(?i)mixed")
_BUMP = re.compile(r"(?i)bump")
# Of two files named alike, the most precise kind is used (a Displacement.exr over a Displacement.jpg).
_PRECISION = {".exr": 0, ".hdr": 0, ".tif": 1, ".tiff": 1, ".png": 2, ".psd": 3, ".tga": 3, ".bmp": 4, ".webp": 5,
              ".jpg": 6, ".jpeg": 6}
# Words after the map word that say how it's stored, not which material it is: "Normal_Raw", "BaseColor_sRGB"
_COLOR_SPACE = re.compile(r"(?i)(?:^|[_\-. ])(raw|srgb|linear|lin|acescg|non_?color|opengl|open_gl|directx|direct_x|gl|dx)(?=$|[_\-. ])")
_SEPARATORS = "_-. "


@dataclass(frozen=True)
class TextureMap:
    role: str  # one of ROLES
    asset: AssetItem
    word: str  # the map word as written ("Base_Color")
    tile: Optional[int] = None  # its UDIM tile, if it's one

    @property
    def directx(self) -> bool:
        """A normal map whose name says DirectX (its green channel is the other way up)."""
        return self.role == "normal" and bool(_DIRECTX.search(self.asset.name))

    @property
    def channels(self) -> tuple[str, str, str]:
        """A packed map's channels (red, green, blue)."""
        return PACKED_CHANNELS.get(self.word.lower().replace("_", ""), ("ao", "roughness", "metal"))


@dataclass(eq=False)
class TextureSet:
    name: str  # "Body"
    folder: str  # where it is (the archive's folder for one inside an archive)
    maps: list[TextureMap] = field(default_factory=list)

    @property
    def assets(self) -> list[AssetItem]:
        """Its files, by kind of map, then tile, then name."""
        order = {role: n for n, role in enumerate(ROLES)}
        return [m.asset for m in sorted(self.maps, key=lambda m: (order[m.role], m.tile or 0, m.asset.name.lower()))]

    @property
    def roles(self) -> list[str]:
        present = {m.role for m in self.maps}
        return [role for role in ROLES if role in present]

    @property
    def tiles(self) -> list[int]:
        return sorted({m.tile for m in self.maps if m.tile is not None})

    def choice(self, role: str) -> list[TextureMap]:
        """The maps used for one kind of map (all its tiles): of two of a kind, the preferred one (an OpenGL
        normal map over a DirectX one or one that doesn't say; a Mixed AO over a plain one; a Displacement or
        Height map over a Bump map, which is fine grain meant for shading only), then the most precise file
        type (EXR over PNG over JPG)."""
        maps = [m for m in self.maps if m.role == role]
        if not maps:
            return []
        variants: dict[str, list[TextureMap]] = {}
        for m in maps:
            variants.setdefault(_variant_key(m), []).append(m)
        if len(variants) == 1:
            return sorted(maps, key=lambda m: m.tile or 0)

        def preference(key: str) -> tuple:
            sample = variants[key][0]
            word = sample.asset.name
            if role == "normal":
                named = 0 if _OPENGL.search(word) else 2 if _DIRECTX.search(word) else 1
            elif role == "ao":
                named = 0 if _MIXED.search(word) else 1
            elif role == "height":
                named = 1 if _BUMP.search(sample.word) else 0
            else:
                named = 0
            return (named, _PRECISION.get(sample.asset.extension.lower(), 5), key)

        best = min(variants, key=preference)
        return sorted(variants[best], key=lambda m: m.tile or 0)

    def flip_green(self) -> bool:
        """Whether the normal map used is DirectX: named so, or a plain "Normal" beside an OpenGL one (a
        Substance export)."""
        chosen = self.choice("normal")
        if not chosen:
            return False
        if chosen[0].directx:
            return True
        others = [m for m in self.maps if m.role == "normal" and m not in chosen]
        return not _OPENGL.search(chosen[0].asset.name) and any(_OPENGL.search(m.asset.name) for m in others)

    @property
    def cover(self) -> AssetItem:
        """The file its card shows: the colour map (its first tile), else the normal map, else the first."""
        for role in ("color", "normal"):
            chosen = self.choice(role)
            if chosen:
                return chosen[0].asset
        return self.assets[0]

    @property
    def in_archive(self) -> bool:
        return self.maps[0].asset.in_archive

    def describe(self) -> str:
        """For the card's tooltip: its maps, one kind per line."""
        lines = []
        for role in self.roles:
            files = [m for m in self.maps if m.role == role]
            names = ", ".join(sorted({m.asset.name for m in files}))
            lines.append(f"{ROLE_NAMES[role]}: {names}" if len(files) < 4 else f"{ROLE_NAMES[role]}: {len(files)} files")
        if self.tiles:
            lines.append(f"UDIM tiles: {self.tiles[0]}-{self.tiles[-1]} ({len(self.tiles)})")
        return "\n".join(lines)


def _variant_key(m: TextureMap) -> str:
    """What sets two maps of one kind apart ("normal_opengl" vs "normal", "disp.exr" vs "disp.jpg"): the name
    without its tile."""
    stem, extension = os.path.splitext(m.asset.name)
    return _UDIM.sub("", stem).lower() + extension.lower()


def read_name(name: str) -> Optional[tuple[str, str, str, Optional[int]]]:
    """(kind of map, the map word as written, the set's name, its UDIM tile) for a file name, or None if no
    map word is in it. "Body_Base_Color.png" -> ("color", "Base_Color", "Body", None)."""
    stem = os.path.splitext(name)[0]
    tile = None
    udim = _UDIM.search(stem)
    if udim and udim.start() > 0:
        tile, stem = int(udim.group(1)), stem[:udim.start()]
    matches = list(_MAP_WORD.finditer(stem))
    if not matches:
        return None
    word = matches[-1]
    role = _WORD_ROLE[word.group(1).lower()]
    before, after = stem[:word.start(1)].rstrip(_SEPARATORS), stem[word.end(1):].strip(_SEPARATORS)
    after = _COLOR_SPACE.sub("", after).strip(_SEPARATORS)  # ("Normal_Raw", "BaseColor_sRGB": the same set)
    set_name = " ".join(part for part in (before, after) if part) or stem
    if role == "normal":  # ("Normal_OpenGL": the variant words aren't part of the set's name)
        set_name = _OPENGL.sub("", _DIRECTX.sub("", set_name)).strip(_SEPARATORS) or set_name
    if role == "ao" and word.group(1).lower() == "ao":  # ("Mixed_AO")
        set_name = re.sub(r"(?i)[_\-. ]?mixed$", "", set_name).strip(_SEPARATORS) or set_name
    return role, word.group(1), set_name, tile


def _folder_of(asset: AssetItem) -> str:
    text = str(asset.path)
    archive, sep, inner = text.partition("|")
    if sep:
        return os.path.normcase(archive) + "|" + os.path.dirname(inner.replace("/", "\\")).lower()
    return os.path.normcase(os.path.dirname(text))


def find_sets(assets: Iterable[AssetItem]) -> list[TextureSet]:
    """The texture sets among assets (only images are looked at), in the order their first file comes."""
    groups: dict[tuple[str, str], TextureSet] = {}
    for asset in assets:
        if not asset.is_texture:
            continue
        found = read_name(asset.name)
        if found is None:
            continue
        role, word, set_name, tile = found
        key = (_folder_of(asset), set_name.casefold())
        texture_set = groups.get(key)
        if texture_set is None:
            texture_set = groups[key] = TextureSet(set_name, key[0])
        texture_set.maps.append(TextureMap(role, asset, word, tile))
    return [s for s in groups.values() if len({m.role for m in s.maps}) >= 2 or len(s.tiles) >= 2]


SET_MARK = "|set"  # a set card's path: its cover's, with this after it (a key of its own in the grid and caches)


def set_card(texture_set: TextureSet) -> AssetItem:
    """The grid's card for a set: like its cover file, named after the set, with the set on it."""
    from dataclasses import replace
    from pathlib import Path

    cover = texture_set.cover
    return replace(
        cover, path=Path(str(cover.path) + SET_MARK), name=texture_set.name,
        size_bytes=sum(a.size_bytes for a in texture_set.assets), texture_set=texture_set,
    )


def group_into_sets(assets: Sequence[AssetItem], sets: Sequence[TextureSet]) -> list[AssetItem]:
    """assets with each set's files replaced by its card, where its first file shown is (a set shows when
    any of its files does: the search found one of its maps)."""
    owner: dict[str, TextureSet] = {str(a.path): s for s in sets for a in s.assets}
    shown: set[int] = set()
    result = []
    for asset in assets:
        texture_set = owner.get(str(asset.path))
        if texture_set is None:
            result.append(asset)
        elif id(texture_set) not in shown:
            shown.add(id(texture_set))
            result.append(set_card(texture_set))
    return result


def files_of(assets: Iterable[AssetItem]) -> list[AssetItem]:
    """The files behind cards: a set card's maps, other cards themselves (no file twice)."""
    seen: set[str] = set()
    result = []
    for asset in assets:
        texture_set = getattr(asset, "texture_set", None)
        for file in (texture_set.assets if texture_set is not None else [asset]):
            if str(file.path) not in seen:
                seen.add(str(file.path))
                result.append(file)
    return result
