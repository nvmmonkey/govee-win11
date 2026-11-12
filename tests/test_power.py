"""Power lifecycle (Task 17 part 1): shutdown / sleep / resume / --tray start.

PowerSync is driven with fake actions (no BLE, no real Windows messages); the real
PowerWindow only gets a smoke test that it starts, routes messages sent to itself and
stops."""
import sys
import threading
import time

import pytest

from govee_win11 import power
from govee_win11.power import PowerSync


class FakeActions:
    def __init__(self, enabled=True, off_delay=0.0, on_results=None):
        self.enabled = enabled
        self.off_delay = off_delay
        self.on_results = list(on_results or [])
        self.calls = []
        self.off_timeouts = []
        self.lock = threading.Lock()
        self.off_done = threading.Event()

    def power_sync_enabled(self):
        return self.enabled

    def power_off_keep_mode(self, timeout):
        with self.lock:
            self.calls.append("off")
            self.off_timeouts.append(timeout)
        time.sleep(self.off_delay)
        self.off_done.set()
        return True

    def power_on_keep_mode(self, timeout):
        with self.lock:
            self.calls.append("on")
        return self.on_results.pop(0) if self.on_results else True

    def restore_last_mode(self):
        with self.lock:
            self.calls.append("restore")
        return True


def make_sync(actions, **kw):
    kw.setdefault("shutting_down", lambda: False)
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("spawn", lambda fn: fn())  # run background work inline
    return PowerSync(actions, **kw)


# ---- shutdown / logoff ----------------------------------------------------------

def test_shutdown_sends_power_off_before_session_ends():
    a = FakeActions()
    ps = make_sync(a, shutdown_bound=1.0)
    assert ps.on_query_end_session() is True   # caller creates the block reason
    ps.on_end_session(True)
    assert a.calls == ["off"]
    assert a.off_done.is_set()
    assert 0 < a.off_timeouts[0] <= 1.0


def test_shutdown_is_bounded_even_if_the_light_hangs():
    a = FakeActions(off_delay=5.0)
    ps = make_sync(a, shutdown_bound=0.3)
    t0 = time.monotonic()
    ps.on_query_end_session()
    ps.on_end_session(True)
    assert time.monotonic() - t0 < 1.0
    assert a.calls == ["off"]


def test_default_shutdown_bound_is_short():
    assert 0 < power.SHUTDOWN_BOUND <= 6.0
    assert 0 < power.SUSPEND_BOUND <= 2.0


def test_shutdown_power_off_runs_once_for_all_callers():
    a = FakeActions(off_delay=0.05)
    ps = make_sync(a, shutdown_bound=1.0)
    ps.on_query_end_session()
    ps.on_app_closing()          # the pywebview window closes during the same shutdown
    ps.on_end_session(True)
    ps.wait_pending()
    assert a.calls == ["off"]


def test_end_session_without_query_still_powers_off():
    a = FakeActions()
    ps = make_sync(a, shutdown_bound=1.0)
    ps.on_end_session(True)       # forced shutdown: no WM_QUERYENDSESSION
    assert a.calls == ["off"]


def test_cancelled_shutdown_turns_the_light_back_on():
    a = FakeActions()
    ps = make_sync(a, shutdown_bound=1.0)
    ps.on_query_end_session()
    ps.on_end_session(False)      # another app vetoed the shutdown
    assert a.calls == ["off", "on", "restore"]


def test_window_close_during_system_shutdown_powers_off_first():
    a = FakeActions()
    ps = make_sync(a, shutdown_bound=1.0, shutting_down=lambda: True)
    ps.on_app_closing()           # pywebview got WM_QUERYENDSESSION before our window
    assert a.calls == ["off"]


def test_normal_window_close_does_not_power_off():
    a = FakeActions()
    ps = make_sync(a, shutting_down=lambda: False)
    ps.on_app_closing()
    ps.wait_pending()
    assert a.calls == []


# ---- sleep / resume ----------------------------------------------------------------

