# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build of KeyDial, the Huion KD100's tray app (keydial_main.py). One analysis, two outputs:
#   dist/KeyDial.exe        portable single file
#   dist/KeyDial/           folder build, installed by installer/AssetManager.iss when its KeyDial box is ticked
# Build locally with:   pyinstaller KeyDial.spec --noconfirm
# The release workflow builds it beside AssetManager.spec.
#
# KeyDial needs only Qt's widgets, the device (hidapi) and the keys it sends (pynput), so everything
# Asset Manager has for thumbnails, 3D models and the web view is left out.
# assets/KeyDial-Icon.ico is the duotone "keydial" icon (ui/duotone.py), drawn at the icon sizes.

a = Analysis(
    ['keydial_main.py'],
    pathex=[],
    binaries=[],
    datas=[('assets/KeyDial-Icon.ico', 'assets')],
    # pynput picks its Windows backend as it's imported (by name, so PyInstaller doesn't see it)
    hiddenimports=['pynput.keyboard._win32', 'pynput.mouse._win32', 'pynput._util.win32', 'pynput._util.win32_vks'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'numpy', 'scipy', 'PIL', 'trimesh', 'OpenGL', 'OpenEXR', 'fbxloader', 'inflate64',
              'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtMultimedia',
              'PySide6.QtMultimediaWidgets', 'PySide6.QtPdf', 'PySide6.QtOpenGL', 'PySide6.QtOpenGLWidgets',
              'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.Qt3DCore'],
    noarchive=False,
    optimize=0,
)
# Qt parts that come along with plugins KeyDial never loads: software OpenGL (it draws with the CPU anyway),
# Qt Quick / QML (the on-screen keyboard plugin), the PDF engine (the PDF image plugin), and the second
# OpenSSL Qt brings for web TLS (KeyDial only talks to itself, through a local pipe)
_UNUSED = {'opengl32sw.dll', 'qt6pdf.dll', 'qpdf.dll', 'qt6qml.dll', 'qt6qmlmeta.dll', 'qt6qmlmodels.dll',
           'qt6qmlworkerscript.dll', 'qt6quick.dll', 'qt6virtualkeyboard.dll', 'qtvirtualkeyboardplugin.dll',
           'qopensslbackend.dll', 'libcrypto-3-x64.dll', 'libssl-3-x64.dll'}
a.binaries = [entry for entry in a.binaries if entry[0].replace('\\', '/').rsplit('/', 1)[-1].lower() not in _UNUSED]
pyz = PYZ(a.pure)

common = dict(
    name='KeyDial',
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
    icon='assets/KeyDial-Icon.ico',
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
    name='KeyDial',
)
