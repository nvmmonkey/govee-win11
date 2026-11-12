"""Strip follows the PC: off at shutdown / logoff / sleep, on again at resume and login.

Two layers:

- ``PowerSync`` -- the policy, driven by plain method calls so it is unit-tested with
  fakes. It talks to the app through four ``actions`` (``Api`` implements them):
  ``power_sync_enabled()``, ``power_off_keep_mode(timeout)``,
  ``power_on_keep_mode(timeout)`` and ``restore_last_mode()``. ``last_mode`` is kept
  across the power-off so resume / the next login restores it.
- ``PowerWindow`` -- the Windows plumbing: a hidden top-level window on its own thread
  (ctypes, no pywin32) whose window procedure turns WM_QUERYENDSESSION /
  WM_ENDSESSION / WM_POWERBROADCAST into ``PowerSync`` calls. It is *not* a
  message-only window (HWND_MESSAGE): those never receive broadcast messages, and
  both the end-session and the power-broadcast messages are sent to top-level
  windows only. It is independent of the pywebview window, so it works while the
  app sits in the tray.

Shutdown: WM_QUERYENDSESSION starts the power-off at once (in the background, so the
query is answered immediately and never vetoed) and puts up a shutdown block reason;
WM_ENDSESSION waits for it, but never longer than ``SHUTDOWN_BOUND`` after it started,
then removes the reason and lets Windows end the process. The pywebview window gets
the same query on the GUI thread and closes the app (``Shell.on_closing``), so that
path calls ``on_app_closing`` first, which joins the same power-off (it detects the
shutdown via GetSystemMetrics(SM_SHUTTINGDOWN) if our window has not been asked yet).

Sleep: PBT_APMSUSPEND powers off synchronously but within ``SUSPEND_BOUND`` (Windows
allows about 2 s). Resume (PBT_APMRESUMEAUTOMATIC, then PBT_APMRESUMESUSPEND if a
user is present -- handled once) powers on with retries (BLE is often not back yet)
and restores ``last_mode``, in the background.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Callable, Protocol, Union

from .i18n import MESSAGES

log = logging.getLogger(__name__)

# s from the shutdown query until we let Windows go on regardless -- the whole close
# path (power-off + engine/link teardown) fits in it. Kept under Windows' default ~5 s
# WaitToKillAppTimeout: a hidden-only app may not be granted more by its block reason.
SHUTDOWN_BOUND = 4.5
SUSPEND_BOUND = 1.8     # s; Windows gives an app about 2 s to handle PBT_APMSUSPEND
POWER_ON_TIMEOUT = 15.0  # per power-on attempt (a BLE connect may time out)
RESUME_DELAY = 2.0      # s after wake before the first attempt (BLE stack coming back)
RETRY_DELAYS = (2.0, 3.0, 5.0, 5.0)  # between power-on attempts
BLOCK_REASON = MESSAGES["zh"]["power.block_reason"]  # default; the app passes its language's

# window messages / power broadcast events
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_POWERBROADCAST = 0x0218
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
SM_SHUTTINGDOWN = 0x2000


class PowerActions(Protocol):
    def power_sync_enabled(self) -> bool: ...

    def power_off_keep_mode(self, timeout: float) -> bool: ...

    def power_on_keep_mode(self, timeout: float) -> bool: ...

    def restore_last_mode(self) -> bool: ...

    # optional: a counter the app bumps whenever the user starts / stops a mode; a wake
    # whose power-on retries are still running then stops instead of overriding it
    # def mode_generation(self) -> int: ...


def _spawn(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True, name="power-sync").start()


def system_shutting_down() -> bool:
    """True while the Windows session is ending (shutdown / restart / logoff)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.WinDLL("user32").GetSystemMetrics(SM_SHUTTINGDOWN))
    except Exception:
        return False


class _Pending:
    """A power-off running on its own thread, with the deadline callers wait until."""

    def __init__(self, fn: Callable[[float], object], bound: float):
        self.done = threading.Event()
        self.deadline = time.monotonic() + bound

        def run():
            try:
                fn(bound)
            except Exception:
                log.exception("power-off failed")
            finally:
                self.done.set()

        threading.Thread(target=run, daemon=True, name="power-off").start()

    def wait(self) -> bool:
        return self.done.wait(max(0.0, self.deadline - time.monotonic()))


