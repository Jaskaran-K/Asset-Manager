"""RAR and 7z archives through 7-Zip (core.archives): finding 7-Zip, listing, reading (multi-part RARs
included), stopping it, and what happens without it. The RAR files in tests/data were made with
WinRAR (7-Zip can't write RAR); 7z archives are made here by 7-Zip itself."""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from core import archives
from core.scanner import AssetScanner

DATA = Path(__file__).parent / "data"
SEVEN_ZIP = archives.find_seven_zip()
needs_seven_zip = pytest.mark.skipif(SEVEN_ZIP is None, reason="7-Zip isn't installed")


@pytest.fixture(autouse=True)
def fresh():
    archives.forget_listings()
    archives.configure_seven_zip("")
    yield
    archives.configure_seven_zip("")
    archives.forget_listings()


def make_7z(folder: Path, files: dict[str, bytes]) -> Path:
    source = folder / "src"
    for name, data in files.items():
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_bytes(data)
    target = folder / "pack.7z"
    subprocess.run([SEVEN_ZIP, "a", "-bd", "-sccUTF-8", str(target), "*"], cwd=source, check=True,
                   capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return target


def by_name(result):
    return {m.name.replace("\\", "/"): m for m in result.members}


# ----------------------------------------------------------------------
# Finding 7-Zip
# ----------------------------------------------------------------------
def fake_exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ")
    return path


def test_a_configured_7zip_wins_and_a_wrong_one_is_not_replaced(tmp_path):
    exe = fake_exe(tmp_path / "tools" / "7z.exe")
    assert archives.find_seven_zip(str(exe)) == str(exe)
    assert archives.find_seven_zip(str(exe.parent)) == str(exe)  # (its folder works too)
    assert archives.find_seven_zip(str(tmp_path / "nowhere" / "7z.exe")) is None


def test_7zip_is_found_in_program_files_the_registry_or_scoop(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(archives, "_registered_seven_zip_folders", lambda: [])
    for var in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        monkeypatch.setenv(var, str(tmp_path / "empty"))
    monkeypatch.setenv("SCOOP", str(tmp_path / "scoop"))
    assert archives.find_seven_zip() is None

    scoop = fake_exe(tmp_path / "scoop" / "apps" / "7zip" / "current" / "7z.exe")
    assert archives.find_seven_zip() == str(scoop)
    registered = fake_exe(tmp_path / "registered" / "7z.exe")
    monkeypatch.setattr(archives, "_registered_seven_zip_folders", lambda: [str(registered.parent)])
    assert archives.find_seven_zip() == str(registered)
    installed = fake_exe(tmp_path / "Program Files" / "7-Zip" / "7z.exe")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    assert archives.find_seven_zip() == str(installed)


def test_without_7zip_rar_cards_say_so_and_nothing_is_remembered(tmp_path, qapp):
    from core.thumbnail_manager import _Request, _ThumbnailRunnable, card_style

    shutil.copy(DATA / "sample.rar", tmp_path / "sample.rar")
    archives.configure_seven_zip(str(tmp_path / "missing" / "7z.exe"))
    result = archives.listing(tmp_path / "sample.rar")
    assert result.error == archives.NO_SEVEN_ZIP
    assert not archives.index_dir().exists() or not list(archives.index_dir().iterdir())
    (card,) = AssetScanner(supported_extensions=[".rar"]).scan(tmp_path)
    assert _ThumbnailRunnable(_Request("x"), card, 110, None, card_style())._render() == (None, archives.NO_SEVEN_ZIP)
    if SEVEN_ZIP is not None:
        archives.configure_seven_zip("")
        assert not archives.listing(tmp_path / "sample.rar").error  # (found once it's there)


# ----------------------------------------------------------------------
# RAR
# ----------------------------------------------------------------------
@needs_seven_zip
def test_a_rar_lists_its_files_but_not_its_folders():
    members = by_name(archives.listing(DATA / "sample.rar"))
    assert sorted(members) == ["images/stone.png", "images/sub/leaf.jpg", "notes.txt"]
    leaf = members["images/sub/leaf.jpg"]
    assert leaf.kind == "7z" and leaf.folder == "images/sub" and leaf.base_name == "leaf.jpg" and leaf.crc


@needs_seven_zip
def test_files_inside_a_rar_are_read_and_become_assets(tmp_path):
    archive = shutil.copy(DATA / "sample.rar", tmp_path / "sample.rar")
    assets = sorted(archives.entry_assets(Path(archive), [".png", ".jpg"]), key=lambda a: a.name)
    assert [a.name for a in assets] == ["leaf.jpg", "stone.png"]
    assert assets[0].in_archive and str(assets[0].path).endswith("sample.rar|images\\sub\\leaf.jpg")
    with Image.open(io.BytesIO(archives.read_bytes(assets[1].archive))) as im:
        assert im.size == (48, 32) and im.convert("RGB").getpixel((0, 0)) == (30, 120, 220)
    path = archives.materialize(assets[0].archive)
    assert path.name == "leaf.jpg" and Image.open(path).size == (40, 40)


@needs_seven_zip
def test_a_multi_part_rar_is_one_card_with_all_its_size(tmp_path):
    for part in DATA.glob("volume.part*.rar"):
        shutil.copy(part, tmp_path / part.name)
    total = sum(p.stat().st_size for p in tmp_path.glob("volume.part*.rar"))
    (card,) = AssetScanner(supported_extensions=[".rar"]).scan(tmp_path)
    assert card.name == "volume.part1.rar" and card.size_bytes == total
    (noise,) = archives.entry_assets(card.path, [".png"], card.size_bytes, card.modified_timestamp)
    data = archives.read_bytes(noise.archive)  # (it spans all three parts)
    with Image.open(io.BytesIO(data)) as im:
        assert im.size == (120, 120)
    assert archives.listing(card.path) is archives.listing(card.path, card.size_bytes, card.modified_timestamp)


@needs_seven_zip
def test_a_rar_cards_picture_comes_from_inside(tmp_path, qapp):
    from core.thumbnail_manager import _Request, _ThumbnailRunnable, archive_label, card_style

    shutil.copy(DATA / "sample.rar", tmp_path / "sample.rar")
    (card,) = AssetScanner(supported_extensions=[".rar"]).scan(tmp_path)
    picture, _note = _ThumbnailRunnable(_Request("x"), card, 110, None, card_style())._render()
    assert picture is not None and picture.pixelColor(picture.width() // 2, picture.height() // 2).blue() > 200
    assert archive_label(card) == "RAR · 3"


# ----------------------------------------------------------------------
# 7z
# ----------------------------------------------------------------------
@needs_seven_zip
def test_7z_members_read_back_exactly_whatever_their_names(tmp_path):
    files = {
        "plain.bin": bytes(range(256)) * 3000,
        "brackets [v2] *star?.bin".replace("*", "").replace("?", ""): b"brackets",
        "Größe/テクスチャ.png": b"unicode name",
        "-starts with a dash.bin": b"dash",
    }
    archive = make_7z(tmp_path, files)
    members = by_name(archives.listing(archive))
    assert sorted(members) == sorted(files)
    for name, data in files.items():
        assert archives.read_bytes(members[name]) == data, name


@needs_seven_zip
def test_reading_stops_7zip_when_the_reader_stops(tmp_path):
    archive = make_7z(tmp_path, {"big.bin": bytes(range(256)) * 20000})
    (member,) = archives.listing(archive).members
    chunks = archives.iter_chunks(member)
    assert next(chunks)
    chunks.close()  # (a header read: 7-Zip is stopped, not left running)
    calls = []
    with pytest.raises(archives.Cancelled):
        archives.read_bytes(member, should_stop=lambda: calls.append(1) or len(calls) > 1)


@needs_seven_zip
def test_a_damaged_7z_says_so(tmp_path):
    archive = make_7z(tmp_path, {"a.bin": bytes(range(256)) * 4000})
    data = bytearray(archive.read_bytes())
    data[200:400] = bytes(200)
    archive.write_bytes(bytes(data))
    result = archives.listing(archive)
    if result.error:
        assert result.error == "Damaged or not an archive"
    else:
        with pytest.raises(archives.ArchiveError, match="damaged"):
            archives.read_bytes(result.members[0])


def test_the_archives_settings_page_saves_7zips_location(tmp_path, qapp):
    from core.settings import get_seven_zip_path
    from ui.settings_dialog import SettingsDialog

    exe = fake_exe(tmp_path / "tools" / "7z.exe")
    dialog = SettingsDialog()
    page = dialog.page_archives
    page.txt_seven_zip.setText(str(tmp_path / "missing.exe"))
    assert "No 7z.exe" in page.lbl_status.label.text()
    page.txt_seven_zip.setText(str(exe))
    assert str(exe) in page.lbl_status.label.text()
    dialog._on_save()
    assert get_seven_zip_path() == str(exe) and dialog.archives_changed
    assert archives.seven_zip() == str(exe)
    dialog.deleteLater()
