# Theme Studio

The Theme Studio is a separate window for making your own themes and background textures. It
doesn't change the app or its settings: you save what you make as a theme file (`.amtheme`).

## Opening it

- **Start menu:** Asset Manager Theme Studio (installed with the app).
- **Or** run `AssetManager.exe --theme-studio`. Add a theme file after it to open that theme.
- **From the source code:** `theme-studio.bat`, or `python main.py --theme-studio`.

## Making a theme

1. Pick a built-in theme under **Start from** and click **New** (**Ctrl+N**). Give it a name.
2. On the **Colours** tab, every colour of the theme is listed, grouped by where it shows. Hover
   a name to see where it's used.
   - Click a **swatch** for the colour picker (the same one as the Artbook's custom colour): a
     square for shade, bars for hue and opacity, a dropper, and **sliders with numbers** for
     R, G, B and opacity (or H, S, L: choose HSL in its box). The preview changes **live** as you
     drag.
   - Or **type** it: `#rrggbb`, `#aarrggbb` (see-through), `rgba(r, g, b, a)` or `transparent`.
     A red box means it isn't a colour yet, and it isn't used.
   - Every **Surfaces** colour (window, panels, sidebar, grid, borders, hover) has a **Gradient**
     toggle: a From and a To colour (and a Middle one if you tick it), each with its own swatch,
     running Diagonal, Down, Across or Up.
   - The **arrow** button beside a colour puts back the base theme's.
3. Text colours show a **contrast** badge against what they're read on:
   - green (4.5:1 or more) is easy to read;
   - amber (3 to 4.5) is fine for big or short text;
   - red (under 3) is hard to read.
4. **Accent Shades from the Accent** works out the accent's border, hover, soft and text
   colours from the accent you picked. **Reset All** goes back to the base theme.

The preview on the right uses the app's own stylesheet and icons, so it looks as the app will.
It has three tabs, and turns to the one that shows the colour you're changing:

- **App:** the main window, and below it a dialog with a viewer's toolbar (Panels (second)) and
  Settings-like controls. The **Window** colour is what dialogs sit on, and it shows in the gaps
  around the main window. Accent text shows in the inspector's heading, the link, the zoom value
  and the details of every icon. Hover and click the preview's buttons to see those states.
- **Glass and Artbook:** an Artbook page with the frosted sidebar over it: the Glass tint and
  Glass edge, the Artbook active area, and the board with its texture.
- **Icons:** every icon of the app at both sizes, in each tone (accent, text, done, danger,
  warning, unavailable), on the sidebar and on buttons.

## Backgrounds

On the **Background** tab:

- **None**, or a **Built-in texture**: the app's own.
- **Pattern:** dots, grid, lines, crosshatch, checker, hexagons, waves or noise. Sliders set the
  spacing, thickness, dot size and strength. Patterns always repeat without a seam.
  **Tint:** they're drawn in the theme's texture ink, or tick **Own colour** and pick one.
- **Image tile:** **Choose Image...** picks a picture of your own, such as a scan or a painted
  texture. It's kept inside the theme file.
  - **Tint with the ink colour:** its light parts (the white of a black and white picture) show
    in the theme's texture ink, so it suits any theme; or tick **Own colour** under **Tint** and
    pick a colour of its own. **Keep its own colours:** it shows as it is.
  - **Strength** and **Size**, and **Invert** to swap light and dark.
  - The seam check says whether copies meet with a visible edge. **Make seamless** blends the
    edges so they don't.

The tile preview shows it repeated 3 x 3 at twice its size, with dashed lines where copies meet.
The texture ink colour (Colours > Glass and background) is what patterns and tinted images are
drawn in, unless you give them their own tint.

## Saving and sharing

- **Save** (**Ctrl+S**) / **Save As...** (**Ctrl+Shift+S**) writes the `.amtheme` file;
  **Open...** (**Ctrl+O**) opens one again. If you close with unsaved changes, it asks first.
- To get a theme into the app, hand over the `.amtheme` file. **Copy for the App** copies the
  theme's colours in the form the app's code uses (`ui/themes.py`).
- A theme file holds only colours, settings and the background picture. When it's opened, every
  colour is checked; anything damaged falls back to the base theme's, and the studio says which.
