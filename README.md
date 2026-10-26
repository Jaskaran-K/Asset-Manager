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
- **Collections:** click ☆ in the search box to save the search in the sidebar. Right-click one to rename or delete it.
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