class PowerSync:
    def __init__(self, actions: PowerActions, *,
                 shutting_down: Callable[[], bool] = system_shutting_down,
                 sleep: Callable[[float], None] = time.sleep,
                 spawn: Callable[[Callable[[], None]], None] = _spawn,
                 shutdown_bound: float = SHUTDOWN_BOUND,
                 suspend_bound: float = SUSPEND_BOUND,
                 power_on_timeout: float = POWER_ON_TIMEOUT,
                 resume_delay: float = RESUME_DELAY,
                 retry_delays: tuple[float, ...] = RETRY_DELAYS):
        self._actions = actions
        self._shutting_down = shutting_down
        self._sleep = sleep
        self._spawn = spawn
        self._shutdown_bound = shutdown_bound
        self._suspend_bound = suspend_bound
        self._power_on_timeout = power_on_timeout
        self._resume_delay = resume_delay
        self._retry_delays = tuple(retry_delays)
        self._lock = threading.Lock()
        self._off: _Pending | None = None  # shutdown power-off in flight
        self._session_ending = False
        self._suspended = False
        self._app_closed = False  # Shell tore the app down: never restore a mode again

    # ---- helpers ----
    def _enabled(self) -> bool:
        try:
            return bool(self._actions.power_sync_enabled())
        except Exception:
            log.exception("power_sync_enabled failed")
            return False

    def _shutdown_off(self) -> _Pending:
        with self._lock:
            if self._off is None:
                log.info("session ending: turning the strip off")
                self._off = _Pending(self._actions.power_off_keep_mode, self._shutdown_bound)
            return self._off

    def _generation(self) -> int | None:
        fn = getattr(self._actions, "mode_generation", None)
        if fn is None:
            return None
        try:
            return int(fn())
        except Exception:
            log.exception("mode_generation failed")
            return None

    def _power_on(self, gen: int | None = None) -> bool | None:
        """Power on, retrying while BLE is not back. True once it went through; None =
        stopped because the user changed the mode meanwhile (``gen`` = the
        ``mode_generation`` when the wake began) -- a retry would stop their mode."""
        delays = self._retry_delays
        for attempt in range(len(delays) + 1):
            if attempt:
                self._sleep(delays[attempt - 1])
                if gen is not None and self._generation() != gen:
                    log.info("mode changed by the user: no more power-on retries")
                    return None
            try:
                if self._actions.power_on_keep_mode(self._power_on_timeout):
                    return True
            except Exception:
                log.exception("power-on failed")
            log.info("power-on attempt %d failed", attempt + 1)
        return False

    def _restore(self) -> None:
        try:
            self._actions.restore_last_mode()
        except Exception:
            log.exception("restore_last_mode failed")

    def _wake(self, delay: float, gen: int | None) -> None:
        """Power on after ``delay``, then restore last_mode. ``gen`` = the
        ``mode_generation`` read when the wake was triggered (before the delay), so a
        mode the user starts during the delay is not restarted by the restore."""
        if not self._enabled():
            return
        if delay > 0:
            self._sleep(delay)
        ok = self._power_on(gen)
        if ok is None or (gen is not None and self._generation() != gen):
            return  # the user picked a mode meanwhile: leave it alone
        if not ok:
            log.warning("could not power the strip on; restoring the last mode anyway")
        if self._app_closed:
            log.info("app already closed: strip back on, no mode restored")
            return
        self._restore()

    # ---- shutdown / logoff ----
    def on_query_end_session(self) -> bool:
        """WM_QUERYENDSESSION: start the power-off, answer at once. True = work started
        (the window then shows a shutdown block reason until ``on_end_session``)."""
        if not self._enabled():
            return False
        self._session_ending = True
        self._shutdown_off()
        return True

    def on_end_session(self, ending: bool) -> None:
        """WM_ENDSESSION: ``ending`` -> wait (bounded) for the power-off; cancelled ->
        turn the strip back on if it was turned off."""
        if ending:
            if self._off is None and not self._enabled():
                return
            self._shutdown_off().wait()
            return
        with self._lock:
            off, self._off = self._off, None
            self._session_ending = False
        if off is not None:
            log.info("session end cancelled: turning the strip back on")
            gen = self._generation()

            def back_on():
                off.wait()
                self._wake(0.0, gen)

            self._spawn(back_on)

    def on_app_closing(self) -> float | None:
        """The app is about to stop its engine and exit: if that is because the session
        ends, power off first (joins a power-off already started).

        Returns the seconds left of the shutdown bound after that power-off (the caller
        fits the rest of its teardown in it), or None for an ordinary close."""
        self._app_closed = True
        if not self._enabled():
            return None
        if not (self._session_ending or self._off is not None or self._shutting_down()):
            return None
        off = self._shutdown_off()
        off.wait()
        return max(0.0, off.deadline - time.monotonic())

    def wait_pending(self) -> None:
        """Exit path: let a running shutdown power-off finish (bounded)."""
        off = self._off
        if off is not None:
            off.wait()

    # ---- sleep / resume ----
    def on_suspend(self) -> None:
        if not self._enabled():
            return
        with self._lock:
            if self._suspended:
                return
            self._suspended = True
        log.info("suspending: turning the strip off")
        _Pending(self._actions.power_off_keep_mode, self._suspend_bound).wait()

    def on_resume(self) -> None:
        with self._lock:
            if not self._suspended:
                return
            self._suspended = False
        log.info("resumed: turning the strip on")
        gen = self._generation()  # before the resume delay: a mode started in it wins
        self._spawn(lambda: self._wake(self._resume_delay, gen))

    # ---- login ----
    def on_tray_start(self) -> None:
        """``--tray`` start (autostart at login): power on, then restore last_mode.
        Without power sync only the restore runs (as before)."""
        gen = self._generation()
        if self._enabled():
            ok = self._power_on(gen)
            if ok is None:
                return  # the user already started something: keep it
            if not ok:
                log.warning("could not power the strip on at start")
        if gen is not None and self._generation() != gen:
            return
        self._restore()


