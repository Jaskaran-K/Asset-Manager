# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build for the single-file windowed AssetManager.exe.
# Build locally with:   pyinstaller AssetManager.spec
# The release workflow (.github/workflows/release.yml) uses this same file.


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('assets/Asset-Manager-Icon.ico', 'assets')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AssetManager',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/Asset-Manager-Icon.ico',
)
