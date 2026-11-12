"""``--selftest``: verify a (packaged) build without touching the light.

Imports every runtime dependency the app uses (bleak and its WinRT backend, pywebview's
EdgeChromium / pythonnet platform, pystray, Pillow, mss, numpy, PyAudioWPatch) and every
app module, loads the web assets and the data files -- and stops there: no Bluetooth
scan or connection, no window, no tray icon, no settings or log written.

Prints ``selftest ok <version>`` and returns 0, or ``selftest failed: <error>`` and
returns 1. The frozen exe is windowed (no console), so it also writes the same text to
``selftest.txt`` next to the exe; build.ps1 reads that and the exit code.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import traceback
from pathlib import Path

# third-party modules the app imports (some only lazily, inside functions)
DEPENDENCIES = (
    "bleak",
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
    "webview",
    "clr",                               # pythonnet
    "webview.platforms.edgechromium",    # loads the WebView2 .NET assemblies
    "pystray",
    "pystray._win32",
    "PIL.Image",
    "PIL.ImageDraw",
    "mss",
    "mss.windows",                       # mss.mss() picks it at run time
    "numpy",
    "pyaudiowpatch",
)
# present in the build but not executed: importing it picks a renderer and may fall
# back to MSHTML (which writes an IE-emulation registry value) on a PC without WebView2
PRESENT_ONLY = ("webview.platforms.winforms", "webview.platforms.win32")

APP_MODULES = (
    "govee_win11.api", "govee_win11.app", "govee_win11.config", "govee_win11.discovery",
    "govee_win11.diy_format", "govee_win11.effects", "govee_win11.engine",
    "govee_win11.frames", "govee_win11.i18n", "govee_win11.link", "govee_win11.music",
    "govee_win11.palette", "govee_win11.power", "govee_win11.profile",
    "govee_win11.protocol", "govee_win11.scenes", "govee_win11.screen",
)
WEB_ASSETS = ("index.html", "app.js", "i18n.js", "style.css")


def check() -> list[str]:
    """Run every check; returns short notes. Raises on the first failure."""
    from . import __version__, paths

    notes = [f"version {__version__}", f"frozen {paths.is_frozen()}"]
    for name in DEPENDENCIES + APP_MODULES:
        try:
            importlib.import_module(name)
        except Exception as e:
            raise RuntimeError(f"import {name}: {e!r}") from e
    for name in PRESENT_ONLY:
        if importlib.util.find_spec(name) is None:
            raise RuntimeError(f"module missing from the build: {name}")
    notes.append(f"{len(DEPENDENCIES) + len(APP_MODULES)} modules imported")

    import webview  # its JS bridge and the WebView2 loader are read at run time
    wv = Path(webview.__file__).resolve().parent
    for f in (wv / "js" / "api.js",
              wv / "lib" / "Microsoft.Web.WebView2.Core.dll",
              wv / "lib" / "runtimes" / "win-x64" / "native" / "WebView2Loader.dll"):
        if not f.is_file():
            raise RuntimeError(f"pywebview file missing: {f}")

    web = paths.resource("web")
    for asset in WEB_ASSETS:
        f = web / asset
        if not f.is_file() or f.stat().st_size == 0:
            raise RuntimeError(f"web asset missing: {f}")
    index = (web / "index.html").read_text(encoding="utf-8")
    for ref in ('src="i18n.js"', 'src="app.js"', 'href="style.css"'):
        if ref not in index:
            raise RuntimeError(f"index.html does not reference {ref}")
    i18n_js = (web / "i18n.js").read_text(encoding="utf-8")
    start = i18n_js.index("const TABLES = ") + len("const TABLES = ")
    tables = json.loads(i18n_js[start:i18n_js.index("; // end TABLES", start)])
    if set(tables) != {"zh", "en"} or set(tables["zh"]) != set(tables["en"]):
        raise RuntimeError("web/i18n.js tables are incomplete")
    notes.append(f"web assets ok ({len(tables['zh'])} UI texts)")

    from .profile import load_profile
    from .scenes import load_scenes
    profile = load_profile()
    scenes = load_scenes()
    if profile.segment_count <= 0 or not scenes:
        raise RuntimeError("data files are empty")
    notes.append(f"data ok ({profile.segment_count} segments, {len(scenes)} scenes)")
    return notes


def _output_file() -> Path | None:
    from . import paths
    return Path(sys.executable).resolve().parent / "selftest.txt" if paths.is_frozen() else None


def run() -> int:
    from . import __version__

    for name in ("stdout", "stderr"):  # windowed exe / pythonw: no console streams
        if getattr(sys, name) is None:
            import os
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    try:
        notes = check()
        text, code = f"selftest ok {__version__}\n" + "".join(f"  {n}\n" for n in notes), 0
    except Exception as e:
        text = f"selftest failed: {e}\n" + traceback.format_exc()
        code = 1
    print(text, end="", flush=True)
    out = _output_file()
    if out is not None:
        try:
            out.write_text(text, encoding="utf-8")
        except OSError:
            pass
    return code
