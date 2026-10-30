# Asset-Manager
File manging system for organizing and managing all assest

## Installing

Each release has two downloads:
- **AssetManager-Setup.exe** (recommended): installs for your user account only (no administrator prompt)
  and starts much faster, because nothing has to be unpacked at launch. Updates from **Settings > About**
  run the new installer silently and restart the app.
- **AssetManager.exe**: the portable single file. Put it anywhere and run it. It updates by swapping itself.

Settings (`~\.3d_asset_manager_config.json`) and the library (`%LOCALAPPDATA%\AssetManager`) are shared by both
and are never touched by installing, updating or uninstalling.

## The library

- **Live updates:** files added, removed, renamed or edited in your folders show up within a couple of
  seconds while the app is open. Only the changed folders are rescanned. (Up to 4,000 folders are watched;
  in bigger libraries, Refresh picks up the rest.)
- **Instant start:** the last scan is remembered, so the grid appears straight away on launch while a fresh
  scan runs in the background.
- **Big libraries:** the grid only does work for the cards on screen, and thumbnails are cached on disk
  (`%LOCALAPPDATA%\AssetManager\thumbnails`, capped at 512 MB), so 50,000 files scroll as smoothly as 50.
- **Drag files out** of the grid into Explorer, Blender, Photoshop, Unreal and so on. They're copied; the
  library keeps its files.
- **Favorites and ratings:** select files and press **F** to star them, or **1** to **5** to rate them
  (**0** clears). The sidebar has a Favorites row.
- **Find duplicates** (**Tools > Find Duplicates...**) compares file contents, groups identical files, and moves
  the copies you tick to the Recycle Bin. One copy of every file is always kept.
- **Undo:** **Ctrl+Z** / **Ctrl+Y** undo and redo tag, favorite and rating changes.
- **Viewer:** **Page Up** / **Page Down** (or Alt+Left / Alt+Right, or the ‹ › buttons) step through the files
  shown in the grid without closing the viewer.
- **Sidebar:** the menu button (or **Ctrl+B**) slides it in and out, and dragging its right edge resizes it. With the pin button it stays
  docked beside the grid. Unpinned, it floats over the grid on frosted glass and slides away after you pick a
  category (or press Esc, or click beside it).
- **Looks:** **Settings > Appearance** has the themes and a background texture (grain, dots, grid, lines,
  hexagons, carbon fiber, topographic) and a Glass Frost slider for the sidebar. Icons are two-tone and take
  their colors from the theme. Lists scroll smoothly, and categories slide open.

## The Artbook

The Artbook holds reference boards, called **Pages**: endless canvases you fill with images from the library,
Explorer, a web browser or the clipboard.

- **Views:** the **View** button (top right) switches between Asset View and Artbook View. Its arrow, or
  **Ctrl+1** to **Ctrl+4**, also offers Split View (both side by side; drag the divider) and Separate View (the
  Artbook in its own window). The app reopens in the view you left it in.
- **Getting images in:** drop files or images from a browser (Google Images results too), drag them in from
  the asset grid, or paste with **Ctrl+V** (image data, files or an image link). In the asset grid, right-click
  > **Send to Artbook** adds the selected images to any page (listed in its folders), or to a new page.
