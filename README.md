# Asset-Manager
File manging system for organizing and managing all assest

## Installing

Each release has two downloads:
- **AssetManager-Setup.exe** (recommended): installs for your user account only (no administrator prompt)
  and starts much faster, because nothing has to be unpacked at launch. Updates from **Settings > About**
  run the new installer silently and restart the app.
- **AssetManager.exe**: the portable single file. Put it anywhere and run it. It updates by swapping itself.

Settings (`~\.3d_asset_manager_config.json`) and the library (`%LOCALAPPDATA%\AssetManager`) are shared by both
and are never touched by installing, updating or uninstalling. Settings are saved so a crash can't leave the file
half written, and the copy before the latest save is kept next to it (`.json.bak`): if the file is ever damaged,
the app starts from that copy and notes it in the crash log.

## The library

- **Live updates:** files added, removed, renamed or edited in your folders show up within a couple of
  seconds while the app is open. Only the changed folders are rescanned. (Up to 4,000 folders are watched;
  in bigger libraries, Refresh picks up the rest.)
- **Instant start:** the last scan is remembered, so the grid appears straight away on launch while a fresh
  scan runs in the background.
- **Big libraries:** the grid only does work for the cards on screen, and thumbnails are cached on disk
  (`%LOCALAPPDATA%\AssetManager\thumbnails`, capped at 512 MB), so 50,000 files scroll as smoothly as 50.
  After a fast scroll, the cards you land on are made first, and cards you scrolled past are skipped. Only the
  most recently seen thumbnails stay in memory (150 MB); the rest reload from the disk cache. The cache keeps
  pictures without their card, so changing the theme doesn't read every file again.
- **HDR images:** `.exr` (with the OpenEXR package) and `.hdr` files get thumbnails and open in the viewer. HDRIs
  are tone mapped so bright skies keep their detail, and one-channel height maps are stretched to their range.
  Files that can't be shown say so on their card ("No preview", or "Too large to preview" for images over
  about 180 megapixels).
- **Alphas** (grey height images: ZBrush alphas, displacement and height maps) are recognised by themselves:
  any grey image whose name doesn't say it's another kind of grey map (AO, gloss, roughness, metal, mask...).
  - In the grid they're drawn as a **lit relief**, clay grey and lit from the top left, as ZBrush shows them,
    from their full 16-bit heights (8 bits would show steps on gentle slopes). **Settings > Appearance >
    Alpha thumbnails** switches between **Relief** and **Normal** (the flat grey image).
  - `is:alpha` in the search finds them (`-is:alpha`: everything else), including alphas inside archives.
  - In the viewer, **3D** (or **3**) shows an alpha as a surface with the 3D viewer's materials and lights:
    **Height** slider, **Invert** (**I**) for alphas made the other way round, and **512 px** / **1024 px**
    detail.
  - **Tile** (or **T**) shows any image repeated **2×2** or **3×3**, edge to edge, to check its seams.
- **ZBrush and other app files** are in the library too: ZBrush brushes, tools, projects and materials
  (`.zbp`, `.ztl`, `.zpr`, `.zmt`), Substance files (`.spp`, `.sbsar`, `.spsm`), Maya scenes (`.ma`, `.mb`),
  3ds Max (`.max`) and Toolbag scenes (`.tbscene`), under **ZBrush** and **App Files** in the sidebar.
  - Their cards show a picture when there's a sure one: a `.blend`'s own render, or a picture beside the file
    named after it (`Carve_alpha.psd`, `Carve_ico.png`) or like a preview (`Preview.jpg`, `cover.png`). Otherwise
    they show **the icon of the app they open in**, read from the app on your computer. An app file whose app
    isn't installed shows a plain sheet.
  - **Double-click** shows the file's picture and details, with a button (or **O**) that opens it in its app.
