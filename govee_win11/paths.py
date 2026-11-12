r"""Where things live, from source and in the PyInstaller build.

- Read-only resources (``web/``, ``data/*.json``): next to the package modules. In the
  frozen build PyInstaller unpacks them to ``sys._MEIPASS/govee_win11``.
- User data (``settings.json``, ``govee.log``): the repo's ``user_data/`` when running
  from source (the developer's settings keep working); ``%APPDATA%\GoveeWin11`` when
  frozen (the install folder may be read-only, and an update replaces it).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping

APP_NAME = "GoveeWin11"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def package_dir(frozen: bool | None = None) -> Path:
    """Directory holding ``web/`` and ``data/``."""
    if frozen is None:
        frozen = is_frozen()
    base = getattr(sys, "_MEIPASS", None)
    if frozen and base:
        return Path(base) / "govee_win11"
    return Path(__file__).resolve().parent


def resource(*parts: str) -> Path:
    return package_dir().joinpath(*parts)


def user_data_dir(frozen: bool | None = None,
                  environ: Mapping[str, str] | None = None) -> Path:
    if frozen is None:
        frozen = is_frozen()
    if not frozen:
        return Path(__file__).resolve().parent.parent / "user_data"
    env = os.environ if environ is None else environ
    appdata = env.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / APP_NAME
