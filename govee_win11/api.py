"""Api exposed to the web UI (``window.pywebview.api.<method>``).

Threading: pywebview calls these methods on its own worker threads. The DeviceLink and
Engine live on a background asyncio loop; every device operation is submitted with
``asyncio.run_coroutine_threadsafe(...).result(timeout=...)``. Never call these methods
from the loop thread itself (it would deadlock).

Return values are JSON-serialisable:
- queries (``get_state`` and ``list_*``) return their data directly;
- commands return ``{"ok": True, ...}`` or ``{"ok": False, "error": <message>}``, the
  message in the UI language (``i18n``: ``Settings.language``, auto = Windows).

State pushes: ``on_frame`` / ``on_status`` (wired to Engine / DeviceLink by app.py) call
``push(state_dict)``; frame pushes are throttled to ``FRAME_PUSH_INTERVAL``, status
changes are pushed immediately.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import functools
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import __version__
from . import palette as palette_mod
from . import effects as effects_mod
from . import protocol
from .config import (TRANSITION_MIXES, TRANSITION_RATES, Settings, clamp_color_cal,
                     default_color_cal, default_transition, save_settings)
from . import diy_format
from . import i18n
from .discovery import FoundDevice, normalise_address
from .effects import EFFECTS, EffectMode, speed_from_pct
from .frames import plan_frame
from .link import HIDDEN_LINGER_S, VISIBLE_LINGER_S, classify_connect_error
from .music import MusicMode
from .profile import DeviceProfile
from .protocol import RGB, brightness_packet, power_packet
from .scenes import Scene, load_scenes, packets_for
from .screen import ScreenSyncMode

log = logging.getLogger(__name__)

ONESHOT_TIMEOUT = 30.0  # oneshot may wait out a full BLE connect timeout (phone holds strip)
RUN_TIMEOUT = 30.0      # run(): stop old mode + open + hold (connect attempt)
STOP_TIMEOUT = 15.0
SCAN_TIMEOUT = 20.0     # 搜索设备: a ~5 s BLE scan
SHUTDOWN_TIMEOUT = 3.0  # total for engine.stop + link.close when the window closes
SAVE_ATTEMPTS = 3       # settings.json may be briefly locked by AV / the search indexer
SAVE_RETRY_DELAY = 0.05
FRAME_PUSH_INTERVAL = 0.1  # at most ~10 frame pushes per second

_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")
_SCREEN_KEYS = ("monitor", "saturation", "transition_s", "gain", "mode", "tempo")
_SCREEN_MODES = ("dominant", "fade", "projection", "region")
# link statuses with a message for the UI (state "message", command warnings / errors):
# i18n key "link.<status>"
_LINK_TROUBLE = ("busy_elsewhere", "bt_unavailable", "not_found")


class ApiError(Exception):
    """A user-facing failure: an ``i18n`` message key (+ format params). ``_command``
    turns it into the message in the UI language, shown in the UI as-is."""

    def __init__(self, key: str, **params):
        super().__init__(key)
        self.key = key
        self.params = params

    def text(self, lang: str) -> str:
        return i18n.tr(lang, self.key, **self.params)


class ColorTestMode:
    """颜色校准 test colour: the whole strip one colour, *streamed* by the engine so a
    calibration change goes out through the planner (packet budget, per-segment rate
    cap) via ``Engine.recolor``; static content sends nothing in between."""
    fps = 10.0

    def __init__(self, n: int, color: RGB):
        self.n = n
        self.color = color  # swapped in place by test_color (read by the frame loop)

    def open(self) -> None:
        pass

    def next_frame(self, t: float) -> list[RGB]:
        return [self.color] * self.n

    def close(self) -> None:
        pass


def _command(fn):
    """Wrap a JS-callable command: ApiError -> its message, anything else -> logged."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except ApiError as e:
            return {"ok": False, "error": e.text(self._lang())}
        except Exception as e:  # never let a traceback reach the UI
            log.exception("api.%s failed", fn.__name__)
            return {"ok": False, "error": i18n.tr(self._lang(), "err.generic", detail=e)}
    return wrapper


def _internal(fn):
    """Mark a public method as Python-only: pywebview skips ``_serializable = False``."""
    fn._serializable = False
    return fn


def _parse_color(value: Any) -> RGB:
    m = _HEX.match(value.strip()) if isinstance(value, str) else None
    if not m:
        raise ApiError("err.bad_color", value=value)
    h = m.group(1)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _hex(rgb: RGB) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(int(c) for c in rgb))


def _parse_colors(values: Any) -> list[RGB]:
    if not isinstance(values, (list, tuple)) or not values:
        raise ApiError("err.need_color")
    return [_parse_color(v) for v in values]


def _number(value: Any, lo: float, hi: float, message: str) -> float:
    if isinstance(value, bool):
        raise ApiError(message)
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise ApiError(message) from None
    if not lo <= x <= hi:
        raise ApiError(message)
    return x


