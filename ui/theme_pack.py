"""Theme files (.amtheme) made with the Theme Studio (ui.theme_studio): a theme's colour tokens
and its background, as plain data.

A file is JSON: {"format", "version", "name", "base", "tokens", "background"}. Every token is a
colour (#rgb, #rrggbb, #aarrggbb, rgba(...) or transparent; the surfaces, GRADIENT_TOKENS, may also
be a qlineargradient), checked strictly, since tokens go into the app's stylesheet. Missing or
damaged tokens fall back to the base theme's, with a note saying which.

The background is one of:
- {"kind": "none"}
- {"kind": "builtin", "texture": <ui.backgrounds.TEXTURES id>}
- {"kind": "pattern", <ui.pattern_tiles.PATTERN_DEFAULTS settings>}
- {"kind": "image", "png": <base64 PNG>, <ui.pattern_tiles.IMAGE_DEFAULTS settings>}
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColor, QImage

from ui import pattern_tiles
from ui.themes import DEFAULT_THEME, THEMES, parse_color

FORMAT = "asset-manager-theme"
VERSION = 1
SUFFIX = ".amtheme"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_NAME = 60
GRADIENT_TOKENS = {"window_bg", "surface", "surface_alt", "sidebar_bg", "grid_bg", "border", "hover"}  # (the surfaces)

_HEX = r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-fA-F])"
_RGBA = r"rgba\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*(?:0|1|0?\.\d+|1\.0+)\s*\)"
_COLOR = rf"(?:{_HEX}|{_RGBA}|transparent)"
_NUMBER = r"-?\d+(?:\.\d+)?"
COLOR_RE = re.compile(rf"^{_COLOR}$")
HEX_RE = re.compile(rf"^{_HEX}$")  # (a background's own tint colour)
GRADIENT_RE = re.compile(
    rf"^qlineargradient\(\s*x1:\s*{_NUMBER},\s*y1:\s*{_NUMBER},\s*x2:\s*{_NUMBER},\s*y2:\s*{_NUMBER}"
    rf"(?:,\s*stop:\s*{_NUMBER}\s+{_COLOR})+\s*\)$"
)
GRADIENT_STOP_RE = re.compile(rf"stop:\s*({_NUMBER})\s+({_COLOR})")


class ThemePackError(Exception):
    """A theme file that can't be read (the message says why, for the person)."""


def token_keys() -> list[str]:
    """Every token a theme has, in the built-in themes' order."""
    return [key for key in THEMES[DEFAULT_THEME] if key != "label"]