- **Open In** (right-click) opens a file in ZBrush, Blender, Maya, Photoshop, Substance 3D Painter or Marmoset
  Toolbag, whichever fit its type. OBJ and FBX are imported into Blender and Maya, and an OBJ loads into a ZBrush
  that's already open. A file inside an archive is unpacked first; that's a temporary copy, so save it somewhere
  else to keep changes. **Open with External App** still uses the program Windows has for the file.
  - **Settings > Apps** lists the apps. They're found by themselves, wherever they're installed, and the
    newest version is used unless you pick another there or browse to one. **File Types...** changes which
    types Open In offers an app for.
  - **Add App...** adds any other program, such as Krita: pick it, name it and give it its file types. It's
    started with the file, so it has to accept one when it starts. A type the library doesn't list yet (`.kra`)
    is added to it, and those files show the app's icon.
  - **Into the window you have open:** Maya and Blender start a new window each time unless you press **Install
    Helper** in Settings > Apps. It adds a small script to the app's own scripts folder (Maya: `userSetup.py`
    gets a few marked lines, with whatever else is there left alone; Blender: its `scripts/startup` folder, for
    each version), and from the next time you start the app, OBJ and FBX go into the window that's open. Scenes
    (`.ma`, `.mb`, `.blend`) still open in a new window, so nothing replaces your work. The helper only reads
    requests from the app's own folder and only ever imports model files. **Remove Helper** takes it all out.
- **PureRef boards** (`.pur`) show the picture they hold of themselves. **Double-click** opens a board as an
  Artbook page, the same page every time: one already named like it is used, or it's imported. A board changed
  since says so; right-click > **Import Again** makes a new page. Pages you've edited are never overwritten, and
  boards are only ever read.
- **Drag files out** of the grid into Explorer, Blender, Photoshop, Unreal and so on. They're copied; the
  library keeps its files.
- **Archives (ZIP, RAR, 7z)** are browsed without unpacking them:
  - Each archive is a card showing the best picture inside (one named like *preview*, *cover* or *thumb*
    first), or an archive box with what's inside ("MP4, SRT"). Its badge says how many files it holds
    ("ZIP · 124"), and its tooltip lists them by type.
  - **Double-click** an archive (or press Space) to look inside it like a folder. **Back** or **Backspace**
    returns to where you were, with the archive selected. Search and the sidebar work inside it.
  - **RAR and 7z need 7-Zip** (free, [7-zip.org](https://www.7-zip.org), or `winget install 7zip.7zip`). It's
    found by itself (Program Files, PATH, Scoop), or set its place in **Settings > Archives**. Without it,
    their cards say "Install 7-Zip to look inside". ZIPs never need it.
  - A multi-part RAR (`name.part1.rar`, `name.part2.rar`, ...) is one card, with the size of all its parts.
  - The files inside also show among the rest of your library, marked with a small box, so search finds
    them. **Include Files Inside Archives**, in the Library button's menu (its arrow) or in
    **Settings > Archives**, turns that off; they're then only shown inside an opened archive.
  - Images inside get real thumbnails, read straight from the zip, and their resolution is searchable
    (`res:`). Tags, favourites and ratings work on them like on any file.
  - Opening a model, PDF, video or anything over 100 MB unpacks it first, with progress and a Cancel button.
    Unpacked copies are kept (`%LOCALAPPDATA%\AssetManager\archive-files`, up to 5 GB unless you change it in **Settings > Storage**; the oldest go first),
    so opening it again is instant and its thumbnail can be made. A course video plays like any other once
    unpacked.
  - **Right-click > Extract To...** unpacks a whole archive into a folder named after it. **Extract...** saves
    one file. Nothing is replaced without asking, and names that could escape the folder are skipped.
  - Dragging a file out of an archive unpacks it first (up to 50 MB; bigger ones go through Extract).
  - Zips packed with Deflate64, which many zip tools can't read, are read too. Password-protected files say
    so. Archives inside archives are listed, but must be extracted to be opened.
- **Favorites and ratings:** select files and press **F** to star them, or **1** to **5** to rate them
  (**0** clears). The sidebar has a Favorites row.
- **Find duplicates** (**Tools > Find Duplicates...**) compares file contents, groups identical files, and moves
  the copies you tick to the Recycle Bin. One copy of every file is always kept.
- **Undo:** **Ctrl+Z** / **Ctrl+Y** undo and redo tag, favorite and rating changes.
- **Viewer:** **Page Up** / **Page Down** (or Alt+Left / Alt+Right, or the ‹ › buttons) step through the files
  shown in the grid without closing the viewer. Images load in the background, so the app never waits for a big
  one; images over 8192 pixels are shown at 8192 (the details list their real size), and files over a
  gigapixel, which would need many GB to open, say so instead.
- **Sidebar:** the menu button (or **Ctrl+B**) slides it in and out, and dragging its right edge resizes it. With the pin button it stays
  docked beside the grid. Unpinned, it floats over the grid on frosted glass and slides away after you pick a
  category (or press Esc, or click beside it).
- **Profiles:** separate libraries for separate work, such as a ZBrush profile and a Substance Painter one. Each
  has its own library folders, file types, categories and collections, so only that work's files show. Tags,
  favorites and ratings belong to the files and are shared by every profile, as are the theme, apps, viewer and
  Artbook. Switch with the profile button at the top left (**Ctrl+Shift+P**). Its menu has **New Profile...**,
  which starts as a copy of the current profile or empty (no folders or categories). **Settings > Profiles** renames, duplicates and
  deletes them, and **Desktop Shortcut** puts an icon on the desktop that opens the app in that profile
  (`AssetManager.exe --profile <name>` works too). The app starts in the last profile used, and switching shows
  the profile's last scan at once while it rescans. Right-click a collection to show it in every profile or move
  it to another one.
- **Looks:** **Settings > Appearance** has the themes and a background texture (grain, dots, grid, lines,
  hexagons, carbon fiber, topographic) and a Glass Frost slider for the sidebar. Icons are two-tone and take
  their colors from the theme, and so do the windows' title bars (on Windows 11, the theme's own colour).
  Lists scroll smoothly, categories slide open, and when the window is resized
  the grid's cards glide into their new rows.
- **Your own themes:** the **Theme Studio** (Start menu: Asset Manager Theme Studio, or
  `AssetManager.exe --theme-studio`) is a separate window for making themes and backgrounds. You edit every
  colour with a live preview of the app, and make background patterns or tiles from your own images. It
  saves `.amtheme` files and never changes the app's settings. See **Settings > Guide > Theme Studio**.
- **Help:** **Settings > Guide** has the Artbook guide, the Theme Studio guide and this Read Me, right in the app.

### Texture sets, renaming and converting
- **Texture sets:** a material's maps (Rock_BaseColor, Rock_Normal, Rock_Roughness ...) show as one card, with its
  colour map's picture and a "SET · n" badge (**Library > Group Texture Sets** turns it off). A set is the images in
  one folder, or one folder inside an archive, whose names differ only by a map word; UDIM tiles (`.1001`, `.1002`)
  are one set. A search that finds any of its maps shows the set. Tagging, favorites, ratings, Send to Artbook and
  dragging a set card apply to all its maps (drag one into Substance Painter's shelf or Blender's node editor).
- **The material:** double-click a set (or right-click > **Preview Material**) for it on a sphere, cube or plane,
  under the viewer's lights or your HDRIs: colour, normal (with **Flip Green** for DirectX ones, guessed from the
  names), roughness or gloss, metallic, AO, emissive, opacity, packed ORM maps, and height as real displacement.
  Each map's kind can be corrected, or a map left out; a set with a Normal and a Normal_OpenGL uses the OpenGL one,
  a Displacement map is used for height over a Bump map, and of two files named alike the EXR over a PNG or JPG.
  The **Adjust** sliders (colour, normal, roughness, metal, AO, emissive, height) change the preview only, each with
  its value beside it; double-click one to reset it. Height is the map's depth as a share of its width.
