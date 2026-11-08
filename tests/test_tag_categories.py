"""Sidebar categories that show files by tag (core.category_rules): a file shows when its type is one
of the category's OR it passes the tag condition (any of the tags, or all of them)."""

from __future__ import annotations

from core import category_rules, settings
from core.asset_filter import filter_assets
from core.category_rules import RowFilter, Rule, everything_in, row_filter, rule_of
from tests.test_main_window import library, names_in_grid, open_window, pump  # noqa: F401 (fixture)
from tests.test_tag_repository import make_asset

ASSETS = [make_asset(p) for p in (r"C:\lib\hero.fbx", r"C:\lib\ref.png", r"C:\lib\turn.mp4", r"C:\lib\wall.png")]
IDS = {str(a.path): i for i, a in enumerate(ASSETS, start=1)}
TAGS = {2: frozenset({"main"}), 3: frozenset({"main", "wip"}), 4: frozenset({"wip"})}  # (folded names)


def names(found):
    return [a.name for a in found]


def test_a_file_shows_for_its_type_or_its_tags():
    main_project = rule_of({"extensions": [".fbx"], "tags": ["Main"]})
    assert main_project.matches(".fbx", frozenset())  # the type alone
    assert main_project.matches(".png", frozenset({"main"}))  # the tag alone, whatever the type
    assert not main_project.matches(".png", frozenset({"wip"}))

    both = rule_of({"tags": ["Main", "  WIP "], "tag_match": "all"})
    assert both.tags == {"main", "wip"} and both.labels == ("Main", "WIP")
    assert both.matches(".mp4", frozenset({"main", "wip"})) and not both.matches(".mp4", frozenset({"main"}))
    either = rule_of({"tags": ["Main", "WIP"]})
    assert either.matches(".png", frozenset({"wip"}))
    assert not rule_of({}).matches(".png", frozenset({"main"}))  # (no types, no tags: nothing)


def test_the_grid_shows_a_tag_category_and_uncategorized_is_what_no_category_shows():
    shows = row_filter({"name": "Main Project", "extensions": [".fbx"], "tags": ["main"], "subcategories": []})
    found = filter_assets(ASSETS, mode="category", row=shows, ids=IDS, tag_names=TAGS)
    assert names(found) == ["hero.fbx", "ref.png", "turn.mp4"]
    every_wip = row_filter({"tags": ["WIP", "Main"], "tag_match": "all"})
    assert names(filter_assets(ASSETS, mode="category", row=every_wip, ids=IDS, tag_names=TAGS)) == ["turn.mp4"]

    categories = [{"name": "Models", "extensions": [".fbx"], "subcategories": [{"name": "Main", "extensions": [], "tags": ["Main"]}]}]
    categorized = everything_in(categories)
    found = filter_assets(ASSETS, mode="uncategorized", categorized=categorized, ids=IDS, tag_names=TAGS)
    assert names(found) == ["wall.png"]  # (ref.png and turn.mp4 show under Main, through their tag)
    # A main category shows its subcategories' files too, tags included
    assert names(filter_assets(ASSETS, mode="category", row=row_filter(categories[0]), ids=IDS, tag_names=TAGS)) == [
        "hero.fbx", "ref.png", "turn.mp4",
    ]


def test_row_tooltips_say_what_a_category_shows():
    assert row_filter({"extensions": [".png", ".jpg"]}).describe() == ".jpg, .png"
    assert row_filter({"extensions": [".fbx"], "tags": ["Main"]}).describe() == ".fbx\nTagged Main"
    assert row_filter({"tags": ["Main", "WIP"], "tag_match": "all"}).describe() == "Tagged all of Main, WIP"
    assert RowFilter().describe() == "No file types or tags assigned"
    assert not row_filter({"extensions": [".png"]}).uses_tags and row_filter({"tags": ["x"]}).uses_tags


def test_tags_are_saved_with_a_category_and_renamed_or_dropped_with_the_tag():
    entry = {"name": "Main Project", "extensions": [".fbx"], "tags": ["Main", " main ", "", 5, "Hero  Shot"], "tag_match": "all"}
    settings.set_categories([{**entry, "subcategories": [{"name": "Refs", "extensions": [], "tags": "junk", "tag_match": "x"}]}])
    saved = settings.get_categories()[0]
    assert saved["tags"] == ["Main", "main", "Hero Shot"] and saved["tag_match"] == "all"
    assert "tags" not in saved["subcategories"][0] and "tag_match" not in saved["subcategories"][0]
    assert rule_of(saved).tags == {"main", "hero shot"}

    assert category_rules.renamed(["Main", "WIP"], "main", "Hero") == ["Hero", "WIP"]
    assert category_rules.renamed(["Main", "Hero"], "Main", "hero") == ["hero", "Hero"]  # (a merge keeps both spellings once each)
    assert category_rules.without(["Main", "WIP"], "MAIN") == ["WIP"]


def test_the_sidebar_counts_files_by_tag(qapp):
    from core.tag_repository import get_repository
    from ui.library_sidebar import LibrarySidebar

    settings.set_categories([
        {"name": "Main Project", "extensions": [".fbx"], "tags": ["Main"], "subcategories": [
            {"name": "Work", "extensions": [], "tags": ["WIP"]},
        ]},
        {"name": "Images", "extensions": [".png"], "subcategories": []},
    ])
    sidebar = LibrarySidebar(get_repository())
    assert sidebar.uses_tags
    sidebar.update_counts(ASSETS, lambda asset: False, IDS, TAGS)
    texts = [item.text(0) for item, _name, _shows in sidebar.rows()]
    assert texts == ["Main Project (4)", "Work (2)", "Images (2)"]
    assert sidebar.item_uncategorized.isHidden()  # (turn.mp4 shows under Main Project, by its tags)
    sidebar.update_counts(ASSETS, lambda asset: False)  # (tags not loaded yet: by type only)
    assert [item.text(0) for item, _n, _s in sidebar.rows()] == ["Main Project (1)", "Work (0)", "Images (2)"]
    assert sidebar.item_uncategorized.text(0) == "Uncategorized (1)"
    sidebar.deleteLater()


