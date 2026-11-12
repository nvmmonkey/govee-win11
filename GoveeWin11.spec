# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: one-dir, windowed build of the Govee H6125 controller.
#   python -m PyInstaller --noconfirm --clean GoveeWin11.spec   (or: .\build.ps1)
# Verify the result without touching the light: dist\GoveeWin11\GoveeWin11.exe --selftest
datas = [
    ("govee_win11/web", "govee_win11/web"),
    ("govee_win11/data/*.json", "govee_win11/data"),
]

# Picked at run time (by platform / by name), so listed explicitly. The installed hooks
# (pyinstaller-hooks-contrib: winrt, clr/pythonnet; pywebview's own __pyinstaller hook:
# webview/js + webview/lib WebView2 DLLs) already find these today -- this keeps the
# build honest if a hook changes. Verified by GoveeWin11.exe --selftest.
hiddenimports = [
    "bleak.backends.winrt.client",
    "bleak.backends.winrt.scanner",
    "winrt.windows.devices.bluetooth",
    "winrt.windows.devices.bluetooth.advertisement",
    "winrt.windows.devices.bluetooth.genericattributeprofile",
    "winrt.windows.devices.enumeration",
    "winrt.windows.devices.radios",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.storage.streams",
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "clr",
    "pystray._win32",
    "mss.windows",
    "pyaudiowpatch",
    "govee_win11.selftest",
]

a = Analysis(
    ["govee_win11/__main__.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "_pytest", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GoveeWin11",
    console=False,
    icon="assets/GoveeWin11.ico",
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="GoveeWin11", upx=False)
