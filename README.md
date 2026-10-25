# Asset-Manager
File manging system for organizing and managing all assest

## Video playback

Videos (`.mp4, .mov, .mkv, .avi, .webm, .m4v`) play inside the asset viewer using Qt Multimedia.
No extra Python packages are needed.

- **ffmpeg** is optional. If you install it (for example `winget install ffmpeg`), the app uses it to:
  - make poster-frame thumbnails in the grid
  - make 720p H.264 preview copies ("proxies") of files that won't play or that stutter

  The app finds ffmpeg on the PATH, or you can point it at `ffmpeg.exe` in **Settings > Video**.
- **The cache** for proxies and thumbnails is in `%LOCALAPPDATA%\AssetManager`. You can clear it in **Settings > Video**.
- **Playback backend:** the default is FFmpeg. If videos play incorrectly, switch to Windows Media Foundation in
  **Settings > Video** and restart the app. Setting `QT_MEDIA_BACKEND=windows` (or `ffmpeg`) before launch also works.
- **Testing the player on its own:** `python -m ui.video_player "path\to\clip.mp4"`

Known limitations:
- Seeking snaps to keyframes on long-GOP footage, so it isn't frame-accurate.
- There's no frame-step control. Qt Multimedia doesn't provide one.
- The upgrade path, if frame-accurate scrubbing or wider pro-codec support is ever needed, is python-mpv (libmpv).

## Tags

Tags let you find groups of files without relying on file names. They're stored in a SQLite database at
`%LOCALAPPDATA%\AssetManager\library.db`.

- **File type tags** (PNG, FBX, JPEG, ...) are added automatically to every scanned file and can't be edited.
  The names come from the extension map in `core/scanner.py` (`determine_category_name`).
- **Custom tags:** create, rename, recolor and delete them in **Settings > Tags**. To tag files, select one or many
  in the grid and press **Ctrl+T**, click **Tag**, or right-click and choose **Tag...**. The dialog's checkboxes show
  whether all, some or none of the selected files have each tag. Rows you don't touch are left as they are.
- **Searching:** type `tag:name` (or `tag:"two words"`) and press Enter or Space to add a tag filter chip, or pick a
  tag from the suggestions shown after `tag:`. A file has to match every chip, plus any file name text you type.
  Backspace removes the last chip; Esc or the ✕ button clears everything.

Known limitations:
- Tags are linked to file paths, so moving or renaming a file outside the app loses its tags. There's no relink yet.
- Database entries for deleted files are kept (they might be on a drive that's just offline).
- Tags can only be combined with AND. OR search and tag hierarchies aren't supported.

Developer tests: `pip install -r requirements-dev.txt`, then `python -m pytest`.

## Shortcuts

**Settings > General > Shortcuts** lists every keyboard shortcut and mouse control, grouped by where it works.
That page is generated from `ui/shortcuts.py`, the one place shortcuts are defined. Code binds keys by id from
the registry, and `tests/test_shortcuts.py` fails if a hard-coded key appears anywhere else, so new shortcuts
always show up on the page. See `CLAUDE.md` for the rule.
