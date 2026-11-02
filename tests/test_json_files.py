"""core.json_files and what uses it: a damaged file opens from its backup instead of being lost
(Artbook pages are in test_artbook_pages; here the Artbook's folder index and PDF notes)."""

from __future__ import annotations

import json

from core import artbook_store as store
from core import crash_log, json_files, pdf_notes


def _is_dict(data):
    return isinstance(data, dict)


def test_each_save_keeps_the_one_before(tmp_path):
    path = tmp_path / "data.json"
    json_files.write(path, {"n": 1}, _is_dict)
    assert not json_files.backup_path(path).exists()  # (nothing before the first save)
    json_files.write(path, {"n": 2}, _is_dict)
    json_files.write(path, {"n": 3}, _is_dict)
    assert json_files.read(path) == {"n": 3} and json_files.read(json_files.backup_path(path)) == {"n": 2}
    assert not list(tmp_path.glob("*.tmp"))


def test_a_damaged_file_reads_from_its_backup_and_is_set_aside_on_save(tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(crash_log, "log_error", lambda title, details="": logged.append(title))
    path = tmp_path / "data.json"
    json_files.write(path, {"n": 1}, _is_dict)
    json_files.write(path, {"n": 2}, _is_dict)
    path.write_text("{damaged", encoding="utf-8")
    assert json_files.read_with_backup(path, _is_dict) == ({"n": 1}, True)
    json_files.read_with_backup(path, _is_dict)
    assert len(logged) == 1  # (once)
    json_files.write(path, {"n": 3}, _is_dict)
    assert json_files.damaged_path(path).read_text(encoding="utf-8") == "{damaged"
    assert json_files.read(json_files.backup_path(path)) == {"n": 1}  # (the damaged one never becomes the backup)
    assert json_files.read_with_backup(path, _is_dict) == ({"n": 3}, False)


def test_unusable_contents_count_as_damaged(tmp_path):
    path = tmp_path / "data.json"
    path.write_text("[1, 2]", encoding="utf-8")  # (valid JSON, but not what's expected)
    assert json_files.read(path, _is_dict) is None
    assert json_files.read_with_backup(path, _is_dict) == (None, False)


def test_a_damaged_artbook_index_keeps_its_folders(qapp):
    folder = store.create_folder("Characters")
    page = store.create_page("Hero", folder)  # (the index saved again: the one before is the backup)
    store.create_folder("Props")
    store.index_path().write_text("{damaged", encoding="utf-8")
    tree = store.page_tree()
    assert [f["name"] for f in tree["folders"]] == ["Characters"]  # (the save before "Props")
    assert tree["folders"][0]["pages"] == [page["id"]]
    saved = json.loads(store.index_path().read_text(encoding="utf-8"))  # (repaired from the backup)
    assert [f["name"] for f in saved["folders"]] == ["Characters"]


def test_damaged_pdf_notes_open_from_their_backup(isolated_user_data):
    pdf_notes.save("abc", {"marks": [{"page": 1}]})
    pdf_notes.save("abc", {"marks": [{"page": 1}, {"page": 2}]})
    (pdf_notes.notes_dir() / "abc.json").write_text("{damaged", encoding="utf-8")
    assert pdf_notes.load("abc")["marks"] == [{"page": 1}]  # (not {}: the next mark would have saved over them)
    assert pdf_notes.load(None) == {} and pdf_notes.load("never-opened") == {}
