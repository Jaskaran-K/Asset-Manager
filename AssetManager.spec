# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build. One analysis, two outputs:
#   dist/AssetManager.exe        portable single file (unpacks itself on every launch, so it starts slower)
#   dist/AssetManager/           folder build, packaged by installer/AssetManager.iss into
#                                dist/AssetManager-Setup.exe (starts much faster)
# Build locally with:   pyinstaller AssetManager.spec --noconfirm
# The release workflow (.github/workflows/release.yml) uses this same file.


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('assets/Asset-Manager-Icon.ico', 'assets'), ('README.md', '.'), ('docs/artbook-guide.md', 'docs')],  # (the Guide in Settings)
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter'],
    noarchive=False,
    optimize=0,
)
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