def _jsonable(value: Any) -> Any:
    """Defensive copy into plain JSON types (tuples -> lists, unknown -> str)."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


class Api:
    def __init__(self, engine, profile: DeviceProfile, settings: Settings, settings_path: Path,
                 loop: asyncio.AbstractEventLoop, *,
                 push: Callable[[dict], None] | None = None,
                 scenes: list[Scene] | None = None,
                 list_monitors: Callable[[], list[dict]] | None = None,
                 set_autostart: Callable[[bool], None] | None = None,
                 is_autostart: Callable[[], bool] | None = None,
                 discover: Callable[[], Awaitable[list[FoundDevice]]] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        # everything private: pywebview walks public attributes of the js_api object
        self._engine = engine
        self._profile = profile
        self._settings = settings
        self._path = Path(settings_path)
        self._loop = loop
        self._push = push
        self._scenes = scenes
        self._list_monitors = list_monitors
        self._set_autostart = set_autostart
        self._is_autostart = is_autostart
        self._discover = discover
        self._others: list[FoundDevice] = []  # other H6125s seen by auto-discovery
        self._scanned: dict[str, FoundDevice] = {}  # last 搜索设备 results by address
        # select / forget in flight: auto-discovery results are not saved meanwhile (a
        # scan that started before the user's choice must not overwrite it); the choice
        # is reconciled with the link once it has switched (_adopt_link_device)
        self._device_ops = 0
        self._last_discovery: tuple[FoundDevice, list[FoundDevice]] | None = None
        self._clock = clock
        self._lock = threading.RLock()
        self._power: bool | None = None  # unknown until the user switches it
        self._frame: list[RGB] | None = None
        self._mode_desc: tuple[object, dict] | None = None
        self._device: dict | None = None  # single-effect DIY running on the strip itself
        self._last_push = float("-inf")
        # 颜色校准 test colour running: {"mode": ColorTestMode, "resume": bool (a mode
        # ran before -> 完成 restores last_mode)}
        self._color_test: dict | None = None
        self._mode_gen = 0  # bumped whenever the user starts / stops a mode (PowerSync)
        self._language_listeners: list[Callable[[str], None]] = []
        self._apply_transition()
        self._apply_color_cal()

    # ------------------------------------------------------------------ helpers
    def _lang(self, setting: str | None = None) -> str:
        """The resolved UI language ("zh" | "en") of ``setting`` (default: the stored)."""
        if setting is None:
            with self._lock:
                setting = self._settings.language
        return i18n.resolve(setting)

    def _language_info(self, setting: str | None = None) -> dict:
        if setting is None:
            with self._lock:
                setting = self._settings.language
        setting = i18n.normalise_setting(setting)
        return {"setting": setting, "resolved": i18n.resolve(setting)}

    @property
    def _n(self) -> int:
        return self._profile.segment_count

    @property
    def _link(self):
        return self._engine.link

    def _call(self, coro, timeout: float):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            # leave it running (cancelling mid-connect can strand the link); report busy
            log.warning("device call timed out after %.0f s", timeout)
            raise ApiError(self._trouble_key() or "link.busy_elsewhere") from None

    def _trouble_key(self) -> str | None:
        """i18n key of the link's trouble message, None when the link is fine."""
        status = self._link.status
        return f"link.{status}" if status in _LINK_TROUBLE else None

    def _device_stale(self) -> bool:
        return bool(getattr(self._link, "device_stale", False))

    def _link_message(self, lang: str) -> str | None:
        """The link's trouble text; a saved strip that keeps failing also suggests
        choosing another one (设置 → 设备)."""
        key = self._trouble_key()
        if not key:
            return None
        text = i18n.tr(lang, key)
        if self._device_stale():
            text += " " + i18n.tr(lang, "link.choose_other")
        return text

    def _trouble_message(self) -> str | None:
        return self._link_message(self._lang())

    def _failure_key(self) -> str:
        """i18n key for a send that failed: the link's trouble, else "connection lost"."""
        return self._trouble_key() or "err.send_failed"

    def _save(self) -> bool:
        """Persist settings. Never raises: a transient lock on the file (antivirus,
        indexer -> PermissionError) is retried; a lasting failure is logged and the
        command that changed the setting still succeeds (it stays in memory)."""
        for attempt in range(1, SAVE_ATTEMPTS + 1):
            try:
                save_settings(self._settings, self._path)
                return True
            except Exception:
                if attempt == SAVE_ATTEMPTS:
                    log.exception("could not save settings to %s after %d attempts",
                                  self._path, attempt)
                    return False
                time.sleep(SAVE_RETRY_DELAY * attempt)
        return False

    def _clear_mode_locked(self) -> None:
        self._mode_desc = None
        self._device = None
        if self._settings.last_mode is not None:
            self._settings.last_mode = None
            self._save()

    def _bump_mode(self) -> None:
        with self._lock:
            self._mode_gen += 1

    @_internal
    def mode_generation(self) -> int:
        """Changes whenever a command starts / stops a mode (PowerSync stops its
        power-on retries then, instead of stopping the user's mode)."""
        with self._lock:
            return self._mode_gen

    def _oneshot(self, packets: list[bytes], key: str, paced: bool = False) -> None:
        """Stop any continuous mode and send a one-off command (clears last_mode)."""
        self._bump_mode()
        coro = (self._engine.oneshot(packets, key, paced=True) if paced
                else self._engine.oneshot(packets, key))
        ok = self._call(coro, ONESHOT_TIMEOUT)
        with self._lock:
            self._clear_mode_locked()
        if not ok:
            raise ApiError(self._failure_key())

    def _start(self, mode, desc: dict) -> dict:
        """Run a continuous mode and record it as last_mode."""
        self._bump_mode()
        self._call(self._engine.run(mode), RUN_TIMEOUT)
        with self._lock:
            self._device = None
            self._settings.last_mode = desc
            self._mode_desc = (mode, desc)
            self._save()
        self._push_state()
        out = {"ok": True, "status": self._link.status, "mode": desc}
        warning = self._trouble_message()  # mode keeps retrying the connection
        if warning:
            out["warning"] = warning
        return out

    @staticmethod
    def _device_key(effect, colors: list[RGB]) -> str | None:
        """DIY key when ``effect`` can run on the strip itself (single mappable effect and
        1..MAX_COLORS colours), else None (PC-rendered)."""
        if not isinstance(effect, str) or not 1 <= len(colors) <= diy_format.MAX_COLORS:
            return None
        return diy_format.diy_key_for_app_effect(effect)

    def _start_on_device(self, key: str, colors: list[RGB], speed_pct: int, desc: dict) -> dict:
        """Upload a single-effect DIY (paced) and let the strip run it; the PC lets go."""
        self._bump_mode()
        packets = diy_format.diy_packets(key, colors, speed_pct, "a3")
        ok = self._call(self._engine.oneshot(packets, "diy", paced=True), ONESHOT_TIMEOUT)
        n = max(1, self._n)
        with self._lock:
            self._mode_desc = None
            if ok:
                self._settings.last_mode = desc
                self._device = {"desc": desc, "colors": [_hex(c) for c in colors]}
                self._frame = [colors[i * len(colors) // n] for i in range(n)]
                self._save()  # single save: last_mode is never cleared in between
            else:
                self._device = None
                self._settings.last_mode = None
                self._save()
        if not ok:
            raise ApiError(self._failure_key())
        self._push_state()
        return {"ok": True, "status": self._link.status, "mode": desc}

    def _palette_colors(self) -> list[RGB]:
        with self._lock:
            pals = self._settings.palettes
            colors = pals.get(self._settings.active_palette) or next(iter(pals.values()), [])
        try:
            return _parse_colors(colors)
        except ApiError:
            return [(255, 255, 255)]

    def _segments_packets(self, frame: list[RGB]) -> list[bytes]:
        reverse = self._profile.bit_order == "start_at_end"
        return plan_frame(frame, None, max(1, len(frame)), reverse=reverse)[0]

    def _scene_list(self) -> list[Scene]:
        if self._scenes is None:
            self._scenes = load_scenes()
        return self._scenes

    def _describe_mode(self) -> dict | None:
        cur = self._engine.current
        if cur is None:
            dev = self._device
            if dev is None:
                return None
            return _jsonable({**dev["desc"], "device": True, "colors": dev["colors"]})
        desc = self._mode_desc
        if desc is not None and desc[0] is cur:
            return _jsonable(desc[1])
        if isinstance(cur, EffectMode):
            e = cur.effect
            return {"kind": "effect", "args": {"effect": list(e) if not isinstance(e, str) else e}}
        if isinstance(cur, ScreenSyncMode):
            return {"kind": "screen", "args": {}}
        if isinstance(cur, MusicMode):
            return {"kind": "music", "args": {}}
        return {"kind": "unknown", "args": {}}

    def _push_state(self) -> None:
        if self._push is None:
            return
        try:
            self._push(self.get_state())
        except Exception:
            log.exception("state push failed")

    # ------------------------------------------------------------------ callbacks
    @_internal
    def on_frame(self, frame) -> None:
        """Engine.on_frame (loop thread): remember the frame, push at most ~10/s."""
        self._frame = list(frame)
        now = self._clock()
        if now - self._last_push >= FRAME_PUSH_INTERVAL:
            self._last_push = now
            self._push_state()

    @_internal
    def on_status(self, status: str, message: str | None = None) -> None:
        """DeviceLink.on_status (loop thread): push immediately."""
        self._push_state()

    @_internal
    def on_device_found(self, chosen: FoundDevice, others: list[FoundDevice]) -> None:
        """DeviceLink.on_device (loop thread): auto-discovery picked ``chosen`` -> saved
        (next start connects straight to it); ``others`` are listed in 设置 → 设备."""
        with self._lock:
            self._last_discovery = (chosen, list(others))
            if self._device_ops:
                log.info("discovered %s while the device is being changed: not saved",
                         chosen.address)
                return
            self._settings.device_address = chosen.address
            self._settings.device_name = chosen.name
            self._others = list(others)
            self._save()
        self._push_state()

    def _change_device(self, address: str | None, name: str | None) -> None:
        """Save the user's choice, switch the link, then make the settings agree with
        the address the link actually uses. The link lock orders this after any
        discovery already running, and a held link may rediscover at once (forget)."""
        with self._lock:
            self._device_ops += 1
            self._settings.device_address = address
            self._settings.device_name = name if address else None
            self._others = ([d for d in self._others if d.address != address]
                            if address else [])
            self._save()  # kept even if the switch below times out
        try:
            self._call(self._link.set_address(address), RUN_TIMEOUT)
        finally:
            with self._lock:
                self._device_ops -= 1
        self._adopt_link_device(address, name)

    def _adopt_link_device(self, wanted: str | None, name: str | None) -> None:
        actual = getattr(self._link, "address", wanted)
        with self._lock:
            if actual == wanted:
                self._settings.device_address = wanted
                self._settings.device_name = name if wanted else None
            else:  # forget on a held link: it rediscovered a strip during the switch
                last = self._last_discovery
                found = last[0] if last and last[0].address == actual else None
                self._settings.device_address = actual
                self._settings.device_name = found.name if found else None
                self._others = list(last[1]) if found else []
            self._save()

    @_internal
    def restore_last_mode(self) -> bool:
        """Re-start settings.last_mode (``--tray`` start). Invalid/stale -> logged, False."""
        with self._lock:
            lm = self._settings.last_mode
        if lm is None:
            return False
        try:
            if not isinstance(lm, dict) or not isinstance(lm.get("args", {}), dict):
                raise ValueError("malformed")
            kind, args = lm.get("kind"), lm.get("args", {})
            starters = {"effect": lambda: self.start_effect(**args),
                        "preset": lambda: self.apply_preset(**args),
                        "screen": lambda: self.start_screen_sync(args),
                        "music": lambda: self.start_music(args)}
            if kind not in starters:
                raise ValueError(f"unknown kind {kind!r}")
            r = starters[kind]()
        except Exception as e:
            log.warning("ignoring invalid last_mode %r: %s", lm, e)
            return False
        if not r.get("ok"):
            log.warning("could not restore last_mode %r: %s", lm, r.get("error"))
            return False
        log.info("restored last_mode %r", lm)
        return True

    @_internal
    def set_window_visible(self, visible: bool) -> None:
        """Window shown -> keep the link 60 s after the last command; hidden -> 3 s.
        Any thread; applied on the loop thread. Never connects."""
        seconds = VISIBLE_LINGER_S if visible else HIDDEN_LINGER_S
        try:
            self._loop.call_soon_threadsafe(self._link.set_linger, seconds)
        except RuntimeError:
            pass  # loop closed (shutting down)

    @_internal
    def shutdown(self, timeout: float = SHUTDOWN_TIMEOUT) -> None:
        """Stop the engine and close the link (keeps last_mode for the next start).

        Runs on the GUI thread while the window closes, so it gets one ``timeout`` in
        total; whatever is still running then (e.g. a BLE connect) is abandoned -- the
        process exits right after and Windows drops the connection."""
        end = time.monotonic() + timeout
        for name, coro_fn in (("engine.stop", self._engine.stop), ("link.close", self._link.close)):
            remaining = end - time.monotonic()
            if remaining <= 0:
                log.warning("shutdown deadline reached; skipping %s", name)
                continue
            fut = asyncio.run_coroutine_threadsafe(coro_fn(), self._loop)
            try:
                fut.result(timeout=remaining)
            except concurrent.futures.TimeoutError:
                log.warning("%s did not finish within the shutdown deadline", name)
            except Exception:
                log.exception("%s failed during shutdown", name)

    def _apply_transition(self) -> None:
        """过渡方式 -> engine per-segment interval + the global colour-mixing space.
        Both are read every frame, so a running mode picks them up without a restart."""
        with self._lock:
            tr = {**default_transition(), **(self._settings.transition or {})}
        rate = tr["rate"] if tr["rate"] in TRANSITION_RATES else default_transition()["rate"]
        mix = tr["mix"] if tr["mix"] in TRANSITION_MIXES else "linear"
        self._engine.min_segment_interval = 1.0 / rate
        effects_mod.set_mix(mix)

    def _apply_color_cal(self) -> None:
        """settings.color_cal -> protocol.calibrate (the choke point for every colour
        on the wire). The planner keeps intended colours, so the UI preview does too."""
        with self._lock:
            cal = clamp_color_cal(self._settings.color_cal)
        protocol.set_color_cal(cal["r"], cal["g"], cal["b"])

    # ------------------------------------------------------------------ language
    def get_language(self) -> dict:
        """``{"setting": "auto"|"zh"|"en", "resolved": "zh"|"en"}`` -- the web UI and
        the Python side (tray, errors) use the same resolved language."""
        return self._language_info()

    @_command
    def set_language(self, setting) -> dict:
        """Language setting ``"auto" | "zh" | "en"``: saved and applied at once (the UI
        re-renders; language listeners -- tray, window title -- are told)."""
        if not isinstance(setting, str) or setting not in i18n.SETTINGS:
            raise ApiError("err.language")
        with self._lock:
            self._settings.language = setting
            self._save()
        info = self._language_info(setting)
        for fn in list(self._language_listeners):
            try:
                fn(info["resolved"])
            except Exception:
                log.exception("language listener failed")
        self._push_state()
        return {"ok": True, **info}

    @_internal
    def language(self) -> str:
        """Resolved language for Python-side text (tray, window title)."""
        return self._lang()

    @_internal
    def add_language_listener(self, fn: Callable[[str], None]) -> None:
        """``fn(resolved)`` after the user switches the language."""
        self._language_listeners.append(fn)

    # ------------------------------------------------------------------ device (设置 → 设备)
    @_command
    def scan_devices(self) -> dict:
        """搜索设备: H6125 strips nearby, strongest signal first. (A strip the PC is
        connected to does not advertise, so the current one may be missing.)"""
        if self._discover is None:
            raise ApiError("err.scan_unavailable")
        try:
            devices = list(self._call(self._discover(), SCAN_TIMEOUT))
        except ApiError:
            raise
        except Exception as e:
            if classify_connect_error(e) == "bt_unavailable":
                raise ApiError("link.bt_unavailable") from None
            raise
        with self._lock:
            self._scanned = {d.address: d for d in devices}
            current = self._settings.device_address
        return {"ok": True, "devices": [d.to_dict() for d in devices], "current": current}

    @_command
    def select_device(self, address, name=None) -> dict:
        """Use this strip from now on (saved); the link reconnects to it."""
        addr = normalise_address(address)
        if addr is None:
            raise ApiError("err.bad_address", value=address)
        with self._lock:
            if not isinstance(name, str) or not name.strip():
                found = self._scanned.get(addr)
                name = found.name if found else None
        name = name.strip() if isinstance(name, str) else None
        self._change_device(addr, name)
        self._push_state()
        return {"ok": True, "device": {"address": addr, "name": name},
                "status": self._link.status}

    @_command
    def forget_device(self) -> dict:
        """忘记设备: clear the saved strip; the next connect scans again."""
        self._change_device(None, None)
        self._push_state()
        return {"ok": True, "status": self._link.status}

    # ------------------------------------------------------------------ power sync
    @_internal
    def power_sync_enabled(self) -> bool:
        with self._lock:
            return bool(self._settings.power_sync)

    def _power_keep_mode(self, on: bool, timeout: float) -> bool:
        """Stop any mode and switch the strip on/off, keeping ``last_mode`` (so resume /
        the next login restores it). Bounded by ``timeout``; a call still running then
        is left to finish on the loop (cancelling mid-connect can strand the link)."""
        fut = asyncio.run_coroutine_threadsafe(
            self._engine.oneshot([power_packet(on)], "power"), self._loop)
        try:
            ok = bool(fut.result(timeout=timeout))
        except concurrent.futures.TimeoutError:
            log.warning("power %s did not finish within %.1f s", "on" if on else "off", timeout)
            ok = False
        except Exception:
            log.exception("power %s failed", "on" if on else "off")
            ok = False
        with self._lock:
            self._device = None  # an on-device DIY no longer runs (last_mode kept)
        if ok:
            self._power = on
        self._push_state()
        return ok

    @_internal
    def power_off_keep_mode(self, timeout: float) -> bool:
        return self._power_keep_mode(False, timeout)

    @_internal
    def power_on_keep_mode(self, timeout: float) -> bool:
        return self._power_keep_mode(True, timeout)

    # ------------------------------------------------------------------ queries
    def get_state(self) -> dict:
        frame = self._frame
        status = self._link.status
        with self._lock:
            s = dataclasses.asdict(self._settings)
            others = [d.to_dict() for d in self._others]
        return _jsonable({
            "status": status,
            "message": self._link_message(self._lang(s["language"])),
            "device_stale": self._device_stale(),
            "language": self._language_info(s["language"]),
            "power": self._power,
            "brightness": s["brightness"],
            "mode": self._describe_mode(),
            "segment_count": self._n,
            "palettes": s["palettes"],
            "active_palette": s["active_palette"],
            "settings": s,
            "frame": [_hex(c) for c in frame] if frame is not None else None,
            "autostart": bool(s["autostart"]),
            "color_test": self._color_test_running(),
            "profile": dataclasses.asdict(self._profile),
            "device": ({"address": s["device_address"], "name": s["device_name"]}
                       if s["device_address"] else None),
            "device_others": others,
            "version": __version__,
            "data_dir": str(self._path.parent),
        })

    def list_effects(self) -> list[dict]:
        return [{"id": k, "label": v, "device": diy_format.diy_key_for_app_effect(k) is not None}
                for k, v in EFFECTS.items()]

    def list_presets(self) -> list[dict]:
        with self._lock:
            return _jsonable([{"name": k, **v} for k, v in self._settings.diy_presets.items()])

    def list_scenes(self) -> list[dict]:
        out: dict[str, list] = {}
        for s in self._scene_list():
            out.setdefault(s.category, []).append({"id": s.id, "name": s.name, "hint": s.hint})
        return [{"category": c, "scenes": v} for c, v in out.items()]

    def list_monitors(self) -> list[dict]:
        try:
            if self._list_monitors is not None:
                return _jsonable(self._list_monitors())
            from .screen import list_monitors
            return _jsonable(list_monitors())
        except Exception:
            log.exception("list_monitors failed")
            return []

    # ------------------------------------------------------------------ basic control
    @_command
    def set_power(self, on) -> dict:
        on = bool(on)
        self._oneshot([power_packet(on)], "power")
        self._power = on
        self._push_state()
        return {"ok": True, "power": on}

    @_command
    def set_brightness(self, pct) -> dict:
        """Brightness never stops a running mode: it goes straight to the link."""
        pct = int(_number(pct, 0, 100, "err.brightness_range") + 0.5)
        raw = int(pct * self._profile.brightness_max / 100 + 0.5)
        # send first: the light reacting is what matters, the file write is secondary
        ok = self._call(self._link.send_transient([brightness_packet(raw)], key="brightness"),
                        ONESHOT_TIMEOUT)
        with self._lock:
            self._settings.brightness = pct
            self._save()
        if not ok:
            raise ApiError(self._failure_key())
        return {"ok": True, "brightness": pct}

    @_command
    def set_segments(self, colors) -> dict:
        """Static per-segment colours (a shorter list is repeated along the strip)."""
        rgbs = _parse_colors(colors)
        frame = [rgbs[i % len(rgbs)] for i in range(self._n)]
        with self._lock:
            self._settings.segment_colors = [_hex(c) for c in frame]
            self._save()
        self._oneshot(self._segments_packets(frame), "segments")
        self._frame = frame
        self._push_state()
        return {"ok": True, "colors": [_hex(c) for c in frame]}

    @_command
    def fill(self, color) -> dict:
        _parse_color(color)
        return self.set_segments([color])

    # ------------------------------------------------------------------ palettes
    @_command
    def generate_palette(self, style, hue, mood, method, n=5) -> dict:
        n = int(_number(n, 1, max(15, self._n), "err.color_count"))
        hue = _number(hue, -1e6, 1e6, "err.hue")
        try:
            colors = palette_mod.generate(style, hue, mood, method, n)
        except ValueError:
            raise ApiError("err.palette_params") from None
        return {"ok": True, "colors": colors}

    @_command
    def save_palette(self, name, colors) -> dict:
        name = str(name or "").strip()
        if not name:
            raise ApiError("err.palette_name")
        hexes = [_hex(c) for c in _parse_colors(colors)]
        with self._lock:
            self._settings.palettes[name] = hexes
            self._save()
            return {"ok": True, "palettes": _jsonable(self._settings.palettes)}

    @_command
    def delete_palette(self, name) -> dict:
        with self._lock:
            pals = self._settings.palettes
            if name not in pals:
                raise ApiError("err.no_palette", name=name)
            if len(pals) <= 1:
                raise ApiError("err.keep_palette")
            del pals[name]
            if self._settings.active_palette == name:
                self._settings.active_palette = next(iter(pals))
            self._save()
            return {"ok": True, "palettes": _jsonable(pals),
                    "active_palette": self._settings.active_palette}

    @_command
    def set_active_palette(self, name) -> dict:
        with self._lock:
            if name not in self._settings.palettes:
                raise ApiError("err.no_palette", name=name)
            self._settings.active_palette = name
            self._save()
        return {"ok": True, "active_palette": name}

    # ------------------------------------------------------------------ effects / presets
    @_command
    def start_effect(self, effect, speed_pct, colors=None, width=None) -> dict:
        """``effect``: one effect id, or a list of ids = 混合 (mix; one element = simple)."""
        if isinstance(effect, (list, tuple)):
            effect = self._check_effects(effect)
            effect = effect[0] if len(effect) == 1 else effect
        elif not isinstance(effect, str) or effect not in EFFECTS:
            raise ApiError("err.unknown_effect", effect=effect)
        speed_pct = int(_number(speed_pct, 0, 100, "err.speed_range") + 0.5)
        pal = _parse_colors(colors) if colors is not None else self._palette_colors()
        if width is not None:
            width = _number(width, 0.01, 1000, "err.width")
        args = {"effect": effect, "speed_pct": speed_pct,
                "colors": [_hex(c) for c in pal] if colors is not None else None,
                "width": width}
        key = self._device_key(effect, pal)
        if key is not None:
            return self._start_on_device(key, pal, speed_pct, {"kind": "effect", "args": args})
        mode = EffectMode(effect, pal, speed_from_pct(speed_pct), self._n, width)
        return self._start(mode, {"kind": "effect", "args": args})

    @staticmethod
    def _check_effects(effects) -> list[str]:
        if not isinstance(effects, (list, tuple)) or not effects:
            raise ApiError("err.need_effect")
        for e in effects:
            if not isinstance(e, str) or e not in EFFECTS:
                raise ApiError("err.unknown_effect", effect=e)
        return list(effects)

    @staticmethod
    def _check_preset(effects, colors, speed_pct) -> tuple[list[str], list[RGB], int]:
        effects = Api._check_effects(effects)
        rgbs = _parse_colors(colors)
        speed = int(_number(speed_pct, 0, 100, "err.speed_range") + 0.5)
        return effects, rgbs, speed

    @_command
    def save_preset(self, name, effects, colors, speed_pct, width=None) -> dict:
        """``width`` (optional): aurora band width in segments, kept with the preset."""
        name = str(name or "").strip()
        if not name:
            raise ApiError("err.preset_name")
        effects, rgbs, speed = self._check_preset(effects, colors, speed_pct)
        preset = {"effects": effects, "colors": [_hex(c) for c in rgbs], "speed_pct": speed}
        if width is not None:
            preset["width"] = _number(width, 0.01, 1000, "err.width")
        with self._lock:
            self._settings.diy_presets[name] = preset
            self._save()
        return {"ok": True, "presets": self.list_presets()}

    @_command
    def delete_preset(self, name) -> dict:
        with self._lock:
            if name not in self._settings.diy_presets:
                raise ApiError("err.no_preset", name=name)
            del self._settings.diy_presets[name]
            self._save()
        return {"ok": True, "presets": self.list_presets()}

    @_command
    def apply_preset(self, name) -> dict:
        with self._lock:
            p = self._settings.diy_presets.get(name)
        if not isinstance(p, dict):
            raise ApiError("err.no_preset", name=name)
        effects, rgbs, speed = self._check_preset(p.get("effects"), p.get("colors"),
                                                  p.get("speed_pct", 50))
        width = p.get("width")
        if width is not None:
            width = _number(width, 0.01, 1000, "err.width")
        effect = effects if len(effects) > 1 else effects[0]
        desc = {"kind": "preset", "args": {"name": name}}
        key = self._device_key(effect, rgbs)
        if key is not None:
            return self._start_on_device(key, rgbs, speed, desc)
        mode = EffectMode(effect, rgbs, speed_from_pct(speed), self._n, width)
        return self._start(mode, desc)

    # ------------------------------------------------------------------ scenes
    @_command
    def apply_scene(self, scene_id) -> dict:
        try:
            sid = int(scene_id)
        except (TypeError, ValueError):
            raise ApiError("err.no_scene", id=scene_id) from None
        scene = next((s for s in self._scene_list() if s.id == sid), None)
        if scene is None:
            raise ApiError("err.no_scene", id=scene_id)
        self._oneshot(packets_for(scene, self._profile.scene_cmd_version), "scene")
        self._frame = None
        self._push_state()
        return {"ok": True, "scene": {"id": scene.id, "name": scene.name}}

    # ------------------------------------------------------------------ screen / music
    def _screen_options(self, options) -> dict:
        if options is None:
            return {}
        if not isinstance(options, dict):
            raise ApiError("err.screen_params")
        out: dict[str, Any] = {}
        for k in _SCREEN_KEYS:
            if k not in options:
                continue
            v = options[k]
            if k == "monitor":
                out[k] = int(_number(v, 1, 64, "err.monitor"))
            elif k == "saturation":
                out[k] = _number(v, 0, 3, "err.saturation")
            elif k == "transition_s":
                out[k] = round(_number(v, 0.3, 5.0, "err.transition_s"), 2)
            elif k == "gain":
                if not isinstance(v, (list, tuple)) or len(v) != 3:
                    raise ApiError("err.gain")
                out[k] = [_number(g, 0, 3, "err.gain_range") for g in v]
            elif k == "mode":
                if not isinstance(v, str) or v not in _SCREEN_MODES:
                    raise ApiError("err.screen_mode")
                out[k] = v
            elif k == "tempo":
                out[k] = _number(v, 0, 100, "err.tempo")
        return out

    @_command
    def start_screen_sync(self, options=None) -> dict:
        opts = self._screen_options(options)
        with self._lock:
            merged = {**self._settings.screen, **opts}
        mode = ScreenSyncMode(self._n, merged, None)
        with self._lock:
            self._settings.screen = merged
        return self._start(mode, {"kind": "screen", "args": opts})

    @_command
    def start_music(self, options=None) -> dict:
        if options is None:
            options = {}
        if not isinstance(options, dict):
            raise ApiError("err.music_params")
        opts: dict[str, Any] = {}
        with self._lock:
            sensitivity = self._settings.music.get("sensitivity", 1.0)
        if "sensitivity" in options:
            sensitivity = opts["sensitivity"] = _number(options["sensitivity"], 0.1, 5,
                                                        "err.sensitivity")
        if options.get("colors") is not None:
            pal = _parse_colors(options["colors"])
            opts["colors"] = [_hex(c) for c in pal]
        else:
            pal = self._palette_colors()
        mode = MusicMode(self._n, pal, float(sensitivity))
        with self._lock:
            self._settings.music = {**self._settings.music, "sensitivity": float(sensitivity)}
        return self._start(mode, {"kind": "music", "args": opts})

    @_command
    def stop_mode(self) -> dict:
        self._bump_mode()
        self._call(self._engine.stop(), STOP_TIMEOUT)
        with self._lock:
            device, frame = self._device, self._frame
        if device is not None and frame:
            # the strip runs the effect by itself: freeze it to the colours the UI shows
            self._oneshot(self._segments_packets(list(frame)), "segments")
        with self._lock:
            self._clear_mode_locked()
        self._push_state()
        return {"ok": True}

    # ------------------------------------------------------------------ calibration
    @_command
    def calibration_light(self, index) -> dict:
        """Light segment ``index`` white, the rest off (transient; stops any mode)."""
        i = int(_number(index, 0, self._n - 1, "err.segment_index"))
        if i != float(index):
            raise ApiError("err.segment_index")
        frame = [(255, 255, 255) if k == i else (0, 0, 0) for k in range(self._n)]
        self._oneshot(self._segments_packets(frame), "calibration")
        self._frame = frame
        self._push_state()
        return {"ok": True, "index": i}

    @_command
    def save_calibration(self, points) -> dict:
        if not isinstance(points, (list, tuple)) or len(points) != self._n:
            raise ApiError("err.calibration_points", n=self._n)
        clean = []
        for p in points:
            if not isinstance(p, (list, tuple)) or len(p) != 2:
                raise ApiError("err.calibration_format")
            clean.append([_number(p[0], 0, 1, "err.calibration_coord"),
                          _number(p[1], 0, 1, "err.calibration_coord")])
        with self._lock:
            self._settings.calibration = clean
            self._save()
        return {"ok": True}

    # ------------------------------------------------------------------ autostart
    @_command
    def set_autostart(self, on) -> dict:
        if self._set_autostart is None or self._is_autostart is None:
            raise ApiError("err.autostart_unavailable")
        self._set_autostart(bool(on))
        actual = bool(self._is_autostart())
        with self._lock:
            self._settings.autostart = actual
            self._save()
        self._push_state()
        return {"ok": True, "autostart": actual}

    @_command
    def set_power_sync(self, on) -> dict:
        """开关机/睡眠时自动开关灯带."""
        if not isinstance(on, bool):
            raise ApiError("err.power_sync_param")
        with self._lock:
            self._settings.power_sync = on
            self._save()
        self._push_state()
        return {"ok": True, "power_sync": on}

    # ------------------------------------------------------------------ 过渡方式（实验）
    @_command
    def set_transition(self, options) -> dict:
        """``{"rate": 3|5|8|10, "mix": "linear"|"perceptual"}`` (either key may be left
        out). Applies live to every streamed mode."""
        if not isinstance(options, dict):
            raise ApiError("err.transition_params")
        with self._lock:
            tr = {**default_transition(), **(self._settings.transition or {})}
        if "rate" in options:
            rate = options["rate"]
            if (isinstance(rate, bool) or not isinstance(rate, (int, float))
                    or rate not in TRANSITION_RATES):
                raise ApiError("err.transition_rate")
            tr["rate"] = int(rate)
        if "mix" in options:
            mix = options["mix"]
            if not isinstance(mix, str) or mix not in TRANSITION_MIXES:
                raise ApiError("err.transition_mix")
            tr["mix"] = mix
        tr = {"rate": tr["rate"], "mix": tr["mix"]}
        with self._lock:
            self._settings.transition = tr
            self._save()
        self._apply_transition()
        self._push_state()
        return {"ok": True, "transition": dict(tr)}

    # ------------------------------------------------------------------ 颜色校准（白平衡）
    def _color_test_running(self) -> bool:
        test = self._color_test
        return test is not None and self._engine.current is test["mode"]

    @_command
    def get_color_cal(self) -> dict:
        with self._lock:
            cal = clamp_color_cal(self._settings.color_cal)
        return {"ok": True, "color_cal": cal, "testing": self._color_test_running()}

    @_command
    def set_color_cal(self, cal) -> dict:
        """``{"r", "g", "b"}`` percent 0..100 (keys may be left out; ints, or numbers
        rounded to ints). Applies live: running modes re-send through the planner
        (``Engine.recolor``); an on-device DIY effect is not re-uploaded (that blacks the
        strip out) -- the next upload uses the new gains."""
        if not isinstance(cal, dict) or not any(k in cal for k in ("r", "g", "b")):
            raise ApiError("err.color_cal_params")
        with self._lock:
            out = clamp_color_cal(self._settings.color_cal)
        for k in ("r", "g", "b"):
            if k in cal:
                out[k] = int(round(_number(cal[k], 0, 100, "err.color_cal_range")))
        with self._lock:
            self._settings.color_cal = dict(out)
            self._save()
        self._apply_color_cal()
        self._engine.recolor()
        self._push_state()
        return {"ok": True, "color_cal": dict(out)}

    @_command
    def reset_color_cal(self) -> dict:
        """「恢复默认」: 100/100/100."""
        return self.set_color_cal(default_color_cal())

    @_command
    def test_color(self, color) -> dict:
        """Show ``color`` on the whole strip, calibrated (颜色校准 test buttons). Keeps
        last_mode; 「完成」 (end_color_test) restores the mode that ran before."""
        rgb = _parse_color(color)
        with self._lock:
            test = self._color_test
        desc = {"kind": "color_test", "args": {"color": _hex(rgb)}}
        if test is not None and self._engine.current is test["mode"]:
            test["mode"].color = rgb  # the planner sends the change (rate cap holds)
            with self._lock:
                if self._mode_desc is not None and self._mode_desc[0] is test["mode"]:
                    self._mode_desc = (test["mode"], desc)
        else:
            self._bump_mode()
            resume = self._engine.current is not None or self._device is not None
            mode = ColorTestMode(self._n, rgb)
            self._call(self._engine.run(mode), RUN_TIMEOUT)
            with self._lock:
                self._color_test = {"mode": mode, "resume": resume}
                self._device = None  # segment commands replace an on-device effect
                self._mode_desc = (mode, desc)
        self._frame = [rgb] * self._n
        self._push_state()
        out = {"ok": True, "color": _hex(rgb), "status": self._link.status}
        warning = self._trouble_message()
        if warning:
            out["warning"] = warning
        return out

    @_command
    def end_color_test(self) -> dict:
        """「完成」: restore the mode that ran before testing (last_mode); if nothing ran,
        the test colour stays on the strip (the PC lets go of the link)."""
        with self._lock:
            test, self._color_test = self._color_test, None
        if test is None or self._engine.current is not test["mode"]:
            return {"ok": True, "restored": False}  # not testing (another mode took over)
        with self._lock:
            resume = test["resume"] and self._settings.last_mode is not None
        if resume and self.restore_last_mode():
            return {"ok": True, "restored": True}
        if self._engine.current is test["mode"]:
            self._call(self._engine.stop(), STOP_TIMEOUT)
        with self._lock:
            if self._mode_desc is not None and self._mode_desc[0] is test["mode"]:
                self._mode_desc = None
        self._push_state()
        if resume:
            raise ApiError("err.restore_failed")
        return {"ok": True, "restored": False}
