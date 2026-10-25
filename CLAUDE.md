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

## Tests
`python -m pytest` (dev setup: `pip install -r requirements-dev.txt`). Run it before finishing a change.