def test_suspend_powers_off_resume_powers_on_and_restores_once():
    a = FakeActions()
    ps = make_sync(a, suspend_bound=1.0)
    ps.on_suspend()
    assert a.calls == ["off"]
    assert 0 < a.off_timeouts[0] <= 1.0
    ps.on_resume()                # PBT_APMRESUMEAUTOMATIC
    ps.on_resume()                # PBT_APMRESUMESUSPEND for the same wake
    assert a.calls == ["off", "on", "restore"]


def test_suspend_is_bounded():
    a = FakeActions(off_delay=5.0)
    ps = make_sync(a, suspend_bound=0.2)
    t0 = time.monotonic()
    ps.on_suspend()
    assert time.monotonic() - t0 < 0.8


def test_resume_without_suspend_does_nothing():
    a = FakeActions()
    ps = make_sync(a)
    ps.on_resume()
    assert a.calls == []


def test_duplicate_suspend_is_ignored():
    a = FakeActions()
    ps = make_sync(a)
    ps.on_suspend()
    ps.on_suspend()
    assert a.calls == ["off"]


def test_resume_retries_power_on_then_restores():
    slept = []
    a = FakeActions(on_results=[False, False, True])
    ps = make_sync(a, sleep=slept.append, resume_delay=2.0, retry_delays=(1.0, 2.0, 3.0))
    ps.on_suspend()
    ps.on_resume()
    assert a.calls == ["off", "on", "on", "on", "restore"]
    assert slept == [2.0, 1.0, 2.0]   # wait for BLE after wake, then back off


def test_resume_restores_even_if_power_on_keeps_failing():
    a = FakeActions(on_results=[False] * 10)
    ps = make_sync(a, retry_delays=(0.0, 0.0))
    ps.on_suspend()
    ps.on_resume()
    assert a.calls == ["off", "on", "on", "on", "restore"]


def test_resume_runs_in_background():
    a = FakeActions()
    started = []
    ps = make_sync(a, spawn=lambda fn: started.append(fn))
    ps.on_suspend()
    ps.on_resume()
    assert a.calls == ["off"]          # the message handler returns at once
    started[0]()
    assert a.calls == ["off", "on", "restore"]


# ---- --tray start (autostart at login) ---------------------------------------------

def test_tray_start_powers_on_then_restores():
    a = FakeActions()
    make_sync(a).on_tray_start()
    assert a.calls == ["on", "restore"]


def test_tray_start_without_power_sync_only_restores():
    a = FakeActions(enabled=False)
    make_sync(a).on_tray_start()
    assert a.calls == ["restore"]      # as before Task 17


# ---- power_sync off: nothing --------------------------------------------------------

def test_power_sync_off_ignores_everything():
    a = FakeActions(enabled=False)
    ps = make_sync(a, shutting_down=lambda: True)
    assert ps.on_query_end_session() is False
    ps.on_end_session(True)
    ps.on_app_closing()
    ps.on_suspend()
    ps.on_resume()
    ps.wait_pending()
    assert a.calls == []


def test_action_errors_never_escape_handlers():
    class Broken(FakeActions):
        def power_off_keep_mode(self, timeout):
            raise RuntimeError("ble gone")

        def power_on_keep_mode(self, timeout):
            raise RuntimeError("ble gone")

    a = Broken()
    ps = make_sync(a, retry_delays=())
    ps.on_suspend()
    ps.on_resume()
    ps.on_query_end_session()
    ps.on_end_session(True)
    assert a.calls == ["restore"]


# ---- real message window (smoke) -------------------------------------------------

class RecordingEvents:
    def __init__(self):
        self.calls = []

    def on_query_end_session(self):
        self.calls.append("query")
        return False

    def on_end_session(self, ending):
        self.calls.append(("end", ending))

    def on_suspend(self):
        self.calls.append("suspend")

    def on_resume(self):
        self.calls.append("resume")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_power_window_starts_routes_messages_and_stops():
    ev = RecordingEvents()
    w = power.PowerWindow(ev)
    assert w.start(timeout=5.0) is True
    try:
        assert w.hwnd
        # messages sent to our own window (not a real shutdown / sleep)
        assert w.send(power.WM_POWERBROADCAST, power.PBT_APMSUSPEND) == 1
        w.send(power.WM_POWERBROADCAST, power.PBT_APMRESUMEAUTOMATIC)
        w.send(power.WM_POWERBROADCAST, power.PBT_APMRESUMESUSPEND)
        assert w.send(power.WM_QUERYENDSESSION, 0) == 1   # never vetoes the shutdown
        w.send(power.WM_ENDSESSION, 0)                      # cancelled
        assert ev.calls == ["suspend", "resume", "resume", "query", ("end", False)]
    finally:
        w.stop(timeout=5.0)
    assert not w.running
    # can be started again in the same process (window class re-registered)
    w2 = power.PowerWindow(RecordingEvents())
    assert w2.start(timeout=5.0) is True
    w2.stop(timeout=5.0)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_system_shutting_down_is_false_now():
    assert power.system_shutting_down() is False


