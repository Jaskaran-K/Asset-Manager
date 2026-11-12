"""Batch 4: search by colour, Find Similar (core.picture_look), and folder rules (core.folder_rules)."""

from __future__ import annotations

import io
import os
import sqlite3
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from core import folder_rules, picture_look
from core.asset_filter import filter_assets
from core.folder_rules import FolderRule
from core.picture_look import Look, closeness, color_name, find_similar, group_lookalikes, look_of_pixels, parse_color, read_look
from core.scanner import AssetItem, determine_asset_group
from core.search_query import parse_query
from core.tag_repository import MIGRATIONS, TagRepository, default_tag_name


def scene(seed: int, size: int = 512) -> Image.Image:
    """A made-up picture with shapes (smooth colour blobs), different for every seed."""
    rng = np.random.default_rng(seed)
    small = Image.fromarray((rng.random((8, 8, 3)) * 255).astype(np.uint8), "RGB")
    return small.resize((size, size), Image.Resampling.BICUBIC)


def save(image: Image.Image, path: Path, **options) -> Path:
    image.save(path, **options)
    return path


def asset(path: Path, size: int = 1000, mtime: float = 1_700_000_000.0) -> AssetItem:
    ext = path.suffix.lower()
    return AssetItem(path, path.name, ext, size, mtime, determine_asset_group(ext), default_tag_name(ext))


