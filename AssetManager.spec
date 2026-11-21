# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build. One analysis, two outputs:
#   dist/AssetManager.exe        portable single file (unpacks itself on every launch, so it starts slower)
#   dist/AssetManager/           folder build, packaged by installer/AssetManager.iss into
#                                dist/AssetManager-Setup.exe (starts much faster)
# Build locally with:   pyinstaller AssetManager.spec --noconfirm
# The release workflow (.github/workflows/release.yml) uses this same file.

from pathlib import Path

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('assets/Asset-Manager-Icon.ico', 'assets'), ('README.md', '.'), ('docs/artbook-guide.md', 'docs'), ('docs/theme-studio.md', 'docs'),  # (the Guide in Settings)
           ('browser-extension', 'browser-extension')],  # (core.browser_link copies it for the browser to load)
    # Imath: OpenEXR's compiled module imports it as it loads, unseen by PyInstaller.
    # inflate64: Deflate64 zips; core.archives imports it only when one is read.
    # importlib.metadata: ONNX Runtime (downloaded for the AI tools, core.ai_tools) reads its version with it.
    hiddenimports=['Imath', 'inflate64', 'compression.zstd', 'importlib.metadata'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter'],
    noarchive=False,
    optimize=0,
)
# Qt's web view (a course's YouTube lessons: ui.course_web) comes with its developer tools' resources and debug
# copies of its resources (about 85 MB, unused), and its languages besides English (the app is in English)
a.datas = [entry for entry in a.datas
           if not (entry[0].lower().endswith(".debug.pak") or "qtwebengine_devtools_resources" in entry[0].lower()
                   or ("qtwebengine_locales" in entry[0].lower() and not Path(entry[0]).name.lower().startswith("en-")))]
pyz = PYZ(a.pure)

common = dict(
    name='AssetManager',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/Asset-Manager-Icon.ico',
)

# Portable single file
portable = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    upx_exclude=[],
    runtime_tmpdir=None,
    **common,
)

# Folder build for the installer
folder_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    **common,
)
COLLECT(
    folder_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='AssetManager',
)