# ---- review fix round 1 ------------------------------------------------------------

def test_app_closing_returns_time_left_of_the_bound():
    a = FakeActions(off_delay=0.05)
    ps = make_sync(a, shutdown_bound=1.0)
    ps.on_query_end_session()
    left = ps.on_app_closing()
    assert 0.8 < left <= 1.0              # power-off finished early: most of it left
    assert make_sync(FakeActions()).on_app_closing() is None   # ordinary close


def test_app_closing_with_hanging_power_off_returns_nothing_left():
    a = FakeActions(off_delay=2.0)
    ps = make_sync(a, shutdown_bound=0.3)
    t0 = time.monotonic()
    left = ps.on_app_closing() if ps.on_query_end_session() else None
    assert time.monotonic() - t0 < 1.0 and 0.0 <= left < 0.05   # timer granularity


def test_cancelled_shutdown_after_app_closed_powers_on_without_restoring():
    a = FakeActions()
    ps = make_sync(a, shutdown_bound=1.0)
    ps.on_query_end_session()
    ps.on_app_closing()           # pywebview's window already tore the app down
    ps.on_end_session(False)      # ...then another app vetoed the shutdown
    assert a.calls == ["off", "on"]


class GenActions(FakeActions):
    """Fake whose user starts a mode while power-on attempts fail."""

    def __init__(self, change_after_attempt, **kw):
        super().__init__(**kw)
        self.gen = 0
        self.change_after = change_after_attempt
        self.attempts = 0

    def mode_generation(self):
        return self.gen

    def power_on_keep_mode(self, timeout):
        self.attempts += 1
        r = super().power_on_keep_mode(timeout)
        if self.attempts == self.change_after:
            self.gen += 1                 # user starts an effect in the window
        return r


def test_resume_retries_stop_when_the_user_starts_a_mode():
    a = GenActions(change_after_attempt=1, on_results=[False] * 10)
    ps = make_sync(a, retry_delays=(0.0, 0.0, 0.0))
    ps.on_suspend()
    ps.on_resume()
    assert a.calls == ["off", "on"]       # no further retry, no restore over their mode


def test_resume_does_not_restore_over_a_mode_started_during_power_on():
    a = GenActions(change_after_attempt=1, on_results=[True])
    ps = make_sync(a)
    ps.on_suspend()
    ps.on_resume()
    assert a.calls == ["off", "on"]


def test_tray_start_retries_stop_when_the_user_starts_a_mode():
    a = GenActions(change_after_attempt=2, on_results=[False] * 10)
    make_sync(a, retry_delays=(0.0, 0.0, 0.0)).on_tray_start()
    assert a.calls == ["on", "on"]


def test_unchanged_generation_still_retries_and_restores():
    a = GenActions(change_after_attempt=99, on_results=[False, True])
    ps = make_sync(a, retry_delays=(0.0,))
    ps.on_suspend()
    ps.on_resume()
    assert a.calls == ["off", "on", "on", "restore"]


def test_mode_started_during_the_resume_delay_is_not_restarted():
    """The generation baseline is taken when the resume arrives, not after the delay:
    a mode the user starts while BLE is coming back must not be restarted (a blink)."""
    a = GenActions(change_after_attempt=99, on_results=[True])
    delays = []

    def sleep(s):
        delays.append(s)
        a.gen += 1                        # user starts an effect during the delay

    ps = make_sync(a, sleep=sleep, resume_delay=2.0)
    ps.on_suspend()
    ps.on_resume()
    assert delays == [2.0]
    assert a.calls == ["off", "on"]       # strip back on, no restore over their mode