def test_the_window_shows_a_tag_category_and_follows_tagging(library, qapp):  # noqa: F811
    from core.tag_repository import get_repository

    tag = get_repository().create_custom_tag("Main")
    settings.set_categories([{"name": "Main Project", "extensions": [".fbx"], "tags": ["Main"], "subcategories": []}])
    window = open_window(qapp)
    row = next(item for item, name, _shows in window.sidebar_tree.rows() if name == "Main Project")
    assert row.text(0) == "Main Project (1)" and row.toolTip(0) == ".fbx\nTagged Main"
    window.sidebar_tree._on_item_clicked(row)
    assert names_in_grid(window) == ["chair.fbx"]
    assert window.sidebar_tree.item_uncategorized.text(0) == "Uncategorized (2)"

    brick = window.library.ids[str((library / "small_brick.png").resolve())]
    window.repo.add_tags([brick], [tag.id])
    window._after_tags_changed([brick])
    assert names_in_grid(window) == ["chair.fbx", "small_brick.png"]  # (shown straight away)
    assert row.text(0) == "Main Project (2)" and window.sidebar_tree.item_uncategorized.text(0) == "Uncategorized (1)"

    window.repo.remove_tags([brick], [tag.id])
    window._after_tags_changed([brick])
    assert names_in_grid(window) == ["chair.fbx"] and row.text(0) == "Main Project (1)"
    window.close()


def test_settings_pick_tags_and_follow_the_tags_page(qapp, monkeypatch):
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    from core.tag_repository import get_repository
    from ui.settings_dialog import SettingsDialog

    repo = get_repository()
    repo.create_custom_tag("Main")
    repo.create_custom_tag("WIP")
    settings.set_categories([{"name": "Main Project", "extensions": [], "tags": ["main", "Gone"], "subcategories": []}])
    dialog = SettingsDialog()
    page = dialog.page_categories
    page.tree.setCurrentItem(page.tree.topLevelItem(0))
    listed = [page.list_tags.item(i).text() for i in range(page.list_tags.count())]
    assert listed == ["Main", "WIP", "Gone   (no tag has this name)"]
    checked = [page.list_tags.item(i).checkState() == page.list_tags.item(0).checkState() for i in range(3)]
    assert checked == [True, False, True]
    assert page.btn_show_tags.text() == "Tags (2)" and page.btn_show_types.text() == "File Types"

    page.list_tags.item(2).setCheckState(page.list_tags.item(1).checkState())  # untick Gone
    page.list_tags.item(1).setCheckState(page.list_tags.item(0).checkState())  # tick WIP
    page.btn_all_tags.click()
    assert page.categories()[0]["tags"] == ["Main", "WIP"] and page.categories()[0]["tag_match"] == "all"

    # Renamed and deleted on the Tags page: the category follows
    tags_page = dialog.page_tags
    wip = next(t for t in repo.list_tags(kind="custom") if t.name == "WIP")
    tags_page.refresh(select_id=wip.id)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Work", True)))
    tags_page._on_rename()
    assert page.categories()[0]["tags"] == ["Main", "Work"]
    assert [page.list_tags.item(i).text() for i in range(page.list_tags.count())] == ["Main", "Work"]
    main = next(t for t in repo.list_tags(kind="custom") if t.name == "Main")
    tags_page.refresh(select_id=main.id)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    tags_page._on_delete()
    assert page.categories()[0]["tags"] == ["Work"] and page.categories()[0]["name"] == "Main Project"

    # Right-click a tag > Add as Sidebar Category
    tags_page.category_requested.emit("Work")
    assert dialog.stack.currentWidget() is page and page.right_stack.currentIndex() == 1
    assert page.categories()[-1] == {"name": "Work", "color": page.categories()[-1]["color"], "extensions": [],
                                     "tags": ["Work"], "subcategories": []}
    dialog._on_save()
    assert dialog.categories_changed and [c.get("tags") for c in settings.get_categories()] == [["Work"], ["Work"]]
    dialog.deleteLater()


def test_cancel_puts_the_tags_and_categories_back(qapp, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    from core.tag_repository import get_repository
    from ui.settings_dialog import SettingsDialog

    tag = get_repository().create_custom_tag("Main")
    settings.set_categories([{"name": "Main Project", "extensions": [], "tags": ["Main"], "subcategories": []}])
    dialog = SettingsDialog()
    dialog.page_tags.refresh(select_id=tag.id)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Hero", True)))
    dialog.page_tags._on_rename()
    assert dialog.page_categories.categories()[0]["tags"] == ["Hero"]
    dialog.reject()
    assert get_repository().get_tag(tag.id).name == "Main" and settings.get_categories()[0]["tags"] == ["Main"]
    dialog.deleteLater()


def test_a_category_with_no_rule_shows_nothing():
    assert row_filter({"name": "Empty", "subcategories": []}).rules == ()
    assert filter_assets(ASSETS, mode="category", row=RowFilter(), ids=IDS, tag_names=TAGS) == []
    assert Rule(frozenset({".png"})) == rule_of({"extensions": [".png"]})