- **Send to Blender / Maya as Material** (right-click a set, or in the material window) makes the material in the
  Blender or Maya that's open, on its selected objects, through the helper in **Settings > Apps**: a Principled BSDF
  in Blender; an Arnold **aiStandardSurface** in Maya (or a standardSurface: Settings > Apps), with an aiNormalMap,
  Raw colour space for data maps and displacement on the shading group (turn on the mesh's Arnold subdivision to
  see it). AO maps are added but not connected (renderers make their own shadowing). After an update, the helper
  is brought up to date by itself: restart Blender or Maya once.
- **Rename** (F2, or right-click > **Rename...**): new names from a pattern (`{name}`, `{set}`, `{map}` with the map
  words as they are, Substance style or short, `{n}` / `{n:3}`, `{folder}`), a find and replace and a letter case,
  shown for every file first, with what's in the way. Renamed files keep their tags, favorites and ratings, and
  Ctrl+Z renames them back. Files inside archives can't be renamed.
- **Convert and Resize** (right-click pictures): PNG, JPG, TGA or TIF copies, at full size or the longest side at
  512 / 1K / 2K / 4K / any size (never bigger), optionally each side down to a power of two, into a folder next to
  each file or one folder. 16-bit heights and masks stay 16-bit in PNG and TIF; the originals are never changed.
  **Move New Files to Recycle Bin** takes them back.

## The Artbook