def valid_value(key: str, value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 400:
        return False
    value = value.strip()
    return bool(COLOR_RE.match(value) or (key in GRADIENT_TOKENS and GRADIENT_RE.match(value)))


def gradient_stops(value: str) -> list[tuple[float, str]]:
    """A qlineargradient's stops as (position, colour); [] if it isn't one."""
    if not GRADIENT_RE.match(value.strip()):
        return []
    return [(float(at), color) for at, color in GRADIENT_STOP_RE.findall(value)]


def first_color(value: str) -> QColor:
    """A token as one colour (a gradient's first stop), for swatches and contrast."""
    stops = gradient_stops(value)
    return parse_color(stops[0][1] if stops else value.strip())


@dataclass
class ThemePack:
    name: str
    base: str = DEFAULT_THEME
    tokens: dict[str, str] = field(default_factory=dict)
    background: dict[str, Any] = field(default_factory=lambda: {"kind": "none"})

    def full_tokens(self) -> dict[str, str]:
        """Every token: this theme's, the base theme's where it has none (plus the label)."""
        base = THEMES.get(self.base, THEMES[DEFAULT_THEME])
        tokens = {key: base[key] for key in token_keys()}
        tokens.update({k: v for k, v in self.tokens.items() if k in tokens and valid_value(k, v)})
        tokens["label"] = self.name
        return tokens

    def to_json(self) -> dict[str, Any]:
        return {
            "format": FORMAT, "version": VERSION, "name": self.name, "base": self.base,
            "tokens": {key: self.full_tokens()[key] for key in token_keys()}, "background": dict(self.background),
        }


def from_theme(theme_id: str, name: str = "") -> ThemePack:
    """A new theme starting as one of the built-in ones."""
    theme_id = theme_id if theme_id in THEMES else DEFAULT_THEME
    base = THEMES[theme_id]
    return ThemePack(name or f"My {base['label']}", theme_id, {key: base[key] for key in token_keys()})


def _number(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:
        return default
    return min(high, max(low, number))


def _clean_tint(data: dict, clean: dict[str, Any]) -> dict[str, Any]:
    """Keep a background's own tint colour (a hex colour only), if it has a valid one."""
    tint = data.get("tint")
    if isinstance(tint, str) and HEX_RE.fullmatch(tint.strip()):
        clean["tint"] = QColor(tint.strip()).name(QColor.NameFormat.HexArgb)
    return clean


def background_ink(background: dict[str, Any], ink: QColor) -> QColor:
    """What a pattern / tinted image is drawn in: its own tint colour, or else the theme's ink."""
    tint = background.get("tint")
    return QColor(tint) if isinstance(tint, str) and HEX_RE.fullmatch(tint) else ink


def clean_background(data: Any, notes: Optional[list[str]] = None) -> dict[str, Any]:
    """A background as read from a file, with every setting checked (unknown or damaged: none)."""
    notes = notes if notes is not None else []
    if not isinstance(data, dict):
        return {"kind": "none"}
    kind = data.get("kind")
    if kind == "builtin":
        from ui.backgrounds import TEXTURES

        texture = data.get("texture")
        if texture in TEXTURES:
            return {"kind": "builtin", "texture": texture}
        notes.append("Its built-in texture isn't one this version has: no background.")
        return {"kind": "none"}
    if kind == "pattern":
        clean: dict[str, Any] = {"kind": "pattern"}
        defaults = pattern_tiles.PATTERN_DEFAULTS
        clean["shape"] = data.get("shape") if data.get("shape") in pattern_tiles.SHAPES else defaults["shape"]
        clean["direction"] = data.get("direction") if data.get("direction") in pattern_tiles.DIRECTIONS else defaults["direction"]
        clean["stagger"] = bool(data.get("stagger", defaults["stagger"]))
        for key, (low, high) in pattern_tiles.PATTERN_RANGES.items():
            clean[key] = _number(data.get(key, defaults[key]), low, high, defaults[key])
        clean["cell"] = int(round(clean["cell"]))
        clean["seed"] = int(round(clean["seed"]))
        return _clean_tint(data, clean)
    if kind == "image":
        try:
            png = base64.b64decode(str(data.get("png", "")), validate=True)
        except (binascii.Error, ValueError):
            png = b""
        if not png or pattern_tiles.image_from_png(png) is None:
            notes.append("Its background image is damaged: no background.")
            return {"kind": "none"}
        defaults = pattern_tiles.IMAGE_DEFAULTS
        clean = {"kind": "image", "png": base64.b64encode(png).decode("ascii")}
        clean["mode"] = data.get("mode") if data.get("mode") in pattern_tiles.IMAGE_MODES else defaults["mode"]
        clean["invert"] = bool(data.get("invert", defaults["invert"]))
        clean["seamless"] = bool(data.get("seamless", defaults["seamless"]))
        for key, (low, high) in pattern_tiles.IMAGE_RANGES.items():
            clean[key] = _number(data.get(key, defaults[key]), low, high, defaults[key])
        return _clean_tint(data, clean)
    return {"kind": "none"}


def load(path: Path) -> tuple[ThemePack, list[str]]:
    """A theme file, and notes on anything in it that was left out (raises ThemePackError)."""
    path = Path(path)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ThemePackError("It's too big to be a theme file.")
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ThemePackError(f"It couldn't be read ({error.strerror or error}).") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ThemePackError("It isn't a theme file (or it's damaged).") from error
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ThemePackError("It isn't a theme file made with the Theme Studio.")
    if not isinstance(data.get("version"), int) or data["version"] > VERSION:
        raise ThemePackError("It was made by a newer version of the Theme Studio.")
    notes: list[str] = []
    base = data.get("base") if data.get("base") in THEMES else DEFAULT_THEME
    name = data.get("name") if isinstance(data.get("name"), str) and data.get("name").strip() else path.stem
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    kept, damaged, missing = {}, [], []
    for key in token_keys():
        if key not in tokens:
            missing.append(key)
        elif valid_value(key, tokens[key]):
            kept[key] = tokens[key].strip()
        else:
            damaged.append(key)
    if damaged:
        notes.append(f"Colours that couldn't be read, so the base theme's are used: {', '.join(damaged)}.")
    if missing:
        notes.append(f"Colours it didn't have, so the base theme's are used: {', '.join(missing)}.")
    pack = ThemePack(name.strip()[:MAX_NAME], base, kept, clean_background(data.get("background"), notes))
    return pack, notes


def save(pack: ThemePack, path: Path) -> None:
    """Write the theme file (whole, or not at all)."""
    path = Path(path)
    temporary = path.with_name(path.name + ".saving")
    temporary.write_text(json.dumps(pack.to_json(), indent=2), encoding="utf-8")
    temporary.replace(path)


def png_text(image: QImage) -> str:
    """An image as base64 PNG (as a theme file keeps it)."""
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return base64.b64encode(bytes(data)).decode("ascii")


def render_background(background: dict[str, Any], ink: QColor, scale: int = 1) -> Optional[QImage]:
    """The background's tile at 1x / 2x; None for none."""
    kind = background.get("kind")
    if kind == "builtin":
        from ui.backgrounds import render_tile

        return render_tile(background.get("texture", "none"), ink, scale)
    if kind == "pattern":
        return pattern_tiles.render_pattern(background, background_ink(background, ink), scale)
    if kind == "image":
        source = pattern_tiles.image_from_png(base64.b64decode(background["png"]))
        return pattern_tiles.render_image_tile(source, background, background_ink(background, ink), scale) if source is not None else None
    return None


def tile_file(background: dict[str, Any], ink: QColor, folder: Path) -> Optional[Path]:
    """The background's tile saved as PNG (plus @2x) in folder, for a stylesheet; None for none."""
    if background.get("kind") not in ("builtin", "pattern", "image"):
        return None
    key = hashlib.sha1(json.dumps({**background, "ink": ink.name(QColor.NameFormat.HexArgb)}, sort_keys=True).encode()).hexdigest()[:16]
    path = folder / f"tile-{key}.png"
    if not path.is_file():
        one, two = render_background(background, ink, 1), render_background(background, ink, 2)
        if one is None or two is None:
            return None
        folder.mkdir(parents=True, exist_ok=True)
        if not (two.save(str(folder / f"tile-{key}@2x.png")) and one.save(str(path))):
            return None
    return path


def python_snippet(pack: ThemePack, theme_id: str) -> str:
    """The theme as an entry for ui.themes.THEMES (what goes into the app)."""
    tokens = pack.full_tokens()
    lines = [f'    "{theme_id}": {{', f'        "label": {json.dumps(pack.name)},']
    lines += [f'        "{key}": {json.dumps(tokens[key])},' for key in token_keys()]
    lines.append("    },")
    return "\n".join(lines)


def theme_id_for(name: str) -> str:
    """A THEMES key from a theme's name: "My Ocean" -> "my_ocean"."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "custom"
