"""Duotone icons (ui.duotone), and the rule that no emoji / symbol characters stand in for icons."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from PySide6.QtCore import QRectF, QSize
from PySide6.QtGui import QColor, QIcon, QImage, QPainter
from PySide6.QtWidgets import QPushButton

from ui.duotone import ICONS, TONES, IconLabel, draw_icon, duotone_icon, set_icon, tone_colors
from ui.themes import apply_theme

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted([ROOT / "main.py", *(ROOT / "ui").rglob("*.py")])
# Arrows, technical / geometric shapes, dingbats, miscellaneous symbols and emoji
GLYPH = re.compile("[←-⯿☀-➿\U0001F000-\U0001FAFF]")
MARKER = "glyph-ok"  # a comment on lines where the character is text (rating stars), not an icon


def glyph_lines(source: str, rel: str) -> list[str]:
    return [
        f"{rel}:{n}: {line.strip()}"
        for n, line in enumerate(source.splitlines(), 1)
        if GLYPH.search(line) and MARKER not in line
    ]


def test_no_emoji_or_symbol_icons():
    problems = [p for path in SOURCES for p in glyph_lines(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT)))]
    assert not problems, (
        "Use a duotone icon (ui.duotone.set_icon) instead of these characters, "
        f"or mark the line '# {MARKER}: <why>' if it's text:\n" + "\n".join(problems)
    )


@pytest.mark.parametrize("snippet, flagged", [
    ('btn = QPushButton("⚙ Settings")', True),
    ('btn = QPushButton("\U0001F4CC")', True),
    ('text = "★" * rating  # glyph-ok: rating text', False),
    ('label = "1.5× • speed…"', False),
])
def test_checker(snippet, flagged):
    assert bool(glyph_lines(snippet, "probe.py")) is flagged


def render(name, tone="accent", disabled=False, size=48):
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    draw_icon(painter, QRectF(0, 0, size, size), name, tone, disabled)
    painter.end()
    return image


def colors_in(image):
    return {image.pixel(x, y) for x in range(0, image.width(), 2) for y in range(0, image.height(), 2) if image.pixel(x, y) >> 24}


@pytest.mark.parametrize("name", list(ICONS))
def test_every_icon_is_two_tone(qapp, name):
    apply_theme("dark", qapp)
    image = render(name)
    painted = [QColor.fromRgba(c) for c in colors_in(image)]
    assert len(painted) > 4  # a gradient body plus details, not a flat blob
    primary, _secondary = tone_colors("accent")

    def is_primary(c: QColor) -> bool:
        return c.alpha() > 230 and all(abs(a - b) < 14 for a, b in zip(c.getRgb()[:3], primary.getRgb()[:3]))

    assert any(is_primary(c) for c in painted)  # solid details in the primary color
    assert any(not is_primary(c) for c in painted)  # over a body in the other


@pytest.mark.parametrize("tone", list(TONES))
def test_every_tone_draws(qapp, tone):
    assert colors_in(render("check", tone))


def test_icons_follow_the_theme(qapp):
    try:
        apply_theme("dark", qapp)
        dark = duotone_icon("settings").pixmap(QSize(32, 32)).toImage()
        apply_theme("light", qapp)
        light = duotone_icon("settings").pixmap(QSize(32, 32)).toImage()
        assert dark != light  # same QIcon, repainted in the new theme's colors
    finally:
        apply_theme("dark", qapp)


def test_disabled_icons_are_greyed(qapp):
    icon = duotone_icon("tag")
    normal = icon.pixmap(QSize(24, 24), QIcon.Mode.Normal).toImage()
    disabled = icon.pixmap(QSize(24, 24), QIcon.Mode.Disabled).toImage()
    assert normal != disabled


def test_crisp_on_high_dpi(qapp):
    pixmap = duotone_icon("star").pixmap(QSize(16, 16), 2.0)
    assert pixmap.width() == 32 and pixmap.devicePixelRatio() == 2.0  # drawn at 2x, not scaled up


def test_set_icon_and_icon_label(qapp):
    button = QPushButton("Tag")
    set_icon(button, "tag", size=18)
    assert not button.icon().isNull() and button.iconSize() == QSize(18, 18)
    set_icon(button, None)
    assert button.icon().isNull()
    label = IconLabel(box_name="warningLabel")
    label.set_icon("warning", "warning")
    label.setText("Careful")
    assert label.text() == "Careful" and label.glyph.name == "warning"
    with pytest.raises(KeyError):
        duotone_icon("no-such-icon")