- **Working over other apps (Separate View):** the **Window** button in the Artbook window (also under
  **Window** in the board's right-click menu) has:
  - **Always on Top**, to keep your references over Blender, Photoshop and so on.
  - **Opacity**, to see through the whole window (20-100%).
  - **Click-through**: the window ignores the mouse, so clicks go to the app underneath, and it stays on top.
    Press **Ctrl+Alt+T** (from any app) or click the tray icon to turn it off. A short heads-up explains this
    when you turn it on (tick "Don't show again" to skip it).

  Always on Top and Opacity are remembered. Click-through always starts off, and leaving Separate View turns it off.
- **On the board:** drag to move, drag a corner or side handle to scale (the opposite one stays put), the round
  handle to rotate (Shift snaps to 15°).
  **H** / **V** flip, **]** / **[** bring to front / send to back, **R** resets, **Delete** removes, **Ctrl+Z** /
  **Ctrl+Y** undo and redo. The wheel zooms toward the cursor, middle-drag (or Space+drag) pans, **F** frames
  everything, double-click zooms to an image.
- **Right-click menu:** Undo / Redo, Copy / Paste, then **Canvas** (frame, 100%, grayscale board) and **Images**
  (Select All, and the Selection, Arrange, Align and Match Size submenus). Greyed-out items need a selection.
- **Adjusting images:** **C** crops the selected image: drag the handles inward (or back out), Enter or C keeps
  it, Esc cancels. The whole image is kept, so Right-click > Images > Selection > **Reset** > Crop brings it all
  back. The same Reset submenu undoes rotation, size, flips or crop one at a time (**R** resets all but the crop). **G** turns grayscale on / off for the selected images, the same menu has an **Opacity** slider, and
  **Shift+G** shows the whole board in grayscale to judge values (remembered per page; page thumbnails stay in
  colour). Selection also has **Pixelated** (hard pixel edges when zoomed in, for pixel art and textures) and
  **Caption...** (a line of text under the image that moves, scales and flips with it).
- **Groups:** **Ctrl+G** groups the selection: a soft panel shows behind it, and clicking any member (or the
  panel's empty space) picks up the whole group, so it moves, arranges and aligns as one block. **Alt+click**
  picks one member on its own, **Ctrl+Shift+G** ungroups. A selected group gets a small bar above it:
  - **Colour:** the theme's, white, black, or **Custom...** (the same live colour picker as notes, with opacity).
  - **Lock:** the group can't be moved, resized, turned, flipped, cropped or deleted until it's unlocked.
  Both are also in right-click > Images > Selection. Align > **Stack** piles the selection up like a hand of cards.
- **Pages from the board:** right-click > **Pages** makes a new page, duplicates or renames this one, or exports it.
- **Image list:** **L** (or the **Images** button) lists every image on the page with a small picture, its name,
  size, and whether it's linked to a file, kept in the page, or missing its file. Click one to select and frame it;
  search by name or path. With files missing, **Relink...** finds them.
- **Files:** right-click > Copy > **File Path**, and Images > File > **Show in Explorer**, **Open Source File**,
  **Replace Image...** (a new picture that keeps the old one's place, size on the board, crop and adjustments) and
  **Relink Missing Images...** (pick the folder the files are in now: they're found by name, in its folders too).
- **Draw:** **D** draws freehand strokes, with a bar at the top for the pen's colour (the theme's, white, black or
  custom), its size and the **eraser** (**E**: drag over strokes to remove them). D again, Esc or Done stops.
  Strokes move, scale, rotate, group and undo like everything else, and follow the theme in its colour.
- **Notes:** **T** adds a note at the cursor and you type straight away; Esc or clicking elsewhere finishes,
  and double-clicking a note edits it. Notes take the theme's colours. Select one and a format bar appears
  above it: text colour, highlight, note colour, bold / italic / underline (Ctrl+B / I / U while typing), size,
  font, and alignment / clear formatting under the chevron. Each colour offers Theme Default, White, Black or
  **Custom...**: a floating picker (opens beside the note; drag its title to move it) with a colour square,
  hue and opacity sliders, a screen dropper, HEX / RGB / HSL text and ready-made colours. The note changes as
  you pick, and the whole pick is one Ctrl+Z. Highlights have rounded corners, like the notes. With text selected while typing, the bar changes
  that text; otherwise the whole note. Notes move, scale, rotate, arrange and align like images; drag a note's
  left / right handle to make it wider or narrower. An emptied note is removed; Ctrl+Z brings it back.
- **Colour picker:** **I**, then click anywhere on the board to copy that colour's hex code (e.g. `#F59E0B`).
  It stays on for more picks; Esc, I again or a right-click turns it off.
- **Keys shared with other windows** (G is also the 3D viewer's grid) act on the window you're working in.
- **Tidying up:** **A** auto-arranges the selection (or everything) into neat rows, keeping each image's size and
  their reading order. Right-click > **Images** also has Align (left, centers, right, top, middles, bottom,
  and distribute with equal gaps) and Match Size (same height / width). Each is one Ctrl+Z.
- **Pages sidebar:** the menu button at the top left of the Artbook (or **Ctrl+B** while you're working in it)
  slides out your pages, each with a small picture of its board. Organize them in folders, which can hold folders.
  - Click a page to open it. The page you were on is saved first.
  - The buttons at the top make a new page or folder in the selected folder. The pencil on the hovered or selected row (or **F2**) renames.
  - Drag pages and folders to reorder them, or drop one on a folder to move it inside.
  - Right-click to duplicate, move or delete. Deleted pages go to the Recycle Bin. Deleting a folder keeps
    its pages: they move up a level.
  - The search box filters pages by name, or by folder name.
  - **Share a page:** right-click a page > **Export Page...** (or right-click the board > Canvas > Export Page)
    saves it as one `.artbook` file with its images inside (linked files go in whole), ready to send.
  - **Import:** the import button at the top (or right-click > Import Pages..., or drop the files on the board)
    opens `.artbook` files and **PureRef boards** (`.pur`) as new pages. PureRef boards keep their layout:
    positions, sizes, rotation, flips, crops, opacity, notes, image names and groups (with their colour). Pick several at
    once. Drawings are left out for now. Boards from PureRef 1.x need
    opening and saving in PureRef 2 first.
- **Export as PNG:** **Ctrl+E** (or right-click > Canvas) saves the selection, or the whole board, as one picture on
  the board's colour, as sharp as its images allow (up to 16384 pixels on the longest side).
  - Like the library's sidebar, it can be pinned beside the board or float over it on frosted glass.
- **Saving:** pages save themselves a couple of seconds after each change, and when you switch page or quit.
  They're stored in `%LOCALAPPDATA%\AssetManager\artbook`, one folder per page. Library images are *linked*
  (the page keeps a small copy in case the file moves); pasted and web images are stored in the page.

## Video playback

Videos (`.mp4, .mov, .mkv, .avi, .webm, .m4v`) play inside the asset viewer using Qt Multimedia.
No extra Python packages are needed.

- **Playback speed:** the speed button opens presets from 0.25× (lowest) to 3.0× (highest), with a slider
  above them for anything in between, in 0.05× steps. The speed is remembered.
- **Mute:** click the speaker icon or press **M**. Moving the volume slider up unmutes too.
- **Resume:** videos start where you left them. **Home** starts from the beginning.
- **Frame step:** **,** and **.** pause and step one frame back or forward.
- **A-B loop:** press **L** (or the A-B button) at the start and at the end of the part to repeat; press it again to stop.
- **ffmpeg** is optional. If you install it (for example `winget install ffmpeg`), the app uses it to:
  - make poster-frame thumbnails in the grid
  - make 720p H.264 preview copies ("proxies") of files that won't play or that stutter
  - read video lengths for `dur:` searches

  The app finds ffmpeg on the PATH, or you can point it at `ffmpeg.exe` in **Settings > Video**.
- **The cache** for proxies and thumbnails is in `%LOCALAPPDATA%\AssetManager`. You can clear it in **Settings > Video**.
- **Playback backend:** the default is FFmpeg. If videos play incorrectly, switch to Windows Media Foundation in
  **Settings > Video** and restart the app. Setting `QT_MEDIA_BACKEND=windows` (or `ffmpeg`) before launch also works.
- **Testing the player on its own:** `python -m ui.video_player "path\to\clip.mp4"`

Known limitations:
- Seeking (and so frame stepping) snaps to keyframes on long-GOP footage, so it isn't always frame-accurate.
- The upgrade path, if frame-accurate scrubbing or wider pro-codec support is ever needed, is python-mpv (libmpv).

## Tags and search

Tags let you find groups of files without relying on file names. They're stored in a SQLite database at
`%LOCALAPPDATA%\AssetManager\library.db`.

- **File type tags** (PNG, FBX, JPEG, ...) are added automatically to every scanned file and can't be edited.
  The names come from the extension map in `core/scanner.py` (`determine_category_name`).
- **Custom tags:** create, rename, recolor and delete them in **Settings > Tags** (Cancel undoes the changes made
  there). To tag files, select one or many in the grid and press **Ctrl+T**, click **Tag**, or right-click and
  choose **Tag...**. The dialog's checkboxes show whether all, some or none of the selected files have each tag.
- **Moved files keep their tags:** files with tags, a favorite or a rating are fingerprinted, so when one is moved
  or renamed outside the app, the next scan finds it again and keeps everything.
- **Searching** (hover over the search box for a reminder):
  - `tag:name` (or `tag:"two words"`) then Enter or Space adds a tag chip; every chip must match
  - `-tag:name` files without the tag; `tag:a|b` files with any of them; click a chip to switch has / doesn't have
  - `is:fav`, `rating:>=3`, `ext:png,jpg`, `size:>10mb`
  - `res:>=2048` (image or video resolution, `res:4k` = 4096), `dur:<30s`, `poly:<10k` (3D model faces). These
    details are read in the background after a scan; files not read yet don't match.
  - plus any file name text. Backspace removes the last chip; Esc or ✕ clears everything.
- **Collections:** click the star in the search box to save the search in the sidebar. Right-click one to rename or delete it.
- **Backups:** the database is backed up automatically before upgrades and app updates (the last 10 are kept).
  **Tools** also has Back Up Library Now, Restore Library Backup..., and Export / Import Tags (a `.json` file with
  your tags, favorites, ratings and collections, for moving to another PC).

Known limitations:
- A file that's moved *and* edited before the next scan, or that has an identical copy, can't be matched and starts
  untagged (rather than risk tagging the wrong file).
- Database entries for deleted files are kept (they might be on a drive that's just offline).
- 3D model face counts are read for files up to 60 MB; bigger ones don't match `poly:` searches.

## When something goes wrong

Crashes, freezes (the window not responding for 20 seconds) and errors are written to
`%LOCALAPPDATA%\AssetManager\crash.log`. After a crash the app says so on the next launch, and
**Settings > About > Open Crash Log** opens the log. Please attach it to bug reports.

## Shortcuts

**Settings > General > Shortcuts** lists every keyboard shortcut and mouse control, grouped by where it works.
That page is generated from `ui/shortcuts.py`, the one place shortcuts are defined. Code binds keys by id from
the registry, and `tests/test_shortcuts.py` fails if a hard-coded key appears anywhere else, so new shortcuts
always show up on the page. **Settings > Appearance > Animations** turns animations off (or follows Windows).

## Development

`pip install -r requirements-dev.txt`, then `python -m pytest`. The project rules (shortcuts, threads, tooltips)
are in `CLAUDE.md`. Release builds: `pyinstaller AssetManager.spec` makes both the portable exe and the folder
build; `iscc /DAppVersion=x.y.z installer\AssetManager.iss` turns the folder build into the installer.