The Artbook holds reference boards, called **Pages**: endless canvases you fill with images from the library,
Explorer, a web browser or the clipboard. New to it? The [Artbook guide](docs/artbook-guide.md) walks through
making your first board. The list below is the full reference.

- **Views:** the **View** button (top right) switches between Asset View and Artbook View. Its arrow, or
  **Ctrl+1** to **Ctrl+4**, also offers Split View (both side by side; drag the divider) and Separate View (the
  Artbook in its own window). The app reopens in the view you left it in.
- **Getting images in:** drop files or images from a browser (Google Images results too), drag them in from
  the asset grid, or paste with **Ctrl+V** (image data, files or an image link). In the asset grid,
  right-click > **Send to Artbook** adds the selected images to any page (listed in its folders), or to a new page.
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
  everything, double-click zooms to an image. A lighter **active area** sits behind everything on the page and
  grows, shrinks and moves with it as you add or spread out images (exports leave it out).
- **Right-click menu:** Undo / Redo, Copy / Paste, then **Canvas** (frame, 100%, grayscale board) and **Images**
  (Select All, and the Selection, Arrange, Align and Match Size submenus). Greyed-out items need a selection.
- **Adjusting images:** **C** crops the selected image: drag the handles inward (or back out), Enter or C keeps
  it, Esc cancels. The whole image is kept, so Right-click > Images > Selection > **Reset** > Crop brings it all
  back. The same Reset submenu undoes rotation, size, flips or crop one at a time (**R** resets all but the crop). **G** turns grayscale on / off for the selected images, the same menu has an **Opacity** slider, and
  **Shift+G** shows the whole board in grayscale to judge values (remembered per page; page thumbnails stay in
  colour). Selection also has **Pixelated** (hard pixel edges when zoomed in, for pixel art and textures) and
  **Caption...** (a line of text under the image that moves, scales and flips with it).
- **Values, guides and mirroring** (for studying a reference; nothing is changed in the picture itself):
  - Selection > **Values** shows the selected images as 2, 3, 4 or 5 flat greys, black to white: the light and
    shadow shapes without the detail. **Shift+V** (**Values Board**) does it to the whole board, with as many tones
    as you pick under Canvas > Values Board Tones (3 to start with; remembered per page).
  - Selection > **Guides** draws lines over the selected images: **Thirds**, **Grid 4x4**, **Grid 8x8** (for
    transferring proportions), **Golden Ratio**, **Centre and Diagonals**, or **Custom Grid...** (any number of
    columns and rows, up to 32). They stay one pixel thin at any zoom, and show in an exported PNG.
  - **Shift+H** (**Mirror Board**) shows the whole board flipped left to right, to see proportions with fresh
    eyes; Shift+H again turns it back. Everything still works while it's mirrored, and the page doesn't change.
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
- **Upscale:** right-click > Images > **Upscale...** makes small images 2 or 4 times bigger with
  [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN) (free; it downloads once, 45 MB, and runs on the graphics
  card: a second or three a picture). **Photo** suits photos, renders, sculpts and paintings; **Illustration** suits
  line art, manga and anime. Drag the line to compare each one with its original (the wheel zooms), untick any you
  don't want, then **Use Upscaled Pictures**: they keep their place and size on the board, as one Ctrl+Z. It invents
  believable detail rather than recovering it (small text and fine material detail can come out wrong), so every
  image keeps its original: Images > **Revert to Original**, even after the page is reopened. Your files are never
  changed; the bigger picture is kept in the page. Results are at most 6000 px.
- **Draw:** **D** draws freehand strokes, with a bar at the top for the pen's colour (the theme's, white, black or
  custom), its size, **smoothing** (on to start with: steadies lines drawn with a mouse, as with a pen; the button
  turns it off) and the **eraser** (**E**: drag over strokes to remove them). D again, Esc or Done stops.
  Strokes move, scale, rotate, group and undo like everything else, and follow the theme in its colour.
