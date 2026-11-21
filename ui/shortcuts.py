"""Single source of truth for every keyboard shortcut and mouse gesture in the app.

Rule: a shortcut only exists if it is listed here. Code binds or checks keys by id,
never with hard-coded keys:

    button.setShortcut(sequence("main.tag"))         # QAction / QPushButton shortcuts
    if matches("viewer3d.grid", event): ...           # keyPressEvent / event filters

Settings > General > Shortcuts is generated from SHORTCUTS, so registering a shortcut
here is all it takes to document it. tests/test_shortcuts.py fails if a hard-coded key
(Qt.Key.Key_*, setShortcut("..."), QKeySequence("...")) appears anywhere else, or if code
uses an id that isn't registered. Mouse gestures can't be bound by id; list them here
with sequence=None so they still appear on the Shortcuts page.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QKeyCombination, Qt
from PySide6.QtGui import QKeyEvent, QKeySequence


@dataclass(frozen=True)
class Shortcut:
    id: str
    category: str
    keys: str  # shown to the user
    action: str
    sequence: Optional[str] = None  # QKeySequence text the code matches; None for mouse gestures / typed text


# Categories in the order the Shortcuts page shows them
MAIN = "Asset Browser"
SEARCH = "Search Bar"
VIEWER = "Asset Viewer"
PDF = "PDF Viewer"
MODEL = "3D Model Viewer"
IMAGE = "Image Viewer"
VIDEO = "Video Player"
TAG_DIALOG = "Tag Dialog"
THEME_STUDIO = "Theme Studio"
VIEWS = "Views"
ARTBOOK = "Artbook"
PAGES = "Artbook Pages"
FIND = "Artbook Find"
COURSE = "Courses"
KEYDIAL = "KeyDial (Huion KD100)"
CATEGORIES = (VIEWS, MAIN, SEARCH, ARTBOOK, FIND, PAGES, COURSE, VIEWER, PDF, MODEL, IMAGE, VIDEO, TAG_DIALOG, THEME_STUDIO,
              KEYDIAL)

SHORTCUTS: tuple[Shortcut, ...] = (
    # Views (both windows)
    Shortcut("view.asset", VIEWS, "Ctrl+1", "Asset View: the asset browser", "Ctrl+1"),
    Shortcut("view.artbook", VIEWS, "Ctrl+2", "Artbook View: your reference boards", "Ctrl+2"),
    Shortcut("view.split", VIEWS, "Ctrl+3", "Split View: assets and Artbook side by side", "Ctrl+3"),
    Shortcut("view.separate", VIEWS, "Ctrl+4", "Separate View: the Artbook in its own window", "Ctrl+4"),
    Shortcut("view.course", VIEWS, "Ctrl+5", "Course View: your courses, in their own window", "Ctrl+5"),
    Shortcut("view.switch", VIEWS, "Click View", "Switch between Asset and Artbook view (the arrow lists every view)"),
    Shortcut("view.split_drag", VIEWS, "Drag the divider", "Split View: give one side more room (remembered)"),
    Shortcut("view.window_options", VIEWS, "Click Window", "Separate View: always on top, opacity and click-through for the Artbook window"),
    Shortcut("view.click_through", VIEWS, "Ctrl+Alt+T", "Separate View: turn click-through on / off. Works from any app", "Ctrl+Alt+T"),
    Shortcut("view.click_through_tray", VIEWS, "Click the tray icon", "Turn click-through off (the icon shows while it's on)"),

    # Asset browser (main window)
    Shortcut("main.preview", MAIN, "Space", "Preview the selected asset", "Space"),
    Shortcut("main.open", MAIN, "Double-click", "Open an asset in the viewer"),
    Shortcut("main.tag", MAIN, "Ctrl+T", "Tag the selected files", "Ctrl+T"),
    Shortcut("main.settings", MAIN, "Ctrl+,", "Open Settings", "Ctrl+,"),
    Shortcut("main.profiles", MAIN, "Ctrl+Shift+P", "Switch profile (each has its own folders, file types, categories and collections)", "Ctrl+Shift+P"),
    Shortcut("main.undo", MAIN, "Ctrl+Z", "Undo the last tag, favorite or rating change", "Ctrl+Z"),
    Shortcut("main.redo", MAIN, "Ctrl+Y", "Redo it", "Ctrl+Y"),
    Shortcut("main.favorite", MAIN, "F", "Add / remove the selected files from Favorites", "F"),
    Shortcut("main.rate_1", MAIN, "1", "Rate the selected files ★", "1"),  # glyph-ok: rating text
    Shortcut("main.rate_2", MAIN, "2", "Rate ★★", "2"),  # glyph-ok
    Shortcut("main.rate_3", MAIN, "3", "Rate ★★★", "3"),  # glyph-ok
    Shortcut("main.rate_4", MAIN, "4", "Rate ★★★★", "4"),  # glyph-ok
    Shortcut("main.rate_5", MAIN, "5", "Rate ★★★★★", "5"),  # glyph-ok
    Shortcut("main.rate_0", MAIN, "0", "Clear the rating", "0"),
    Shortcut("main.multi_select", MAIN, "Ctrl+Click / Shift+Click", "Select several files"),
    Shortcut("main.open_archive", MAIN, "Double-click an archive", "Look inside a ZIP, RAR or 7z like a folder (Space too)"),
    Shortcut("main.leave_archive", MAIN, "Backspace", "Leave the open archive: back to where you were", "Backspace"),
    Shortcut("main.open_board", MAIN, "Double-click a PureRef board", "Open it as an Artbook page, the same page each time (Space too)"),
    Shortcut("main.open_in", MAIN, "Right-click > Open In", "Open the file in ZBrush, Blender, Maya, Photoshop, Painter, Toolbag or an app you added (Settings > Apps)"),
    Shortcut("main.context_menu", MAIN, "Right-click", "File actions (preview, tag, open, copy path...)"),
    Shortcut("main.send_to_artbook", MAIN, "Right-click > Send to Artbook", "Add the selected images to any Artbook page, or a new one"),
    Shortcut("main.rename", MAIN, "F2", "Rename the selected files (from a pattern, with a preview first)", "F2"),
    Shortcut("main.texture_set", MAIN, "Double-click a texture set", "Its material on a sphere, cube or plane (Library > Group Texture Sets); drag it to drag all its maps"),
    Shortcut("main.drag_out", MAIN, "Drag files out of the grid", "Copy them into Explorer, Blender, Photoshop, Unreal..."),
    Shortcut("main.sidebar", MAIN, "Ctrl+B", "Show / hide the sidebar (or click the menu button). In Split View: the side you're working in", "Ctrl+B"),
    Shortcut("main.close_sidebar", MAIN, "Esc / click beside it", "Close the floating sidebar (unpinned with the pin button)", "Esc"),
    Shortcut("main.sidebar_resize", MAIN, "Drag the sidebar's right edge", "Make the sidebar wider or narrower"),

    # Search bar
    Shortcut("search.tag_token", SEARCH, "tag:name then Enter or Space", "Add a tag filter (tag:\"two words\" for spaces)"),
    Shortcut("search.exclude_token", SEARCH, "-tag:name", "Only files that don't have the tag"),
    Shortcut("search.any_token", SEARCH, "tag:a|b", "Files with any of the tags"),
    Shortcut("search.toggle_chip", SEARCH, "Click a tag chip", "Switch it between has / doesn't have"),
    Shortcut("search.save", SEARCH, "Click the star in the search box", "Save the search as a sidebar collection"),
    Shortcut("search.details", SEARCH, "is:fav  rating:>=3  ext:png", "Favorites, star rating, file type"),
    Shortcut("search.alpha", SEARCH, "is:alpha", "Alphas: grey height images (-is:alpha: everything else)"),
    Shortcut("search.details_size", SEARCH, "res:>=2048  size:>10mb", "Image / video resolution (res:4k = 4096), file size"),
    Shortcut("search.details_media", SEARCH, "dur:<30s  poly:<10k", "Video length, 3D model faces"),
    Shortcut("search.color", SEARCH, "color:red  color:#c04020", "Pictures with that colour on 15% of them or more (the palette button in the search box lists the names, or picks one)"),
    Shortcut("search.similar", SEARCH, "Right-click a picture > Find Similar", "The picture and its look-alikes (resized or re-saved copies), closest first"),
    Shortcut("search.ai", SEARCH, "Click the AI button in the search box", "AI Search: describe what you're looking for (\"creature skull side view\"); files whose pictures match, best first"),
    Shortcut("search.like", SEARCH, "Right-click a file > More Like This", "Files with the same kind of content (AI Search), closest first"),
    Shortcut("search.remove_last_chip", SEARCH, "Backspace (empty box)", "Remove the last tag filter", "Backspace"),
    Shortcut("search.clear", SEARCH, "Esc", "Clear the search text and all tag filters", "Esc"),

    # Artbook board (while it has the keyboard focus)
    Shortcut("artbook.drop", ARTBOOK, "Drop / drag images and files in", "From Explorer, a web browser or the asset grid (a file that isn't a picture: a card linked to it; a web page's address: a link card)"),
    Shortcut("artbook.tray", ARTBOOK, "U", "The tray: Unsorted (sent from your browser) and this page's Trash", "U"),
    Shortcut("artbook.unsorted_drag", ARTBOOK, "Drag from Unsorted onto the board", "Put it on the page where you let go (it leaves Unsorted)"),
    Shortcut("artbook.comment", ARTBOOK, "M", "Comment: on the selected card or picture, else a pin where the mouse is (also: Right-click > Comment)", "M"),
    Shortcut("artbook.comment_open", ARTBOOK, "Click a comment badge or pin", "Read the comments, tick them done, write more (a hovered card's faint bubble starts one)"),
    Shortcut("artbook.comment_pin_drag", ARTBOOK, "Drag a comment pin", "Move it somewhere else on the board"),
    Shortcut("artbook.card_open", ARTBOOK, "Double-click a link, file or page card", "Open it: the web page in your browser, the file in its app (Maya first), the page (Back, at the top, returns)"),
    Shortcut("artbook.card_buttons", ARTBOOK, "Hover a link, file or page card", "Its buttons: open it, open a file in Maya or another app, More"),
    Shortcut("artbook.page_card_drag", ARTBOOK, "Drag a page from the Pages list onto the board", "A page card: it opens that page (also: Right-click > Add Here > Page Link)"),
    Shortcut("artbook.heading", ARTBOOK, "Shift+T", "New heading at the cursor: a big title with an accent line", "Shift+T"),
    Shortcut("artbook.note_markdown", ARTBOOK, "# , ## , - , 1. or [] then Space", "At the start of a line in a note: a heading, a smaller heading, a bulleted or a numbered list, a checklist"),
    Shortcut("artbook.todo", ARTBOOK, "X", "New to-do at the cursor: a title, then a checklist", "X"),
    Shortcut("artbook.todo_tick", ARTBOOK, "Click a to-do's box", "Tick it (done lines sink to the bottom); click again to untick"),
    Shortcut("artbook.todo_row_drag", ARTBOOK, "Drag a to-do line's dots", "Move the line up or down its list"),
    Shortcut("artbook.column", ARTBOOK, "Shift+C", "New column at the cursor: type its title, then drag cards into it", "Shift+C"),
    Shortcut("artbook.column_drop", ARTBOOK, "Drag a card onto a column", "Put it in where the accent line shows (drag it out to take it out again)"),
    Shortcut("artbook.column_fold", ARTBOOK, "Click a column's arrow", "Fold it to its title and count, or open it again"),
    Shortcut("artbook.column_rename", ARTBOOK, "Double-click a column's title", "Rename it (Enter or Esc when done)"),
    Shortcut("artbook.table", ARTBOOK, "Shift+B", "New table at the cursor: Task, Time and Status columns, ready to type in", "Shift+B"),
    Shortcut("artbook.table_cell", ARTBOOK, "Click a cell of a selected table", "Type in it (double-click works too; on its title or a column's name, rename it)"),
    Shortcut("artbook.table_next", ARTBOOK, "Tab (typing in a table)", "The next cell (past the last one: a new row)", "Tab"),
    Shortcut("artbook.table_prev", ARTBOOK, "Shift+Tab (typing in a table)", "The cell before", "Shift+Backtab"),
    Shortcut("artbook.table_down", ARTBOOK, "Enter (typing in a table)", "The cell below (under the last row: a new row); Shift+Enter: a new line in the cell"),
    Shortcut("artbook.table_status", ARTBOOK, "Click a status chip", "Pick its status, or add, rename and recolour the table's statuses"),
    Shortcut("artbook.table_add", ARTBOOK, "Hover a table, click a +", "Add a row (the + under it) or a column (the + beside it)"),
    Shortcut("artbook.table_row_drag", ARTBOOK, "Drag a table row's dots", "Move the row up or down"),
    Shortcut("artbook.table_column_width", ARTBOOK, "Drag a table column's edge", "Make the column wider or narrower"),
    Shortcut("artbook.line", ARTBOOK, "Shift+L", "Line tool: drag from a card to another to join them (Shift+L again or Esc to stop)", "Shift+L"),
    Shortcut("artbook.line_dots", ARTBOOK, "Drag a dot on a hovered card's side", "Draw a line to another card (with several cards selected: one line to each)"),
    Shortcut("artbook.line_edit", ARTBOOK, "Drag a selected line's ends or middle", "Join an end to another card (or leave it free), or bend the line"),
    Shortcut("artbook.line_label", ARTBOOK, "Double-click a line's label", "Type it (Enter or Esc when done); a line with none: select it, Enter"),
    Shortcut("artbook.line_point", ARTBOOK, "Double-click a line", "A point there: drag it to route the line, join other lines to it (Delete: one line again)"),
    Shortcut("artbook.column_delete", ARTBOOK, "Hover a column, click its x", "Delete it with its cards, or keep the cards on the board"),
    Shortcut("artbook.rail_drag", ARTBOOK, "Drag a tool off the rail", "Put a note (or another card) where you let go on the board; a click adds it in the middle"),
    Shortcut("artbook.import_drop", ARTBOOK, "Drop .artbook / .pur files on the board", "Import shared pages, or PureRef boards with their layouts (also: the Import button in Pages)"),
    Shortcut("artbook.paste", ARTBOOK, "Ctrl+V", "Paste an image, files, or a link (an image's: the image; a web page's: a link card)", "Ctrl+V"),
    Shortcut("artbook.copy", ARTBOOK, "Ctrl+C", "Copy the selected image", "Ctrl+C"),
    Shortcut("artbook.pan", ARTBOOK, "Middle-drag", "Pan around the board"),
    Shortcut("artbook.pan_key", ARTBOOK, "Hold Space + drag", "Pan with the left button", "Space"),
    Shortcut("artbook.zoom", ARTBOOK, "Mouse wheel", "Zoom toward the cursor"),
    Shortcut("artbook.move", ARTBOOK, "Drag an image", "Move it (and everything selected with it)"),
    Shortcut("artbook.select", ARTBOOK, "Click / drag a box", "Select images (click empty space to clear)"),
    Shortcut("artbook.multi_select", ARTBOOK, "Shift+Click / Ctrl+Click", "Add an image to the selection, or take it out"),
    Shortcut("artbook.scale", ARTBOOK, "Drag a corner handle", "Scale the selection (keeps its shape)"),
    Shortcut("artbook.rotate", ARTBOOK, "Drag the round handle", "Rotate the image (Shift: 15° steps)"),
    Shortcut("artbook.zoom_to", ARTBOOK, "Double-click an image", "Zoom to it"),
    Shortcut("artbook.fit", ARTBOOK, "F", "Frame the selection, or everything", "F"),
    Shortcut("artbook.zoom_100", ARTBOOK, "Ctrl+0", "Zoom to 100%", "Ctrl+0"),
    Shortcut("artbook.flip_h", ARTBOOK, "H", "Flip the selection horizontally", "H"),
    Shortcut("artbook.flip_v", ARTBOOK, "V", "Flip the selection vertically", "V"),
    Shortcut("artbook.front", ARTBOOK, "]", "Bring the selection to the front", "]"),
    Shortcut("artbook.back", ARTBOOK, "[", "Send the selection to the back", "["),
    Shortcut("artbook.reset", ARTBOOK, "R", "Reset rotation, size and flips (Right-click > Images > Selection > Reset: one at a time)", "R"),
    Shortcut("artbook.arrange", ARTBOOK, "A", "Auto-arrange the selection (or everything) into neat rows", "A"),
    Shortcut("artbook.arrange_menu", ARTBOOK, "Right-click > Images", "Align, distribute, match size; crop, grayscale, opacity"),
    Shortcut("artbook.crop", ARTBOOK, "C", "Crop the selected image: drag its handles (C again or Enter keeps it)", "C"),
    Shortcut("artbook.crop_apply", ARTBOOK, "Enter", "Crop mode: keep the crop (clicking elsewhere keeps it too); a selected line: type its label", "Return"),
    Shortcut("artbook.crop_drag", ARTBOOK, "Drag a crop handle", "Crop mode: cut an edge or corner away (drag back out to undo it)"),
    Shortcut("artbook.note", ARTBOOK, "T", "Add a text note at the cursor and type in it", "T"),
    Shortcut("artbook.note_edit", ARTBOOK, "Double-click a note", "Type in it (Esc or click elsewhere when done)"),
    Shortcut("artbook.note_bold", ARTBOOK, "Ctrl+B (typing)", "Bold, while typing in a note (otherwise Ctrl+B shows the Pages)", "Ctrl+B"),
    Shortcut("artbook.note_italic", ARTBOOK, "Ctrl+I (typing)", "Italic, while typing in a note", "Ctrl+I"),
    Shortcut("artbook.note_underline", ARTBOOK, "Ctrl+U (typing)", "Underline, while typing in a note", "Ctrl+U"),
    Shortcut("artbook.upscale_compare", ARTBOOK, "Drag the line (Upscale window)", "Compare an image's original with the upscaled picture"),
    Shortcut("artbook.upscale_zoom", ARTBOOK, "Mouse wheel / drag (Upscale window)", "Zoom into the comparison and move around it; double-click to fit"),
    Shortcut("artbook.note_paste_plain", ARTBOOK, "Ctrl+Shift+V (typing)", "Paste as plain text in the note's own style, while typing in a note", "Ctrl+Shift+V"),
    Shortcut("artbook.note_format", ARTBOOK, "The bar above a note", "Text colour, highlight, note colour, bold / italic / underline, size, font, alignment"),
    Shortcut("artbook.edge_handles", ARTBOOK, "Drag a side handle", "Scale from the opposite side; on a note, left / right set its width"),
    Shortcut("artbook.picker", ARTBOOK, "I", "Colour picker: each click copies a colour's hex code (Esc when done)", "I"),
    Shortcut("artbook.custom_color", ARTBOOK, "Note bar colour > Custom...", "Pick any colour and opacity; the note changes as you drag (one undo step). Drag the picker's title to move it, Esc closes it"),
    Shortcut("artbook.color_dropper", ARTBOOK, "Click the dropper, then anywhere", "Custom colour: take it from anywhere on screen (Esc cancels)"),
    Shortcut("artbook.gray", ARTBOOK, "G", "Grayscale on / off for the selected images", "G"),
    Shortcut("artbook.gray_board", ARTBOOK, "Shift+G", "Show the whole board in grayscale, to judge values", "Shift+G"),
    Shortcut("artbook.values_board", ARTBOOK, "Shift+V", "Values Board: the whole board as a few flat greys (2 to 5: Right-click > Canvas)", "Shift+V"),
    Shortcut("artbook.mirror_board", ARTBOOK, "Shift+H", "Mirror Board: the whole board flipped left to right, to check proportions", "Shift+H"),
    Shortcut("artbook.group", ARTBOOK, "Ctrl+G", "Group the selection: it's selected, moved and arranged as one", "Ctrl+G"),
    Shortcut("artbook.ungroup", ARTBOOK, "Ctrl+Shift+G", "Ungroup the selected groups (everything stays put)", "Ctrl+Shift+G"),
    Shortcut("artbook.draw", ARTBOOK, "D", "Draw: freehand strokes on the board (D again or Esc to stop)", "D"),
    Shortcut("artbook.eraser", ARTBOOK, "E", "While drawing: the eraser on / off (drag over strokes to remove them)", "E"),
    Shortcut("artbook.draw_drag", ARTBOOK, "Drag while drawing", "Draw a stroke (with the eraser on: remove the strokes you drag over)"),
    Shortcut("artbook.image_list", ARTBOOK, "L", "Show / hide the list of every image on the page", "L"),
    Shortcut("artbook.group_pick", ARTBOOK, "Alt+Click", "Select one image or note inside a group, on its own"),
    Shortcut("artbook.group_click", ARTBOOK, "Click / drag a group's panel", "Select the whole group from its empty space, and drag to move it"),
    Shortcut("artbook.export_png", ARTBOOK, "Ctrl+E", "Export as a PNG image, with your last export settings", "Ctrl+E"),
    Shortcut("artbook.export_options", ARTBOOK, "Ctrl+Shift+E", "Export Options: PNG, JPG or PDF, what, its size and background", "Ctrl+Shift+E"),
    Shortcut("artbook.copy_image", ARTBOOK, "Ctrl+Shift+C", "Copy the page (or the selection) as an image, to paste in another app", "Ctrl+Shift+C"),
    Shortcut("artbook.export_menu", ARTBOOK, "Click Export (the top bar)", "Export as PNG, JPG or PDF, or copy it, in one click"),
    Shortcut("artbook.delete", ARTBOOK, "Delete", "Remove the selection from the page", "Del"),
    Shortcut("artbook.select_all", ARTBOOK, "Ctrl+A", "Select everything", "Ctrl+A"),
    Shortcut("artbook.deselect", ARTBOOK, "Esc", "Clear the selection (in crop mode: cancel the crop)", "Esc"),
    Shortcut("artbook.undo", ARTBOOK, "Ctrl+Z", "Undo a change to the page", "Ctrl+Z"),
    Shortcut("artbook.redo", ARTBOOK, "Ctrl+Y", "Redo it", "Ctrl+Y"),
    Shortcut("artbook.context_menu", ARTBOOK, "Right-click", "Board actions (paste, flip, order, delete...)"),
    Shortcut("artbook.page_title", ARTBOOK, "Double-click the page's header", "Rename the page (Enter when done); under its name: the line about the page"),
    Shortcut("artbook.duplicate", ARTBOOK, "Ctrl+D", "Duplicate the selection beside it (lines between the cards too)", "Ctrl+D"),
    Shortcut("artbook.send_to_page", ARTBOOK, "Right-click > Move / Copy to Page", "Send the selection to another page (Undo brings it back)"),
    Shortcut("artbook.card_colour", ARTBOOK, "Right-click > Card Colour", "A card's colour: a strip down its edge (a column's outline, a heading's line)"),
    Shortcut("artbook.quick_add", ARTBOOK, "Double-click empty space", "Pick what to put there: a note, a heading, a to-do, a column, a table, a link, pictures"),
    Shortcut("artbook.snap", ARTBOOK, "Drag a card near another", "It snaps into line with it, or to an equal gap (the lines and gaps show)"),
    Shortcut("artbook.snap_off", ARTBOOK, "Hold Alt while dragging", "Drag without snapping"),
    Shortcut("artbook.nudge_left", ARTBOOK, "Left", "Nudge the selection left (nothing selected: move about the board)", "Left"),
    Shortcut("artbook.nudge_right", ARTBOOK, "Right", "Nudge the selection right", "Right"),
    Shortcut("artbook.nudge_up", ARTBOOK, "Up", "Nudge the selection up", "Up"),
    Shortcut("artbook.nudge_down", ARTBOOK, "Down", "Nudge the selection down", "Down"),
    Shortcut("artbook.nudge_left_big", ARTBOOK, "Shift+Left", "Nudge the selection left, ten times as far", "Shift+Left"),
    Shortcut("artbook.nudge_right_big", ARTBOOK, "Shift+Right", "Nudge the selection right, ten times as far", "Shift+Right"),
    Shortcut("artbook.nudge_up_big", ARTBOOK, "Shift+Up", "Nudge the selection up, ten times as far", "Shift+Up"),
    Shortcut("artbook.nudge_down_big", ARTBOOK, "Shift+Down", "Nudge the selection down, ten times as far", "Shift+Down"),

    # Artbook find bar
    Shortcut("artbook.find", FIND, "Ctrl+F", "Find on this page (or every page): matches light up, the rest fades", "Ctrl+F"),
    Shortcut("artbook.find_next", FIND, "Enter (in the find box)", "The next match (All pages: open the result picked in the list)", "Return"),
    Shortcut("artbook.find_previous", FIND, "Shift+Enter (in the find box)", "The previous match", "Shift+Return"),
    Shortcut("artbook.find_result_down", FIND, "Down (in the find box)", "All pages: the next result in the list", "Down"),
    Shortcut("artbook.find_result_up", FIND, "Up (in the find box)", "All pages: the result before it in the list", "Up"),
    Shortcut("artbook.find_result_click", FIND, "Click a result", "All pages: open its page (Back returns) at the match"),
    Shortcut("pages.search_content", PAGES, "Type in Search pages", "Finds pages by name and by what's on them (the match shows on hover)"),
    Shortcut("pages.switch", PAGES, "Ctrl+P", "Go to a page: type a few letters of its name (empty: your recent pages)", "Ctrl+P"),
    Shortcut("pages.switch_open", PAGES, "Enter (in the page switcher)", "Open the page picked (Back returns)", "Return"),
    Shortcut("pages.switch_down", PAGES, "Down (in the page switcher)", "The next page in the list", "Down"),
    Shortcut("pages.switch_up", PAGES, "Up (in the page switcher)", "The page before it in the list", "Up"),

    # Artbook Pages sidebar
    Shortcut("pages.sidebar", PAGES, "Ctrl+B", "Show / hide the Pages sidebar (while you're working in the Artbook)", "Ctrl+B"),
    Shortcut("pages.close_sidebar", PAGES, "Esc / click beside it", "Close the floating Pages sidebar", "Esc"),
    Shortcut("pages.open", PAGES, "Click a page", "Open it (the page you were on is saved first)"),
    Shortcut("pages.rename", PAGES, "F2", "Rename the selected page or folder", "F2"),
    Shortcut("pages.rename_mouse", PAGES, "Click the pencil (or double-click a page)", "Rename a page or folder: the pencil shows on the hovered or selected row"),
    Shortcut("pages.toggle_folder", PAGES, "Click a folder", "Open or close it"),
    Shortcut("pages.drag", PAGES, "Drag a page or folder", "Reorder it, or drop it on a folder to move it inside"),
    Shortcut("pages.delete", PAGES, "Delete", "Send the selected page to the Recycle Bin (asks first)", "Del"),
    Shortcut("pages.context_menu", PAGES, "Right-click", "New page / folder, rename, duplicate, move, delete"),
    Shortcut("pages.sidebar_resize", PAGES, "Drag the sidebar's right edge", "Make the Pages sidebar wider or narrower"),

    # Asset viewer window
    Shortcut("viewer.fullscreen", VIEWER, "F11", "Toggle full screen", "F11"),
    Shortcut("viewer.exit_fullscreen", VIEWER, "Esc", "Leave full screen (closes the viewer when not full screen)", "Esc"),
    Shortcut("viewer.previous", VIEWER, "Page Up", "Previous file in the grid", "PgUp"),
    Shortcut("viewer.next", VIEWER, "Page Down", "Next file in the grid", "PgDown"),
    Shortcut("viewer.previous_alt", VIEWER, "Alt+Left", "Previous file in the grid", "Alt+Left"),
    Shortcut("viewer.next_alt", VIEWER, "Alt+Right", "Next file in the grid", "Alt+Right"),
    Shortcut("viewer.info", VIEWER, "Ctrl+I", "Show / hide the file's details", "Ctrl+I"),
    Shortcut("viewer.open_app", VIEWER, "O", "A file only its app can show: open it there (a PureRef board: in the Artbook)", "O"),

    # PDF viewer (ui.pdf_viewer)
    Shortcut("pdf.previous_page", PDF, "Left", "Previous page", "Left"),
    Shortcut("pdf.next_page", PDF, "Right", "Next page", "Right"),
    Shortcut("pdf.first_page", PDF, "Home", "First page", "Home"),
    Shortcut("pdf.last_page", PDF, "End", "Last page", "End"),
    Shortcut("pdf.zoom_in", PDF, "Ctrl+=", "Zoom in", "Ctrl+="),
    Shortcut("pdf.zoom_in_alt", PDF, "Ctrl++", "Zoom in", "Ctrl++"),
    Shortcut("pdf.zoom_out", PDF, "Ctrl+-", "Zoom out", "Ctrl+-"),
    Shortcut("pdf.actual_size", PDF, "Ctrl+0", "Actual size (100%)", "Ctrl+0"),
    Shortcut("pdf.fit_width", PDF, "W", "Fit the page's width", "W"),
    Shortcut("pdf.fit_page", PDF, "F", "Fit the whole page", "F"),
    Shortcut("pdf.sidebar", PDF, "Ctrl+B", "Show / hide the Contents and Pages sidebar", "Ctrl+B"),
    Shortcut("pdf.scroll", PDF, "Mouse wheel", "Scroll through the pages"),
    Shortcut("pdf.zoom_wheel", PDF, "Ctrl+wheel / pinch", "Zoom toward the pointer"),
    Shortcut("pdf.pan", PDF, "Drag (Hand tool) / middle-drag", "Move the pages around"),
    Shortcut("pdf.space_pan", PDF, "Space+drag", "Move the pages around, whatever tool is on", "Space"),
    Shortcut("pdf.select_tool", PDF, "S", "Select Text tool: drag over text to select it", "S"),
    Shortcut("pdf.escape", PDF, "Esc", "In the find box: clear the search. On the pages: clear the selection, then back to the Hand tool (then Esc closes the viewer)", "Esc"),
    Shortcut("pdf.select_drag", PDF, "Drag (Select Text tool)", "Select text, across pages too"),
    Shortcut("pdf.copy", PDF, "Ctrl+C", "Copy the selected text", "Ctrl+C"),
    Shortcut("pdf.select_page", PDF, "Ctrl+A", "Select all the text on this page", "Ctrl+A"),
    Shortcut("pdf.context_menu", PDF, "Right-click", "Copy, select this page's text"),
    Shortcut("pdf.highlight_tool", PDF, "H", "Highlight tool: drag over text (or press H with text selected)", "H"),
    Shortcut("pdf.pen_tool", PDF, "D", "Pen tool: draw on the page", "D"),
    Shortcut("pdf.note_tool", PDF, "T", "Note tool: click on the page and type", "T"),
    Shortcut("pdf.eraser_tool", PDF, "E", "Eraser: drag over highlights, drawings and notes to remove them", "E"),
    Shortcut("pdf.undo", PDF, "Ctrl+Z", "Undo a change to your marks", "Ctrl+Z"),
    Shortcut("pdf.redo", PDF, "Ctrl+Y", "Redo", "Ctrl+Y"),
    Shortcut("pdf.redo_alt", PDF, "Ctrl+Shift+Z", "Redo", "Ctrl+Shift+Z"),
    Shortcut("pdf.delete_mark", PDF, "Delete", "Delete the picked highlight, drawing or note", "Del"),
    Shortcut("pdf.pick_mark", PDF, "Click a mark (Hand tool)", "Pick a highlight, drawing or note; drag a drawing or note to move it"),
    Shortcut("pdf.edit_note", PDF, "Double-click a note", "Type in it (Esc or clicking elsewhere finishes)"),
    Shortcut("pdf.find", PDF, "Ctrl+F", "Find in the PDF", "Ctrl+F"),
    Shortcut("pdf.find_next", PDF, "F3", "Next match", "F3"),
    Shortcut("pdf.find_previous", PDF, "Shift+F3", "Previous match", "Shift+F3"),
    Shortcut("pdf.find_box_next", PDF, "Enter (in the find box)", "Next match", "Return"),
    Shortcut("pdf.find_box_next_alt", PDF, "Enter (number pad, in the find box)", "Next match", "Enter"),
    Shortcut("pdf.find_box_previous", PDF, "Shift+Enter (in the find box)", "Previous match", "Shift+Return"),
    Shortcut("pdf.link", PDF, "Click a link", "Follow it: a page in the PDF, or a web address in your browser"),
    Shortcut("pdf.go_to_page", PDF, "Type in the page box", "Go to that page (Enter)"),
    Shortcut("pdf.contents", PDF, "Click in Contents / Pages", "Go to that chapter or page"),

    # 3D model viewport
    Shortcut("viewer3d.orbit", MODEL, "Left-drag", "Turn the model (the lights or HDRI, when Turn Lights is on)"),
    Shortcut("viewer3d.light_drag", MODEL, "Shift+Left-drag", "Turn the lights round the model (an HDRI: round the vertical)"),
    Shortcut("viewer3d.pan", MODEL, "Right-drag / Middle-drag", "Pan the camera"),
    Shortcut("viewer3d.zoom", MODEL, "Mouse wheel", "Zoom"),
    Shortcut("material.reset_slider", MODEL, "Double-click a slider (material preview)", "Put an Adjust slider back where it started"),
    Shortcut("viewer3d.wireframe", MODEL, "W", "Cycle shaded / wireframe / both", "W"),
    Shortcut("viewer3d.grid", MODEL, "G", "Toggle the ground grid", "G"),
    Shortcut("viewer3d.reset", MODEL, "R", "Reset the camera", "R"),
    Shortcut("viewer3d.drag_lights", MODEL, "L", "Turn Lights on / off: whether dragging turns the lights or the model", "L"),
    Shortcut("viewer3d.render", MODEL, "F12", "Render what's in the viewer, with real light (LuxCore) or as it's shown", "F12"),
    Shortcut("viewer3d.uv", MODEL, "U", "See the model's UV layout, or back to the model", "U"),
    Shortcut("viewer3d.uv_pan", MODEL, "Drag (UV layout)", "Move the layout around"),
    Shortcut("viewer3d.uv_zoom", MODEL, "Mouse wheel (UV layout)", "Zoom toward the pointer (R fits it to the window)"),
    Shortcut("viewer3d.backdrop", MODEL, "B", "Under an HDRI: show it behind the model, or not", "B"),
    Shortcut("viewer3d.hdri_pick", MODEL, "Right-click an HDR > Use as Viewer Lighting", "Light models with that HDRI (also: the Lighting list)"),

    # Image / texture viewer
    Shortcut("image.zoom", IMAGE, "Mouse wheel", "Zoom toward the cursor"),
    Shortcut("image.tile", IMAGE, "T", "Tile: the image repeated 2 x 2 or 3 x 3, to check its seams", "T"),
    Shortcut("image.view_3d", IMAGE, "3", "An alpha as a 3D surface, or back to the flat image", "3"),
    Shortcut("image.invert", IMAGE, "I", "3D view of an alpha: invert its heights (alphas made the other way round)", "I"),
    Shortcut("image.pan", IMAGE, "Left-drag / Middle-drag", "Pan the image"),

    # Video player
    # Courses (the Course View window)
    Shortcut("course.note", COURSE, "N", "A note at this moment of the lesson (also on the course's Artbook page)", "N"),
    Shortcut("course.cut", COURSE, "K", "Editing, a long video: cut it into lessons here", "K"),
    Shortcut("course.note_cancel", COURSE, "Esc (typing a note)", "Drop the note (playing carries on)", "Esc"),
    Shortcut("course.remove", COURSE, "Delete", "Editing: take the selected lesson out of the course (or the cut after it, in the cuts bar)", "Delete"),
    Shortcut("course.lesson_click", COURSE, "Click a lesson", "Play it (where you were in it)"),
    Shortcut("course.tick", COURSE, "Click a lesson's box", "Mark it done or not done (it's ticked by itself at 90% watched)"),
    Shortcut("course.note_click", COURSE, "Click a note", "Jump back to its moment"),
    Shortcut("course.file_open", COURSE, "Double-click a lesson file", "Open it in its app (Maya first), a folder in Explorer"),
    Shortcut("course.file_drag", COURSE, "Drag a lesson file out", "Onto the Artbook (a picture), or into another app"),
    Shortcut("course.lesson_drag", COURSE, "Editing: drag a lesson", "Put it somewhere else in the course (under a chapter's name: in that chapter)"),
    Shortcut("course.lesson_rename", COURSE, "Editing: double-click a lesson or chapter", "Rename it (Enter when done)"),
    Shortcut("course.file_drop", COURSE, "Editing: drop files on a lesson", "They become its files (videos dropped elsewhere: new lessons)"),
    Shortcut("course.link_drop", COURSE, "Editing: drag a link from your browser onto the lessons", "A web lesson: YouTube plays here, a video file's address in the player, anything else in your browser"),
    Shortcut("course.cut_drag", COURSE, "Editing: drag a cut", "Move where one part ends and the next begins"),
    Shortcut("course.part_rename", COURSE, "Editing: double-click a part", "Name that lesson"),

    # KeyDial, the KD100's own app (ui/keydial_*.py): every control is the mouse, or the KD100 itself
    Shortcut("keydial.pick", KEYDIAL, "Click a key or the dial", "Pick it in the KD100 picture, to set what it does"),
    Shortcut("keydial.press_pick", KEYDIAL, "Press a key on the KD100", "With KeyDial's window in front: pick that key (it sends nothing then)"),
    Shortcut("keydial.profile", KEYDIAL, "Click empty space by the KD100", "The profile's own settings: its programs, and what keys it doesn't set do"),
    Shortcut("keydial.capture", KEYDIAL, "Click a shortcut box, then press keys", "Set the shortcut (modifiers alone, like Ctrl, for Hold Keys)"),
    Shortcut("keydial.record", KEYDIAL, "Macro: Record, then type", "Each shortcut becomes a step, pauses become waits; click Stop when done"),
    Shortcut("keydial.reorder", KEYDIAL, "Drag a macro step or a dial mode", "Put it in a new place in the order"),
    Shortcut("keydial.profile_menu", KEYDIAL, "Right-click a profile", "Duplicate or delete it"),
    Shortcut("keydial.tray", KEYDIAL, "Click the tray icon", "Open KeyDial (right-click: pause, Auto / Manual mode, settings, quit)"),

    Shortcut("video.play_pause", VIDEO, "Space", "Play / pause", "Space"),
    Shortcut("video.back", VIDEO, "Left arrow", "Jump back by the jump time", "Left"),
    Shortcut("video.forward", VIDEO, "Right arrow", "Jump forward by the jump time", "Right"),
    Shortcut("video.mute", VIDEO, "M", "Mute / unmute (or click the speaker icon)", "M"),
    Shortcut("video.frame_back", VIDEO, ",", "Pause and step back one frame", ","),
    Shortcut("video.frame_forward", VIDEO, ".", "Pause and step forward one frame", "."),
    Shortcut("video.loop", VIDEO, "L", "A-B loop: set the start, then the end, then turn it off", "L"),
    Shortcut("video.restart", VIDEO, "Home", "Back to the start (videos otherwise resume where you left them)", "Home"),
    Shortcut("video.speed", VIDEO, "Click the speed button", "Pick a preset speed, or drag the slider (arrow keys: 0.05× steps)"),
    Shortcut("video.fullscreen", VIDEO, "F", "Toggle video full screen", "F"),
    Shortcut("video.fullscreen_mouse", VIDEO, "Double-click video", "Toggle video full screen"),
    Shortcut("video.exit_fullscreen", VIDEO, "Esc", "Leave video full screen", "Esc"),
    Shortcut("video.seek", VIDEO, "Click / drag seek bar", "Jump to a point / scrub (hover shows a preview)"),
    Shortcut("video.frame_menu", VIDEO, "Right-click the video", "The frame showing: Send Frame to Artbook, Copy Frame, Save Frame As PNG"),

    # Tag dialog
    Shortcut("tagdialog.create", TAG_DIALOG, "Enter (in the new tag box)", "Create the tag and tick it"),  # QLineEdit.returnPressed

    # Theme Studio (AssetManager.exe --theme-studio: ui.theme_studio)
    Shortcut("studio.new", THEME_STUDIO, "Ctrl+N", "Start a new theme from a built-in one", "Ctrl+N"),
    Shortcut("studio.open", THEME_STUDIO, "Ctrl+O", "Open a theme file (.amtheme)", "Ctrl+O"),
    Shortcut("studio.save", THEME_STUDIO, "Ctrl+S", "Save the theme file", "Ctrl+S"),
    Shortcut("studio.save_as", THEME_STUDIO, "Ctrl+Shift+S", "Save the theme as a new file", "Ctrl+Shift+S"),
    Shortcut("studio.pick_color", THEME_STUDIO, "Click a swatch", "Pick that colour (with see-through)"),
    Shortcut("studio.type_color", THEME_STUDIO, "Type in a colour box", "#rrggbb, #aarrggbb, rgba(r, g, b, a) or transparent"),
)

_BY_ID = {s.id: s for s in SHORTCUTS}
assert len(_BY_ID) == len(SHORTCUTS), "duplicate shortcut id"


def get(shortcut_id: str) -> Shortcut:
    return _BY_ID[shortcut_id]


def sequence(shortcut_id: str) -> QKeySequence:
    """Key sequence to bind with setShortcut()."""
    seq = _BY_ID[shortcut_id].sequence
    if seq is None:
        raise ValueError(f"{shortcut_id} is a mouse gesture / typed text, not a key sequence")
    return QKeySequence(seq)


def matches(shortcut_id: str, event: QKeyEvent) -> bool:
    """True if a key press is this shortcut (keypad / number-pad variants count too)."""
    combo = event.keyCombination()
    modifiers = combo.keyboardModifiers() & ~Qt.KeyboardModifier.KeypadModifier
    pressed = QKeySequence(QKeyCombination(modifiers, combo.key()))
    return pressed.matches(sequence(shortcut_id)) == QKeySequence.SequenceMatch.ExactMatch


def by_category() -> list[tuple[str, list[Shortcut]]]:
    return [(cat, [s for s in SHORTCUTS if s.category == cat]) for cat in CATEGORIES]