# ---------------------------------------------------------------------------
# Windows plumbing
# ---------------------------------------------------------------------------

class PowerEvents(Protocol):
    def on_query_end_session(self) -> bool: ...

    def on_end_session(self, ending: bool) -> None: ...

    def on_suspend(self) -> None: ...

    def on_resume(self) -> None: ...


_CLASS_NAME = "GoveeWin11PowerWindow"
_ERROR_CLASS_ALREADY_EXISTS = 1410
_DEVICE_NOTIFY_WINDOW_HANDLE = 0
_WS_EX_TOOLWINDOW = 0x00000080


def _user32():
    """A private user32 handle with full prototypes (64-bit safe). Private so the
    argtypes set here never affect ctypes.windll.user32 used by pystray / pywebview."""
    import ctypes
    from ctypes import wintypes

    u = ctypes.WinDLL("user32", use_last_error=True)
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                 wintypes.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

    proto = {
        "DefWindowProcW": (LRESULT, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                     wintypes.LPARAM]),
        "RegisterClassW": (wintypes.ATOM, [ctypes.POINTER(WNDCLASSW)]),
        "UnregisterClassW": (wintypes.BOOL, [wintypes.LPCWSTR, wintypes.HINSTANCE]),
        "CreateWindowExW": (wintypes.HWND, [wintypes.DWORD, wintypes.LPCWSTR,
                                            wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            wintypes.HWND, wintypes.HMENU,
                                            wintypes.HINSTANCE, wintypes.LPVOID]),
        "DestroyWindow": (wintypes.BOOL, [wintypes.HWND]),
        "GetMessageW": (wintypes.BOOL, [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                        wintypes.UINT, wintypes.UINT]),
        "TranslateMessage": (wintypes.BOOL, [ctypes.POINTER(wintypes.MSG)]),
        "DispatchMessageW": (LRESULT, [ctypes.POINTER(wintypes.MSG)]),
        "PostMessageW": (wintypes.BOOL, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                         wintypes.LPARAM]),
        "SendMessageW": (LRESULT, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                   wintypes.LPARAM]),
        "PostQuitMessage": (None, [ctypes.c_int]),
        "ShutdownBlockReasonCreate": (wintypes.BOOL, [wintypes.HWND, wintypes.LPCWSTR]),
        "ShutdownBlockReasonDestroy": (wintypes.BOOL, [wintypes.HWND]),
        "RegisterSuspendResumeNotification": (wintypes.HANDLE, [wintypes.HANDLE,
                                                                wintypes.DWORD]),
        "UnregisterSuspendResumeNotification": (wintypes.BOOL, [wintypes.HANDLE]),
    }
    for name, (res, args) in proto.items():
        fn = getattr(u, name, None)
        if fn is not None:
            fn.restype, fn.argtypes = res, args
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetModuleHandleW.restype = wintypes.HMODULE
    k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    return ctypes, wintypes, u, k, WNDPROC, WNDCLASSW


