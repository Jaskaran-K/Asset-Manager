# Asset Manager: project rules

## Shortcuts
Every keyboard shortcut and mouse control must be registered in `ui/shortcuts.py`. That
registry generates Settings > General > Shortcuts, so registering is what puts it on that page.

- Add a `Shortcut(id, category, keys, action, sequence)` entry. Use an existing category, or add
  a new one to `CATEGORIES`.
- Bind it by id, never with a hard-coded key: `widget.setShortcut(shortcuts.sequence("id"))` for
  buttons/actions, and `shortcuts.matches("id", event)` in `keyPressEvent` / event filters.
- Mouse gestures and typed syntax (double-click, drag, wheel, `tag:name`) get an entry with
  `sequence=None`. Tests can't detect these, so register them by hand whenever you add or change one.
- `tests/test_shortcuts.py` fails on hard-coded keys (`Qt.Key.Key_*`, `setShortcut("...")`,
  `QKeySequence(...)`) outside the registry, on unknown ids, on registered keys that are never
  bound, and on two shortcuts sharing a key in the same category.

## Threads
PySide6 runs a `lambda` or `functools.partial` connected to a signal on the thread that *emitted*
it. A worker's signal connected to a lambda therefore runs UI code on the worker thread, which
freezes or crashes the app. Methods defined with `def` on a QObject are queued to its thread.

- Connect signals from other threads to methods (`@Slot` preferred), never to a lambda/partial.
  For callbacks, use `ui.tag_widgets.run_in_background`, which relays results to the UI thread.
- Name objects that emit from other threads `worker` / `*_worker`, or put a QRunnable's signals
  on `.signals`. `tests/test_thread_safety.py` relies on these names to catch violations.

## Tooltips
Every button gets a hover tooltip (`setToolTip`) saying what it does, plus its shortcut if it
has one. `tests/test_tooltips.py` fails on buttons without one.

## Tests
`python -m pytest` (dev setup: `pip install -r requirements-dev.txt`). Run it before finishing a change.