def pump(qapp, condition, seconds=15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


# ----------------------------------------------------------------------
# Colours
# ----------------------------------------------------------------------
@pytest.mark.parametrize("rgb, name", [
    ((214, 40, 30), "red"), ((240, 140, 30), "orange"), ((240, 215, 40), "yellow"), ((60, 160, 70), "green"),
    ((40, 185, 195), "cyan"), ((40, 90, 210), "blue"), ((135, 70, 200), "purple"), ((230, 100, 175), "pink"),
    ((110, 70, 35), "brown"), ((224, 172, 150), "skin"), ((10, 10, 12), "black"), ((250, 250, 250), "white"),
    ((128, 128, 128), "grey"),
])
def test_colour_names(rgb, name):
    assert color_name(rgb) == name


def test_main_colours_and_colour_search():
    pixels = np.zeros((100, 100, 3), dtype=np.float32)
    pixels[:, :60] = (0.84, 0.16, 0.12)  # red, 60%
    pixels[:, 60:] = (0.16, 0.35, 0.82)  # blue, 40%
    look = look_of_pixels(pixels)
    assert [round(share, 1) for _rgb, share in look.colors] == [0.6, 0.4]
    assert parse_color("red").matches(look) and parse_color("blue").matches(look)
    assert not parse_color("green").matches(look)
    assert parse_color("#d62a1f").matches(look)  # an exact colour: close enough to the red
    assert not parse_color("#20c020").matches(look)


def test_transparent_parts_have_no_colour():
    pixels = np.zeros((100, 100, 3), dtype=np.float32)
    pixels[...] = (0.1, 0.7, 0.2)
    opacity = np.zeros((100, 100), dtype=np.float32)
    opacity[40:60, 40:60] = 1.0  # only a small green square is there
    look = look_of_pixels(pixels, opacity)
    assert len(look.colors) == 1 and look.names == {"green": 1.0}


def test_normal_maps_have_no_colour_names():
    pixels = np.zeros((64, 64, 3), dtype=np.float32)
    pixels[...] = (128 / 255, 128 / 255, 1.0)
    pixels[20:30] = (150 / 255, 110 / 255, 230 / 255)
    look = look_of_pixels(pixels)
    assert look.normal_map
    assert not parse_color("blue").matches(look) and not parse_color("purple").matches(look)


def test_parse_colour_words():
    assert parse_color("Gray").name == "grey" and parse_color("teal").name == "cyan"
    assert parse_color("#C42").text == "#cc4422" and parse_color("cc4422").text == "#cc4422"
    for bad in ("nope", "#12345", "#gg0000"):
        with pytest.raises(ValueError):
            parse_color(bad)


def test_looks_survive_the_database_text():
    look = look_of_pixels(np.asarray(scene(1), dtype=np.float32) / 255)
    again = Look.from_stored(look.colors_text(), look.hash, look.grey)
    assert again.hash == look.hash and again.grey == look.grey
    assert [rgb for rgb, _ in again.colors] == [rgb for rgb, _ in look.colors]
    assert again.names.keys() == look.names.keys()


# ----------------------------------------------------------------------
# Look-alikes
# ----------------------------------------------------------------------
def test_resized_and_resaved_copies_look_alike(tmp_path):
    original = save(scene(3), tmp_path / "rock.png")
    base = read_look(str(original))
    assert base.comparable
    half = read_look(str(save(scene(3).resize((256, 256), Image.Resampling.LANCZOS), tmp_path / "rock_half.png")))
    jpeg = read_look(str(save(scene(3), tmp_path / "rock.jpg", quality=60)))
    other = read_look(str(save(scene(4), tmp_path / "moss.png")))
    assert closeness(base, half) is not None and closeness(base, jpeg) is not None
    assert closeness(base, other) is None
    found = find_similar(base, {1: half, 2: jpeg, 3: other, 4: base}, exclude=4)
    assert set(found) == {1, 2}


def test_flat_pictures_cannot_be_compared(tmp_path):
    flat = read_look(str(save(Image.new("RGB", (64, 64), (128, 128, 255)), tmp_path / "flat_normal.png")))
    assert flat.hash is None and not flat.comparable
    assert find_similar(flat, {1: flat}) == {}


def test_16_bit_greys_keep_their_shapes(tmp_path):
    """A displacement alpha with a tiny range: flat in 8 bits, but its shapes are there in 16."""
    yy, xx = np.mgrid[0:256, 0:256]
    heights = (32768 + 300 * np.sin(xx / 20.0) * np.cos(yy / 30.0)).astype(np.uint16)
    path = tmp_path / "alpha.png"
    Image.fromarray(heights).save(path)  # (a 16-bit grey PNG)
    look = read_look(str(path))
    assert look.comparable  # (in 8 bits it'd be one flat grey)


def test_the_typical_hash_mistake_is_caught():
    """Two different mostly-black alphas, each with a thin white shape in the middle: alike by hash only."""
    bar = np.zeros((256, 256, 3), dtype=np.float32)
    bar[120:136, 40:216] = 1.0
    zigzag = np.zeros((256, 256, 3), dtype=np.float32)
    for x in range(40, 216):
        y = 128 + int(24 * (1 if (x // 22) % 2 else -1) * ((x % 22) / 22))
        zigzag[y - 8:y + 8, x] = 1.0
    a, b = look_of_pixels(bar), look_of_pixels(zigzag)
    assert closeness(a, b) is None


def test_lookalike_groups_are_not_chained():
    """Frames of a slow turn: each close to the next, the last far from the first. Grouped round each
    group's first picture, not chained into one."""
    looks = {}
    base = np.asarray(scene(9, 256), dtype=np.float32) / 255
    for n in range(12):
        looks[n] = look_of_pixels(np.roll(base, n * 12, axis=1))
    groups = group_lookalikes(looks)
    for group in groups:
        first = looks[group[0]]
        assert all(closeness(first, looks[i]) is not None for i in group[1:])
    assert len(groups) >= 2


def test_unreadable_pictures(tmp_path):
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not a picture")
    assert read_look(str(broken)) is picture_look.UNREADABLE
    assert read_look(str(tmp_path / "missing.png")) is picture_look.UNREADABLE


def test_a_picture_inside_an_archive(tmp_path):
    from core import archives
    from zip_maker import DEFLATED, Entry, make_zip

    buffer = io.BytesIO()
    scene(5, 128).save(buffer, "PNG")
    archive = make_zip(tmp_path / "pack.zip", [Entry("maps/rock.png", buffer.getvalue(), DEFLATED)])
    archives.forget_listings()
    try:
        inside = archives.entry_assets(archive, [".png"])[0]
        look = read_look(str(inside.path))
        loose = read_look(str(save(scene(5, 128), tmp_path / "rock.png")))
        assert look.comparable and closeness(look, loose) is not None
    finally:
        archives.forget_listings()


# ----------------------------------------------------------------------
# Searching
# ----------------------------------------------------------------------
def test_colour_and_similar_tokens():
    query = parse_query("rock color:red -colour:grey similar:12")
    assert [(f.field, f.value.name, f.negate) for f in query.filters] == [("color", "red", False), ("color", "grey", True)]
    assert query.similar_to == 12 and query.words == ["rock"] and query.uses_looks
    assert parse_query("color:nope").errors == ["color:nope"]
    assert parse_query("similar:abc").errors == ["similar:abc"]


def test_colour_filter_and_similar_order(tmp_path):
    red = np.zeros((32, 32, 3), dtype=np.float32)
    red[...] = (0.84, 0.16, 0.12)
    red[:4] = 0  # (not flat)
    files = [asset(tmp_path / f"{n}.png") for n in ("red", "blue", "unread")]
    ids = {str(a.path): i for i, a in enumerate(files, 1)}
    blue = red[..., ::-1].copy()
    looks = {1: look_of_pixels(red), 2: look_of_pixels(blue)}
    shown = filter_assets(files, query=parse_query("color:red"), ids=ids, looks=looks)
    assert [a.name for a in shown] == ["red.png"]
    shown = filter_assets(files, query=parse_query("-color:red"), ids=ids, looks=looks)
    assert [a.name for a in shown] == ["blue.png"]  # (not looked at yet: unknown either way)
    shown = filter_assets(files, ids=ids, similar={2: 0.0, 1: 3.5})
    assert [a.name for a in shown] == ["blue.png", "red.png"]  # closest first


# ----------------------------------------------------------------------
# The database
# ----------------------------------------------------------------------
def test_looks_are_stored_until_the_file_changes(tmp_path):
    repo = TagRepository(tmp_path / "lib.db")
    pic = asset(save(scene(1), tmp_path / "a.png"))
    doc = asset(tmp_path / "notes.pdf")
    ids = repo.sync_assets([pic, doc])
    todo = repo.assets_needing_look(list(ids.values()))
    assert [(row[0], row[1]) for row in todo] == [(ids[str(pic.path)], str(pic.path))]  # (only pictures)
    look = read_look(str(pic.path))
    repo.store_looks({todo[0][0]: (look, todo[0][2])})
    assert repo.assets_needing_look(list(ids.values())) == []
    stored = repo.looks_by_asset(list(ids.values()))[ids[str(pic.path)]]
    assert stored.hash == look.hash and stored.colors_text() == look.colors_text()
    edited = asset(pic.path, size=2000)
    repo.sync_assets([edited, doc])
    assert len(repo.assets_needing_look(list(ids.values()))) == 1
    assert repo.looks_by_asset(list(ids.values())) == {}


def test_upgrade_keeps_files_from_being_tagged_by_rules(tmp_path):
    """Files known before folder rules existed never get a rule's tags (only ones that arrive later)."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    for version, step in enumerate(MIGRATIONS[:7]):
        conn.executescript(f"BEGIN; {step} PRAGMA user_version = {version + 1}; COMMIT;")
    key = os.path.normcase(str(tmp_path / "wip" / "old.png"))
    conn.execute("INSERT INTO assets (path_key, path, name, name_fold, extension) VALUES (?, ?, 'old.png', 'old.png', '.png')",
                 (key, str(tmp_path / "wip" / "old.png")))
    conn.commit()
    conn.close()
    repo = TagRepository(path)
    assert repo.schema_version() == len(MIGRATIONS)
    rule = FolderRule(str(tmp_path / "wip"), ("WIP",))
    old, new = asset(tmp_path / "wip" / "old.png"), asset(tmp_path / "wip" / "new.png")
    ids = repo.sync_assets([old, new], [rule])
    tags = repo.tags_by_asset(list(ids.values()))
    assert "WIP" not in {t.name for t in tags[ids[str(old.path)]]}
    assert "WIP" in {t.name for t in tags[ids[str(new.path)]]}


# ----------------------------------------------------------------------
# Folder rules
# ----------------------------------------------------------------------
def custom_tags(repo, ids, item):
    return {t.name for t in repo.tags_by_asset([ids[str(item.path)]])[ids[str(item.path)]] if not t.is_default}


def test_files_arriving_in_a_folder_get_its_tags_once(tmp_path):
    repo = TagRepository(tmp_path / "lib.db")
    wip = tmp_path / "Library" / "WIP"
    rules = [FolderRule(str(wip), ("WIP", "Kitbash"))]
    head = asset(wip / "sub" / "head.png")
    elsewhere = asset(tmp_path / "Library" / "done.png")
    ids = repo.sync_assets([head, elsewhere], rules)
    assert custom_tags(repo, ids, head) == {"WIP", "Kitbash"}  # (the tags were made)
    assert custom_tags(repo, ids, elsewhere) == set()
    wip_tag = repo.find_tag("WIP")
    repo.remove_tags([ids[str(head.path)]], [wip_tag.id])
    ids = repo.sync_assets([head, elsewhere], rules)  # the next scan
    assert custom_tags(repo, ids, head) == {"Kitbash"}  # a tag taken off stays off


def test_rules_only_for_their_folder_and_names_of_file_types(tmp_path):
    repo = TagRepository(tmp_path / "lib.db")
    folder = tmp_path / "Refs"
    rules = [FolderRule(str(folder), ("Refs", "PNG"), subfolders=False)]
    top, deeper = asset(folder / "a.png"), asset(folder / "deeper" / "b.png")
    ids = repo.sync_assets([top, deeper], rules)
    assert custom_tags(repo, ids, top) == {"Refs"}  # ("PNG" is the file type tag: left alone)
    assert custom_tags(repo, ids, deeper) == set()
    assert repo.find_tag("PNG").is_default


def test_a_moved_file_gets_the_tags_of_its_new_folder(tmp_path):
    repo = TagRepository(tmp_path / "lib.db")
    first = tmp_path / "Inbox"
    first.mkdir()
    picture = save(scene(2), first / "rock.png")
    old = asset(picture, size=picture.stat().st_size)
    ids = repo.sync_assets([old])
    keep = repo.create_custom_tag("Mine")
    repo.add_tags([ids[str(old.path)]], [keep.id])  # (tagged: moves keep their row)
    repo.fill_missing_hashes()
    final = tmp_path / "Final"
    final.mkdir()
    moved_path = final / "rock.png"
    picture.rename(moved_path)
    moved = asset(moved_path, size=moved_path.stat().st_size)
    rules = [FolderRule(str(final), ("Final",))]
    assert repo.relink_moved_assets([moved]) == 1
    ids = repo.sync_assets([moved], rules)
    assert custom_tags(repo, ids, moved) == {"Mine", "Final"}


def test_tagging_the_files_already_there(tmp_path):
    repo = TagRepository(tmp_path / "lib.db")
    folder = tmp_path / "Kit"
    files = [asset(folder / f"{n}.png") for n in range(3)] + [asset(tmp_path / "other.png")]
    ids = repo.sync_assets(files)
    rule = FolderRule(str(folder), ("Kitbash",))
    assert repo.files_in_folder_rule(rule) == 3
    assert repo.apply_folder_rule(rule) == 3
    assert repo.apply_folder_rule(rule) == 0  # (they have it already)
    assert custom_tags(repo, ids, files[0]) == {"Kitbash"} and custom_tags(repo, ids, files[3]) == set()


def test_a_file_inside_an_archive_is_where_its_archive_is(tmp_path):
    rule = FolderRule(str(tmp_path / "Packs"), ("Packs",), subfolders=False)
    key = os.path.normcase(str(tmp_path / "Packs" / "rocks.zip")) + "|maps\\rock.png"
    assert rule.covers(key)
    assert not rule.covers(os.path.normcase(str(tmp_path / "Packs2" / "x.png")))  # (not a folder that starts the same)


def test_rules_are_kept_in_settings():
    rules = [FolderRule("D:\\Library\\WIP", ("WIP",)), FolderRule("D:\\Refs", ("Refs", "Faces"), subfolders=False)]
    folder_rules.save(rules)
    assert folder_rules.load() == rules
    assert folder_rules.rules_for("d:\\library\\wip\\", rules) == rules[:1]


# ----------------------------------------------------------------------
# The window's parts
# ----------------------------------------------------------------------
def test_search_bar_similar_chip_and_colours(qapp):
    from ui.tag_widgets import TagSearchBar

    bar = TagSearchBar()
    bar.similar_label = lambda asset_id: f"Similar to #{asset_id}"
    changes = []
    bar.changed.connect(lambda: changes.append(1))
    bar.edit.setText("rock similar:42 ")
    assert bar.similar_id() == 42 and bar.text() == "rock"
    assert bar.query_text() == "similar:42 rock"
    assert bar.btn_save.isVisibleTo(bar) and bar.btn_clear.isVisibleTo(bar)
    names = [a.text() for a in bar.btn_color.menu().actions() if a.text()]
    assert names[0] == "Red" and names[-1] == "Pick a Colour..."
    bar.add_color("blue")
    assert bar.text() == "rock color:blue"
    bar.set_query_text("")
    assert bar.similar_id() is None
    bar.set_similar(7)
    assert bar.query_text() == "similar:7"
    bar.clear()
    assert bar.similar_id() is None and changes
    bar.deleteLater()


def test_picking_a_colour_follows_the_picker(qapp):
    from PySide6.QtGui import QColor

    from ui.tag_widgets import TagSearchBar

    bar = TagSearchBar()
    bar.edit.setText("rock ")
    bar.pick_color()
    bar._on_color_picked(QColor("#112233"))
    bar._on_color_picked(QColor("#445566"))
    assert bar.text() == "rock color:#445566"  # (the picker's colour replaced, not added again)
    bar._color_popup.close()
    bar.deleteLater()


def test_menu_entries_for_pictures(qapp):
    from PySide6.QtWidgets import QWidget

    from ui.asset_menu import ArtbookSender, AssetMenuActions, build_asset_menu

    picked = []
    actions = AssetMenuActions(
        open_viewer=lambda: None, reveal=lambda: None, tag=lambda: None, toggle_favorite=lambda: None,
        set_rating=lambda r: None, find_similar=lambda: picked.append("similar"),
        colors=(((214, 40, 30), 0.6),), search_color=picked.append, add_folder_rule=lambda: picked.append("rule"),
    )
    parent = QWidget()
    menu = build_asset_menu(parent, asset(Path("D:/x/rock.png")), [], [], actions, ArtbookSender(None, picked.append))
    texts = {a.text(): a for a in menu.actions()}
    texts["Find Similar"].trigger()
    texts["Folder Rule..."].trigger()
    colours = texts["Search by Its Colours"].menu().actions()
    assert colours[0].text().startswith("Red") and "#d6281e" in colours[0].text()
    colours[0].trigger()
    assert picked == ["similar", "rule", "#d6281e"]
    menu = build_asset_menu(parent, asset(Path("D:/x/notes.pdf")), [], [], actions, ArtbookSender(None, picked.append))
    assert "Find Similar" not in {a.text() for a in menu.actions()}
    parent.deleteLater()


def test_folder_rule_dialog(qapp, tmp_path):
    from ui.folder_rules_dialog import FolderRuleDialog, split_tags

    assert split_tags(" WIP, kitbash ,wip,, Kitbash ") == ("WIP", "kitbash")
    repo = TagRepository(tmp_path / "lib.db")
    repo.sync_assets([asset(tmp_path / "Kit" / "a.png"), asset(tmp_path / "Kit" / "b.png")])
    dialog = FolderRuleDialog(repo, folder=str(tmp_path / "Kit"))
    assert not dialog.btn_ok.isEnabled()  # (no tags yet)
    dialog.txt_tags.setText("PNG")
    assert "file type tag" in dialog.problem()
    dialog.txt_tags.setText("Kitbash, WIP")
    assert dialog.btn_ok.isEnabled() and dialog.tag_existing()
    assert "2 files" in dialog.chk_existing.text()
    assert dialog.rule() == FolderRule(str(tmp_path / "Kit"), ("Kitbash", "WIP"))
    dialog.deleteLater()


def test_settings_page_keeps_rules_and_follows_tag_renames(qapp, tmp_path):
    from ui.folder_rules_dialog import FolderRulesPage

    repo = TagRepository(tmp_path / "lib.db")
    files = [asset(tmp_path / "Kit" / "a.png")]
    ids = repo.sync_assets(files)
    page = FolderRulesPage(repo, [FolderRule(str(tmp_path / "Kit"), ("Kitbash", "Old"))])
    page.rename_tag("old", "New")
    page.remove_tag("Kitbash")
    assert page.rules() == [FolderRule(str(tmp_path / "Kit"), ("New",))]
    page._tag_now = page.rules()
    assert page.changed() and page.apply() == 1
    assert folder_rules.load() == page.rules()
    assert custom_tags(repo, ids, files[0]) == {"New"}
    page.remove_tag("New")  # (a rule left with no tags goes)
    assert page.rules() == []
    page.deleteLater()


def test_settings_has_the_page(qapp):
    from ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog()
    assert "Folder Rules" in dialog.nav_items
    dialog.deleteLater()


def test_lookalike_dialog_groups(qapp, tmp_path):
    from ui.duplicates_dialog import DuplicatesDialog, lookalike_groups

    big = asset(save(scene(6), tmp_path / "big.png"), size=5000)
    small = asset(save(scene(6).resize((200, 200)), tmp_path / "small.jpg"), size=900)
    other = asset(save(scene(7), tmp_path / "other.png"), size=4000)
    pairs = [(a, read_look(str(a.path))) for a in (small, other, big)]
    groups = lookalike_groups(pairs)
    assert [[a.name for a in g.files] for g in groups] == [["big.png", "small.jpg"]]  # the biggest first
    dialog = DuplicatesDialog(TagRepository(":memory:"), [a for a, _ in pairs], looks=pairs)
    dialog._show(groups)
    assert dialog.windowTitle() == "Find Look-alikes" and "look the same" in dialog.lbl_summary.text()
    assert dialog.tree.topLevelItem(0).text(0).startswith("2 look-alikes")
    dialog._tick_all_but_oldest()
    ticked = [c.text(0) for c in dialog._ticked()]
    assert ticked == ["small.jpg"]
    dialog.deleteLater()


def test_library_looks_at_pictures_in_the_background(qapp, tmp_path):
    from ui.library_state import LibraryState

    repo = TagRepository(tmp_path / "lib.db")
    files = [asset(save(scene(n), tmp_path / f"p{n}.png")) for n in range(5)]
    state = LibraryState(repo)
    state.assets = files
    state.ids = repo.sync_assets(files)
    loaded = []
    state.looks_loaded.connect(lambda: loaded.append(1))
    state.start_meta_indexing()
    assert pump(qapp, lambda: len(state.looks) == 5 and state.looks_todo == 0)
    assert all(look.comparable for look in state.looks.values()) and loaded
    assert repo.assets_needing_look(list(state.ids.values())) == []  # stored
    state.looks.clear()
    state.start_meta_indexing()  # (next start: read back, not looked at again)
    assert pump(qapp, lambda: len(state.looks) == 5)
    # Find Similar on a picture not looked at yet
    extra = asset(save(scene(0).resize((300, 300)), tmp_path / "copy.png"))
    state.ids.update(repo.sync_assets([extra]))
    got = []
    state.look_now(extra, got.append)
    assert pump(qapp, lambda: got)
    assert got[0].comparable and find_similar(got[0], state.looks, exclude=state.id_of(extra))
    state.shutdown()


def test_big_pictures_are_read_one_at_a_time(tmp_path, monkeypatch):
    small = save(scene(1, 64), tmp_path / "small.png")
    big = save(scene(2, 300), tmp_path / "big.png")
    assert picture_look._pixel_count(str(big), "big.png") == 90_000
    monkeypatch.setattr(picture_look, "HEAVY_PIXELS", 10_000)
    heavy = []
    real = picture_look._one_at_a_time
    monkeypatch.setattr(picture_look, "_one_at_a_time", lambda h: heavy.append(h) or real(h))
    assert read_look(str(small)).comparable and read_look(str(big)).comparable
    assert heavy == [False, True]
    psd_header = b"8BPS" + b"\x00\x01" + b"\x00" * 6 + b"\x00\x03" + (2000).to_bytes(4, "big") + (3000).to_bytes(4, "big")
    assert picture_look._pixel_count(psd_header + b"\x00" * 4, "x.psd") == 6_000_000


def test_huge_exrs_are_skipped(tmp_path, monkeypatch):
    exr = tmp_path / "layers.exr"
    exr.write_bytes(b"\x00" * 2048)
    monkeypatch.setattr(picture_look, "MAX_EXR_BYTES", 1024)
    assert read_look(str(exr)) is picture_look.UNREADABLE