class PowerWindow:
    """Hidden top-level window on a dedicated thread that receives the session-end
    and power-broadcast messages and forwards them to ``events``."""

    def __init__(self, events: PowerEvents,
                 block_reason: Union[str, Callable[[], str]] = BLOCK_REASON):
        """``block_reason``: the shutdown block text, or a callable giving it (read at
        shutdown, so it follows a language switched meanwhile)."""
        self._events = events
        self._reason = block_reason
        self.hwnd: int | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._blocking = False
        self._wndproc = None  # keep the ctypes callback alive

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _reason_text(self) -> str:
        r = self._reason
        if callable(r):
            try:
                r = r()
            except Exception:
                log.exception("block reason failed")
                r = BLOCK_REASON
        return str(r or BLOCK_REASON)

    def start(self, timeout: float = 5.0) -> bool:
        """Create the window on its own thread. False if that failed (logged)."""
        if sys.platform != "win32":
            return False
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="power-window")
        self._thread.start()
        self._ready.wait(timeout)
        return self.hwnd is not None

    def stop(self, timeout: float = 2.0) -> None:
        hwnd, t = self.hwnd, self._thread
        if hwnd is not None:
            try:
                _, _, u, *_ = _user32()
                u.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except Exception:
                log.exception("power window: close failed")
        if t is not None:
            t.join(timeout)

    def send(self, msg: int, wparam: int = 0, lparam: int = 0) -> int:
        """SendMessage to this window (tests / diagnostics)."""
        _, _, u, *_ = _user32()
        return int(u.SendMessageW(self.hwnd, msg, wparam, lparam))

    # ---- window thread ----
    def _handle(self, u, hwnd, msg, wparam, lparam) -> int | None:
        ev = self._events
        if msg == WM_QUERYENDSESSION:
            try:
                if ev.on_query_end_session() and not self._blocking:
                    self._blocking = bool(u.ShutdownBlockReasonCreate(hwnd, self._reason_text()))
            except Exception:
                log.exception("power window: query-end-session handler failed")
            return 1  # never veto the shutdown
        if msg == WM_ENDSESSION:
            try:
                ev.on_end_session(bool(wparam))
            except Exception:
                log.exception("power window: end-session handler failed")
            finally:
                if self._blocking:
                    u.ShutdownBlockReasonDestroy(hwnd)
                    self._blocking = False
            return 0
        if msg == WM_POWERBROADCAST:
            try:
                if wparam == PBT_APMSUSPEND:
                    ev.on_suspend()
                elif wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                    ev.on_resume()
            except Exception:
                log.exception("power window: power-broadcast handler failed")
            return 1
        if msg == WM_CLOSE:
            u.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            u.PostQuitMessage(0)
            return 0
        return None

    def _run(self) -> None:
        notify = None
        hinst = None
        registered = False
        try:
            ctypes, wintypes, u, k, WNDPROC, WNDCLASSW = _user32()
            hinst = k.GetModuleHandleW(None)

            def wndproc(hwnd, msg, wparam, lparam):
                try:
                    r = self._handle(u, hwnd, msg, wparam, lparam)
                except Exception:  # never let an exception unwind into user32
                    log.exception("power window: wndproc failed")
                    r = None
                return r if r is not None else u.DefWindowProcW(hwnd, msg, wparam, lparam)

            self._wndproc = WNDPROC(wndproc)
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = _CLASS_NAME
            if not u.RegisterClassW(ctypes.byref(wc)):
                err = ctypes.get_last_error()
                if err != _ERROR_CLASS_ALREADY_EXISTS:
                    raise OSError(err, "RegisterClassW failed")
                # left over from a previous window in this process: re-register ours
                u.UnregisterClassW(_CLASS_NAME, hinst)
                if not u.RegisterClassW(ctypes.byref(wc)):
                    raise OSError(ctypes.get_last_error(), "RegisterClassW failed")
            registered = True
            hwnd = u.CreateWindowExW(_WS_EX_TOOLWINDOW, _CLASS_NAME, "GoveeWin11 power", 0,
                                     0, 0, 0, 0, None, None, hinst, None)
            if not hwnd:
                raise OSError(ctypes.get_last_error(), "CreateWindowExW failed")
            self.hwnd = hwnd
            # Modern Standby systems deliver suspend/resume to registered windows
            # (harmless duplicate elsewhere: PowerSync handles each transition once)
            if getattr(u, "RegisterSuspendResumeNotification", None) is not None:
                notify = u.RegisterSuspendResumeNotification(hwnd,
                                                             _DEVICE_NOTIFY_WINDOW_HANDLE)
            self._ready.set()
            msg = wintypes.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            log.exception("power window failed")
        finally:
            self.hwnd = None
            try:
                if notify:
                    u.UnregisterSuspendResumeNotification(notify)
                if registered:
                    u.UnregisterClassW(_CLASS_NAME, hinst)
            except Exception:
                log.debug("power window cleanup failed", exc_info=True)
            self._ready.set()