- **Notes:** **T** adds a note at the cursor (its text centered) and you type straight away; Esc or clicking elsewhere finishes,
  and double-clicking a note edits it. Notes take the theme's colours. Select one and a format bar appears
  above it: text colour, highlight, note colour, bold / italic / underline (Ctrl+B / I / U while typing), size,
  font, and alignment / clear formatting under the chevron. Each colour offers Theme Default (No Highlight for highlights), White, Black or
  **Custom...**: a floating picker (opens beside the note; drag its title to move it) with a colour square,
  hue and opacity sliders, a screen dropper, HEX / RGB / HSL text and ready-made colours. The note changes as
  you pick, and the whole pick is one Ctrl+Z. Highlights have rounded corners, like the notes. With text selected while typing, the bar changes
  that text; otherwise the whole note. Notes move, scale, rotate, arrange and align like images; drag a note's
  left / right handle to make it wider or narrower. An emptied note is removed; Ctrl+Z brings it back.
  Text pasted into a note (Ctrl+V while typing) keeps its formatting but not a web page's paragraph backgrounds;
  **Ctrl+Shift+V** pastes plain text in the note's own style. Highlight > **No Highlight** removes every
  background behind the text, including ones pasted before.
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
  - Like the library's sidebar, it can be pinned beside the board or float over it on frosted glass.
  - **Share a page:** right-click a page > **Export Page...** (or right-click the board > Canvas > Export Page)
    saves it as one `.artbook` file with its images inside (linked files go in whole), ready to send.
  - **Import:** the import button at the top (or right-click > Import Pages..., or drop the files on the board)
    opens `.artbook` files and **PureRef boards** (`.pur`) as new pages. PureRef boards keep their layout:
    positions, sizes, rotation, flips, crops, opacity, notes, image names, groups (with their colour) and
    drawings (each stroke with its colour and width; a drawing's strokes are grouped, so they move as one).
    Pick several at once. Boards from PureRef 1.x need opening and saving in PureRef 2 first.
- **Export as PNG:** **Ctrl+E** (or right-click > Canvas) saves the selection, or the whole board, as one picture on
  the board's colour, as sharp as its images allow (up to 16384 pixels on the longest side).
- **Saving:** pages save themselves a couple of seconds after each change, and when you switch page or quit.
  Library images are *linked* (the page keeps a small copy in case the file moves); pasted and web images are
  stored in the page. Images you've deleted from a page are cleared from its folder the next time it opens.
  Each page keeps its previous save as `page.json.bak`. If its file is ever damaged (a disk error, a sync tool),
  the page opens from that copy and says so. A page with no good copy stays in the list, in its folder, to delete.
- **Earlier versions:** the first time a page changes on a day, how it was before is kept (the last 10 versions,
  in the page's `history` folder). Right-click a page in the Pages sidebar (or the board > Pages) >
  **Earlier Versions...** lists them with a picture of each: **Restore This Version** (how the page is now is kept
  as a version first, so you can go back) or **Restore as a New Page**. Duplicating or exporting a page leaves
  its versions behind.
- **Where pages are kept:** one folder per page, in `%LOCALAPPDATA%\AssetManager\artbook` unless you choose
  another folder in Settings > General > **Artbook** (a synced OneDrive or Dropbox folder keeps them backed up).
  **Change...** moves your pages there: they're copied first and only then removed from the old folder. A folder
  that already has pages shows those instead (your current ones stay put, to switch back to). **Use Default**
  moves them back. If the folder isn't there when the app starts (an unplugged drive), the default one is used
  until it's back.
- **Big boards:** images load at the detail they're seen at. Zoomed out, each shows a small copy; the ones in
  view get sharper as you zoom in, up to the full picture (past 4096 pixels), and let it go again once they're
  well out of view, so even boards with hundreds of large images open quickly and stay light on memory. Small
  copies are cached (Settings > Video > **Clear Cache** clears them too), so a page opens almost at once the
  second time. Export as PNG and Copy always use the full pictures, whatever is showing.
- **Motion:** arranging, aligning, stacking and undoing glide images to their new places, and pictures fade in
  as they load. The Motion setting in Settings > Appearance turns this off (or follows Windows).

## Viewing 3D models

Open a model (`.fbx`, `.obj`, `.gltf`, `.glb`, `.stl`, `.ply` ...) from the grid to turn it round in 3D.

- **Big models:** the viewer opens at once and the model loads in the background. A 300 MB, 14-million-triangle
  sculpt takes a few seconds: binary FBX and OBJ are read with quick readers of their own (`core/fbx_fast.py`,
  `core/obj_fast.py`; a 270 MB OBJ in about 5 seconds, a 2 GB one in about a minute). OBJ files keep their own
  normals, so hard edges stay sharp. Moving on to another file or closing the viewer stops a model still loading.
- **Read safely, apart from the app:** models (for the viewer, the grid's thumbnails and the face counts) are read
  by a helper program of the app's own, one model at a time, so the window never stutters while a big one is read
  and a file that crashes its reader can't take the app with it. The helpers together may use up to half the
  computer's memory. A model that crashed the reader, ran out of memory or took too long gets a "Couldn't read" card
  and isn't read again until the file changes (the viewer always tries).
- **Moving round:** drag to turn the model, right-drag (or middle-drag) to pan, scroll to zoom. **R** resets the
  view, **G** the ground grid, **W** cycles shaded / wireframe / both.
- **Its look:** pick a **material** (Matte, Clay, Phong, Glossy, Metallic, Toon, or Normals to check the
  model's normals), a **colour** (the Artbook's colour picker: the model follows as you drag) and a
  **lighting** set-up (Studio, Soft, Dramatic, Top, Headlight). They're remembered for every model.
- **Turning the lights:** Shift+drag turns the lights round the model instead of the model; or switch on
  **Turn Lights** (**L**) to make a plain drag do it. The dial in the top right shows where the main light is
  (hollow when it's behind the model). **Reset View** puts the lights back too.
- **Metal's roughness:** Metallic has a **Roughness** slider, from a mirror (0) to a soft sheen (100), remembered
  for every model.
- **HDRI lighting:** the colour 2:1 HDRIs in your library are listed under **Lighting**, with **Browse for an
  HDRI...** for any other, and right-click an HDR in the grid > **Use as Viewer Lighting**. Up to three of its
  brightest sources become real lights (`core/environment.py`) and the rest gives the soft light. The HDRI stays
  fixed in the scene as you orbit, **Shift+drag** turns it, and **Auto Rotate** spins the model inside it. It has
  an **Exposure** slider, and **Backdrop** (**B**) shows it behind the model. Presets and thumbnails are unaffected,
  and there are no cast shadows or ambient occlusion.
- **What it's made of:** the details list polygons, quads, triangles and n-gons as the file has them, and the size
  with its unit where the format says one (cm from FBX, m for glTF).
- **UVs:** **U** shows the UV layout, read only when you ask: true polygons for OBJ and binary FBX, the UDIM tiles
  in use numbered, and a picker for files with several UV sets. Drag to pan, scroll to zoom, **R** to fit.
- **Rendering:** **Render** (**F12**) makes a PNG of what's in the viewer, at 720p, 1080p or 1440p. It uses
  [LuxCore](https://luxcorerender.org/), a free path tracer that gives cast shadows and light between parts of a
  model. It downloads once, the first time (157 MB, about 300 MB unpacked), and renders in a process of its own for 5,
  20 or 60 seconds (**Draft**, **Good**, **Best**), with a live preview, **Stop** (which keeps what's done) and
  denoising. Its first render on a computer spends a couple of minutes preparing for the graphics card. If LuxCore can't
  run on your computer, the **Viewer** renderer needs nothing: the same picture as on screen, instantly, without
  cast shadows. Toon and Normals can't be rendered, and textures aren't used yet. **Remove LuxCore** deletes it.
- **Thumbnails:** models get a rendered picture in the grid, made once and kept with the other thumbnails.
  Models over 150 MB get theirs the first time you open them (reading one just for the grid would take
  seconds and a lot of memory). A picture next to the model (`model.png`) is used instead when there is one;
  Settings > Appearance > **Pictures of 3D models** turns them off.

## Reading PDFs

Open a PDF from the grid and the viewer shows it page by page, sharp at any zoom.

- **Moving around:** scroll, or drag with the Hand tool (middle-drag or Space+drag work with any tool).
  **Left** / **Right** turn pages, **Home** / **End** go to the first and last; type a page number in the
  page box. Page Up / Page Down still switch to the previous / next file, as in the rest of the viewer.
- **Zoom:** Ctrl+wheel (or pinch) zooms toward the pointer; the zoom box takes presets or a typed size;
  **W** fits the page's width, **F** the whole page, **Ctrl+0** is actual size.
- **Sidebar (Ctrl+B):** **Contents** lists the PDF's chapters, **Pages** shows a picture of every page, and
  **Notes** lists your marks. Click any of them to go there. Links in the PDF work too: a page of the PDF,
  or a web address in your browser.
- **Find (Ctrl+F):** every match is highlighted; Enter / F3 for the next, Shift+Enter / Shift+F3 for the
  previous. Capitals don't matter and phrases are found across line breaks.
- **Text:** the Select Text tool (**S**) selects text by dragging, across pages too; **Ctrl+C** copies,
  **Ctrl+A** selects a page's text. (Scanned pages have no text to find or select.)
- **Your marks:** **Highlight** (**H**: drag over text, or highlight a selection), **Pen** (**D**),
  **Note** (**T**: click and type) and **Eraser** (**E**), with a colour for each and a pen size. With the
  Hand tool, click a mark to pick it, drag a drawing or note to move it, **Delete** removes it, and
  double-clicking a note edits it. **Ctrl+Z** / **Ctrl+Y** undo and redo; **Marks** hides them all.
- **Sharing:** right-click a page > **Send Page to Artbook** (with or without your marks), or **Export...**
  saves a copy of the pages with your marks drawn in: one PDF or PNG images, at 150 or 300 dpi.
- **The PDF is never changed.** Your marks, and the page and zoom you were at, are kept by the app
  (`%LOCALAPPDATA%\AssetManager\pdf-notes`), matched to the file's contents, so they follow it when it's
  renamed or moved. A PDF reopens where you left it.

## Video playback

Videos (`.mp4, .mov, .mkv, .avi, .webm, .m4v`) play inside the asset viewer using Qt Multimedia.
No extra Python packages are needed.

- **Playback speed:** the speed button opens presets from 0.25× (lowest) to 3.0× (highest), with a slider
  above them for anything in between, in 0.05× steps. The speed is remembered.
- **Mute:** click the speaker icon or press **M**. Moving the volume slider up unmutes too.
- **Resume:** videos start where you left them. **Home** starts from the beginning.
- **Frame step:** **,** and **.** pause and step one frame back or forward.
- **A-B loop:** press **L** (or the A-B button) at the start and at the end of the part to repeat; press it again to stop.
- **The frame on screen:** right-click the video > **Send Frame to Artbook** (any page, or a new one), **Copy Frame**
  or **Save Frame As PNG...**, at the video's own resolution, named after the video and the time (e.g.
  "Creating The Head 12:41"). Playback carries on. A video playing from its preview copy gives that copy's frame.
- **ffmpeg** is optional. If you install it (for example `winget install ffmpeg`), the app uses it to:
  - make poster-frame thumbnails in the grid
  - make 720p H.264 preview copies ("proxies") of files that won't play or that stutter
  - read video lengths for `dur:` searches

  The app finds ffmpeg on the PATH, or you can point it at `ffmpeg.exe` in **Settings > Video**.
- **The cache** for proxies and thumbnails is in `%LOCALAPPDATA%\AssetManager`. You can clear it in **Settings > Storage**.
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
  - `is:fav`, `rating:>=3`, `ext:png,jpg`, `size:>10mb`. Sizes, lengths and face counts without `<` or `>` match
    to the precision typed: `size:10mb` is 9.5-10.5 MB, `size:10.2mb` is 10.15-10.25 MB
  - a minus excludes, as with tags: `-ext:png`, `-is:fav`
  - `res:>=2048` (image or video resolution, `res:4k` = 4096), `dur:<30s`, `poly:<10k` (3D model faces). These
    details are read in the background after a scan; files not read yet don't match.
  - `color:red` pictures with red on 15% of them or more (red, orange, yellow, green, cyan, blue, purple, pink,
    brown, skin, black, white, grey; normal maps are left out), `color:#c04020` colours close to that one,
    `-color:grey` without it. The palette button in the search box lists the names, or picks any colour (also
    from the screen), and the search follows as you pick.
  - plus file name words, in any order (`rock wall` finds `Wall_Rock_02.png`; `"rock wall"` keeps them together).
    Backspace removes the last chip; Esc or ✕ clears everything.
- **Find Similar:** right-click a picture > **Find Similar** shows it and its look-alikes from the whole library,
  closest first: resized or re-saved copies, and very close versions (a "Similar to ..." chip in the search box;
  remove it, combine it with other searches, or save it as a collection). **Search by Its Colours** in the same menu
  searches for one of the picture's main colours, which its tooltip shows too. **Tools > Find Look-alikes...**
  groups the look-alike pictures in the files shown, the biggest file first, to recycle extra copies; check the
  groups first: a turntable's frames or an alpha's JPG preview look alike too.
  - Each picture is looked at once in the background (its main colours, and a small fingerprint of how it looks),
    then only again when it changes. The first time takes a while on a big library (about 9 minutes for 8,500
    pictures, half of them inside archives); searches fill in as it goes.
  - On the author's library, 100% of half-size copies, 97% of low-quality JPEG re-saves and 90% of slightly cropped
    copies were found, while unrelated pictures matched 0.06% of the time. A picture of one flat colour (a blank
    normal map) can't be compared.
- **Folder rules:** **Settings > Folder Rules** (or right-click a file > **Folder Rule...**) tags files that are
  added to a folder from now on: "files added to WIP get the tags WIP and Kitbash". New files, and files copied or
  moved in, get the tags once, also when they were added while the app was closed (at the next scan); a tag you
  take off a file stays off. Making a rule can tag the files already in the folder too. Rules work in every
  profile, and follow tags being renamed or deleted.
- **Collections:** click the star in the search box to save the search in the sidebar. Right-click one to rename or
  delete it. Renaming a tag updates the collections that use it.
- **Categories by tag:** in **Settings > Categories**, a category's **Tags** list adds the files with any (or all)
  of the ticked tags to what its file types show. A "Main Project" category with FBX and the tag Main shows every
  FBX file plus anything tagged Main, images and videos included. Right-click a tag in **Settings > Tags** and
  choose **Add as Sidebar Category** to make one in a click. Renaming a tag renames it in categories too, and
  deleting one takes it out of them; **Uncategorized** shows the files no category shows.
- **Backups:** the database is backed up automatically once a day (the last 7 are kept) and before upgrades and app
  updates (the last 10). If it's ever damaged, the app offers to restore the newest good backup, keeping the
  damaged file in the backups folder. **Tools** also has Back Up Library Now, Restore Library Backup..., and
  Export / Import Tags (a `.json` file with your tags, favorites, ratings and collections, for moving to another PC).
  Artbook pages, their folders and PDF notes each keep their previous save too (`.bak`), opened if a file is damaged.

Known limitations:
- A file that's moved *and* edited before the next scan, or that has an identical copy, can't be matched and starts
  untagged (rather than risk tagging the wrong file).
- After each library scan, the database forgets files the scan didn't find (deleted ones, or folders opened with
  Open Folder) unless they have custom tags, a favorite or a rating; those are kept in case the file turns up again,
  as are files on a drive that's offline.
- 3D model face counts are read for files up to 1 GB (the smallest first, so a big one never holds up the rest);
  bigger ones don't match `poly:` searches.

## When something goes wrong

Crashes, freezes (the window not responding for 20 seconds) and errors are written to
`%LOCALAPPDATA%\AssetManager\crash.log`. After a crash the app says so on the next launch, and
**Settings > About > Open Crash Log** opens the log. **Report a Problem...** next to it makes a zip for a bug
report: the crash and session logs, the app's and Windows' versions, your processor, memory and graphics card,
and how big the library is (counts only). Your settings go in only if you tick it (they have your folders'
paths), and nothing is uploaded; **Open a GitHub Issue** opens the form to attach it to.

**Settings > Storage** shows what the app keeps on disk, with the total: files unpacked from archives (with
their limit), thumbnails and previews, the Artbook's small copies (each can be cleared: they're made again),
LuxCore and Real-ESRGAN (each can be removed: they download again when needed), and the library's backups
and your Artbook pages (only opened from here, never cleared).

## Shortcuts

**Settings > General > Shortcuts** lists every keyboard shortcut and mouse control, grouped by where it works.
That page is generated from `ui/shortcuts.py`, the one place shortcuts are defined. Code binds keys by id from
the registry, and `tests/test_shortcuts.py` fails if a hard-coded key appears anywhere else, so new shortcuts
always show up on the page. **Settings > Appearance > Animations** turns animations off (or follows Windows).

## Development

`pip install -r requirements-dev.txt -c requirements.lock`, then `python -m pytest`. **Speed tests**
(`python -m pytest -m speed`, also a step of the Tests run on GitHub) time loading, searching and counting on a
made-up 50,000-file library, and fail when something takes over its budget (`tests/test_speed.py`). `requirements.lock` holds the
exact versions the tests pass with; CI and release builds install those, so a release never ships an untested
version (its header says how to update it). The project rules (shortcuts, threads, tooltips) are in `CLAUDE.md`. Release builds: `pyinstaller AssetManager.spec` makes both the portable exe and the folder
build; `iscc /DAppVersion=x.y.z installer\AssetManager.iss` turns the folder build into the installer.
