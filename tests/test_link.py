import asyncio

import pytest
from bleak.exc import BleakDeviceNotFoundError

from govee_win11.link import BUSY_MESSAGE, DeviceLink


class FakeClient:
    instances: list = []
    connect_errors: list = []   # popped one per connect call (None = ok)
    write_errors: list = []     # popped one per write call (None = ok)
    connect_calls = 0
    writes: list = []
    disconnects = 0

    def __init__(self, address):
        self.address = address
        self.is_connected = False

    async def connect(self):
        FakeClient.connect_calls += 1
        if FakeClient.connect_errors:
            err = FakeClient.connect_errors.pop(0)
            if err:
                raise err
        self.is_connected = True

    async def disconnect(self):
        FakeClient.disconnects += 1
        self.is_connected = False

    async def write_gatt_char(self, char, data, response=False):
        if FakeClient.write_errors:
            err = FakeClient.write_errors.pop(0)
            if err:
                raise err
        FakeClient.writes.append(bytes(data))


@pytest.fixture(autouse=True)
def reset():
    FakeClient.connect_errors = []
    FakeClient.write_errors = []
    FakeClient.connect_calls = 0
    FakeClient.writes = []
    FakeClient.disconnects = 0


def make(statuses=None, **kw):
    cb = (lambda s, m: statuses.append((s, m))) if statuses is not None else (lambda s, m: None)
    return DeviceLink("AA", client_factory=FakeClient, linger_s=0.05, on_status=cb, **kw)


async def test_transient_connects_sends_then_disconnects_after_linger():
    link = make()
    assert await link.send_transient([b"\x01", b"\x02"]) is True
    assert FakeClient.writes == [b"\x01", b"\x02"]
    assert link.status == "connected"
    await asyncio.sleep(0.15)
    assert FakeClient.disconnects == 1
    assert link.status == "disconnected"


async def test_transient_coalesces_by_key():
    link = make()
    tasks = [asyncio.create_task(link.send_transient([bytes([i])], key="bright")) for i in range(50)]
    results = await asyncio.gather(*tasks)
    assert FakeClient.writes == [bytes([49])]
    assert FakeClient.connect_calls == 1
    assert all(results)
    await link.close()


async def test_busy_elsewhere_status_and_recovery():
    statuses = []
    link = make(statuses)
    FakeClient.connect_errors = [BleakDeviceNotFoundError("AA")]
    assert await link.send_transient([b"\x01"]) is False
    assert link.status == "busy_elsewhere"
    assert ("busy_elsewhere", BUSY_MESSAGE) in statuses
    assert BUSY_MESSAGE == "连不上灯带：可能被手机占用，请关闭手机蓝牙或 Govee App"
    assert await link.send_transient([b"\x02"]) is True
    assert link.status == "connected"
    await link.close()


async def test_hold_keeps_connection_past_linger():
    link = make()
    await link.hold()
    assert link.status == "connected"
    await asyncio.sleep(0.15)
    assert FakeClient.disconnects == 0
    assert link.status == "connected"
    assert await link.send([b"\x01"]) is True
    await link.release()
    await asyncio.sleep(0.15)
    assert FakeClient.disconnects == 1
    assert link.status == "disconnected"


async def test_send_reconnects_after_drop():
    link = make(backoff=(0.01, 0.02))
    await link.hold()
    FakeClient.write_errors = [OSError("dropped")]
    assert await link.send([b"\x01"]) is False
    FakeClient.connect_errors = [BleakDeviceNotFoundError("AA"), None]
    for _ in range(100):
        await asyncio.sleep(0.01)
        if link.status == "connected" and FakeClient.connect_calls >= 3:
            break
    assert link.status == "connected"
    assert await link.send([b"\x02"]) is True
    assert FakeClient.writes == [b"\x02"]
    await link.close()


async def test_cancelled_drain_does_not_hang_other_callers():
    gate = asyncio.Event()
    orig = FakeClient.write_gatt_char

    async def slow(self, char, data, response=False):
        await gate.wait()
        return await orig(self, char, data, response)

    FakeClient.write_gatt_char = slow
    try:
        link = make()
        a = asyncio.create_task(link.send_transient([b"\x01"], key="a"))
        b = asyncio.create_task(link.send_transient([b"\x02"], key="b"))
        await asyncio.sleep(0.01)
        a.cancel()
        gate.set()
        assert await asyncio.wait_for(b, 1) in (True, False)
        await asyncio.sleep(0.15)
        assert link.status == "disconnected"  # linger still scheduled
    finally:
        FakeClient.write_gatt_char = orig


async def test_linger_not_cancelled_mid_disconnect():
    started, done = asyncio.Event(), asyncio.Event()
    orig = FakeClient.disconnect

    async def slow_disc(self):
        started.set()
        await asyncio.sleep(0.05)
        await orig(self)
        done.set()

    FakeClient.disconnect = slow_disc
    try:
        link = make()
        assert await link.send_transient([b"\x01"]) is True
        await asyncio.wait_for(started.wait(), 1)
        assert await link.send_transient([b"\x02"]) is True
        assert done.is_set()
        assert link.status == "connected"
        assert FakeClient.connect_calls == 2
    finally:
        FakeClient.disconnect = orig
    await link.close()


async def test_busy_status_persists_after_linger():
    statuses = []
    link = make(statuses)
    FakeClient.connect_errors = [BleakDeviceNotFoundError("AA")]
    assert await link.send_transient([b"\x01"]) is False
    await asyncio.sleep(0.15)
    assert link.status == "busy_elsewhere"
    assert statuses[-1] == ("busy_elsewhere", BUSY_MESSAGE)


async def test_release_during_slow_failing_reconnect_resets_status():
    """Phone holds the strip: every connect fails after a while. Stopping the mode
    while a reconnect is mid-connect must not leave the status on 'connecting'."""
    class SlowFailingClient(FakeClient):
        async def connect(self):
            FakeClient.connect_calls += 1
            await asyncio.sleep(0.2)
            raise BleakDeviceNotFoundError("AA")

    statuses = []
    link = DeviceLink("AA", client_factory=SlowFailingClient, linger_s=0.05,
                      on_status=lambda s, m: statuses.append(s), backoff=(0.01,))
    await link.hold()                       # first attempt fails -> busy, reconnect loop
    assert link.status == "busy_elsewhere"
    for _ in range(200):                    # wait until the reconnect is inside connect()
        await asyncio.sleep(0.005)
        if link.status == "connecting":
            break
    assert link.status == "connecting"
    await link.release()
    await asyncio.sleep(0.1)                # linger runs with no client
    assert link.status == "disconnected"
    assert statuses[-1] == "disconnected"


async def test_connect_cancelled_from_outside_resets_status():
    class Hanging(FakeClient):
        async def connect(self):
            await asyncio.sleep(10)

    link = DeviceLink("AA", client_factory=Hanging, linger_s=0.05)
    task = asyncio.ensure_future(link.send_transient([b"x"]))
    await asyncio.sleep(0.02)
    assert link.status == "connecting"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert link.status == "disconnected"


async def test_set_linger_long_keeps_connection_and_command_resets_timer():
    link = make()
    link.set_linger(0.2)
    await link.send_transient([b""])
    await asyncio.sleep(0.12)
    assert FakeClient.disconnects == 0
    await link.send_transient([b""])   # resets the timer
    await asyncio.sleep(0.12)
    assert FakeClient.disconnects == 0
    assert FakeClient.connect_calls == 1
    await asyncio.sleep(0.15)
    assert FakeClient.disconnects == 1


async def test_set_linger_short_reschedules_pending_linger():
    link = make()
    link.set_linger(5.0)
    await link.send_transient([b""])
    link.set_linger(0.05)                    # window minimised
    await asyncio.sleep(0.15)
    assert FakeClient.disconnects == 1
    assert link.status == "disconnected"


async def test_set_linger_never_connects_and_respects_hold():
    link = make()
    link.set_linger(0.05)
    await asyncio.sleep(0.1)
    assert FakeClient.connect_calls == 0
    await link.hold()
    link.set_linger(0.05)
    await asyncio.sleep(0.15)
    assert FakeClient.disconnects == 0
    assert link.status == "connected"
    await link.close()


async def test_paced_upload_gaps_before_each_packet_and_longer_before_last():
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    link = make(pace_sleep=fake_sleep)
    assert await link.send_transient([b"a", b"b", b"c", b"d"], paced=True) is True
    assert FakeClient.writes == [b"a", b"b", b"c", b"d"]
    assert sleeps == [0.04, 0.04, 0.15]
    sleeps.clear()
    assert await link.send_transient([b"x", b"y"]) is True  # unpaced: no gaps
    assert sleeps == []
    await link.close()


# ---- Task 18: the PC's Bluetooth unavailable is not "busy elsewhere" -----------------

from bleak.exc import (BleakBluetoothNotAvailableError,  # noqa: E402
                       BleakBluetoothNotAvailableReason, BleakError)

from govee_win11.link import (BT_UNAVAILABLE_MESSAGE,  # noqa: E402
                              classify_connect_error)


def _winerr(code: int, text: str = "x") -> OSError:
    e = OSError(text)
    e.winerror = code
    return e


@pytest.mark.parametrize("exc", [
    BleakBluetoothNotAvailableError("No Bluetooth adapter found",
                                    BleakBluetoothNotAvailableReason.NO_BLUETOOTH),
    BleakBluetoothNotAvailableError(
        "Bluetooth radio is not powered on. Turn on Bluetooth and try again.",
        BleakBluetoothNotAvailableReason.POWERED_OFF),
    BleakError("Failed to start scanner. Is Bluetooth turned on?"),
    BleakError("Bluetooth device is turned off."),
    _winerr(-2147020577),                       # 0x800710DF device not ready for use
    OSError("[WinError -2147020577] 设备未就绪"),  # wrapped: code read from the text
    _winerr(4319),
])
def test_adapter_problems_classify_as_bt_unavailable(exc):
    assert classify_connect_error(exc) == "bt_unavailable"


@pytest.mark.parametrize("exc", [
    BleakDeviceNotFoundError("AA", "Device with address AA was not found."),
    asyncio.TimeoutError(), TimeoutError(), BleakError("Unreachable"),
    OSError("dropped"), _winerr(-2147023673),  # 0x800704C7 cancelled
    _winerr(-2147023729), _winerr(1167),       # 0x8007048F: usually the strip, ambiguous
    OSError("[WinError -2147023729] 设备没有连接。"),
])
def test_strip_problems_stay_busy_elsewhere(exc):
    assert classify_connect_error(exc) == "busy_elsewhere"


def test_classification_follows_the_exception_chain():
    try:
        try:
            raise BleakBluetoothNotAvailableError(
                "off", BleakBluetoothNotAvailableReason.POWERED_OFF)
        except BleakError as inner:
            raise BleakError("connect failed") from inner
    except BleakError as outer:
        assert classify_connect_error(outer) == "bt_unavailable"


async def test_bt_unavailable_status_message_and_retry_with_backoff():
    statuses = []
    link = make(statuses, backoff=(0.01, 0.02))
    off = BleakBluetoothNotAvailableError("Bluetooth radio is not powered on.",
                                          BleakBluetoothNotAvailableReason.POWERED_OFF)
    FakeClient.connect_errors = [off, off, None]
    await link.hold()
    assert ("bt_unavailable", BT_UNAVAILABLE_MESSAGE) in statuses
    assert BT_UNAVAILABLE_MESSAGE == "电脑蓝牙不可用（请检查蓝牙是否开启/设备管理器）"
    assert not any(s == "busy_elsewhere" for s, _ in statuses)
    for _ in range(100):                      # keeps retrying: Bluetooth comes back
        await asyncio.sleep(0.01)
        if link.status == "connected":
            break
    assert link.status == "connected" and FakeClient.connect_calls == 3
    await link.close()


async def test_transient_send_reports_bt_unavailable():
    statuses = []
    link = make(statuses)
    FakeClient.connect_errors = [BleakError("Failed to start scanner. Is Bluetooth turned on?")]
    assert await link.send_transient([b"\x01"]) is False
    assert link.status == "bt_unavailable"
    assert statuses[-1] == ("bt_unavailable", BT_UNAVAILABLE_MESSAGE)
    await link.close()


@pytest.mark.parametrize("answer,expected", [(False, "bt_unavailable"), (True, "busy_elsewhere"),
                                             (None, "busy_elsewhere")])
async def test_adapter_check_explains_ambiguous_failures(answer, expected):
    asked = []

    async def check():
        asked.append(1)
        return answer

    link = make(adapter_check=check)
    FakeClient.connect_errors = [asyncio.TimeoutError()]
    assert await link.send_transient([b"\x01"]) is False
    assert link.status == expected and asked == [1]
    await link.close()


async def test_adapter_check_failures_are_contained():
    async def broken():
        raise RuntimeError("winrt exploded")

    async def hanging():
        await asyncio.sleep(10)

    for check in (broken, hanging):
        link = make(adapter_check=check)
        import govee_win11.link as link_mod
        old, link_mod.ADAPTER_CHECK_TIMEOUT_S = link_mod.ADAPTER_CHECK_TIMEOUT_S, 0.05
        try:
            FakeClient.connect_errors = [BleakDeviceNotFoundError("AA")]
            assert await link.send_transient([b"\x01"]) is False
            assert link.status == "busy_elsewhere"
        finally:
            link_mod.ADAPTER_CHECK_TIMEOUT_S = old
        await link.close()


async def test_clear_exception_skips_the_adapter_check():
    asked = []

    async def check():
        asked.append(1)
        return True

    link = make(adapter_check=check)
    FakeClient.connect_errors = [BleakError("Failed to start scanner. Is Bluetooth turned on?")]
    await link.send_transient([b"\x01"])
    assert link.status == "bt_unavailable" and asked == []
    await link.close()


@pytest.mark.parametrize("adapter_ok,expected", [(True, "busy_elsewhere"),
                                                 (None, "busy_elsewhere"),
                                                 (False, "bt_unavailable")])
async def test_device_not_connected_is_decided_by_the_adapter_check(adapter_ok, expected):
    """0x8007048F mid-connect (phone grabbed the strip) must not blame the PC's Bluetooth
    while the adapter is fine."""
    asked = []

    async def check():
        asked.append(1)
        return adapter_ok

    link = make(adapter_check=check)
    FakeClient.connect_errors = [_winerr(-2147023729, "设备没有连接。")]
    assert await link.send_transient([b""]) is False
    assert link.status == expected and asked == [1]
    await link.close()


async def test_winrt_adapter_check_without_radio_object_cannot_tell(monkeypatch):
    import sys
    import types

    class Radio:
        state = 1

    class Adapter:
        def __init__(self, radio):
            self._radio = radio

        async def get_radio_async(self):
            return self._radio

    results = []
    for adapter in (Adapter(None), None, Adapter(types.SimpleNamespace(state=0))):
        class BluetoothAdapter:
            @staticmethod
            async def get_default_async(a=adapter):
                return a

        bt = types.ModuleType("winrt.windows.devices.bluetooth")
        bt.BluetoothAdapter = BluetoothAdapter
        radios = types.ModuleType("winrt.windows.devices.radios")
        radios.RadioState = types.SimpleNamespace(ON=1)
        monkeypatch.setitem(sys.modules, "winrt.windows.devices.bluetooth", bt)
        monkeypatch.setitem(sys.modules, "winrt.windows.devices.radios", radios)
        from govee_win11.link import winrt_adapter_available
        results.append(await winrt_adapter_available())
    assert results == [None, False, False]   # no radio object: unknown, not "off"


# ---- device discovery (address None) ---------------------------------------------

from govee_win11.discovery import FoundDevice  # noqa: E402
from govee_win11.link import NOT_FOUND_MESSAGE  # noqa: E402

DEV_A = FoundDevice("Govee_H6125_000A", "AA:BB:CC:DD:EE:0A", -50)
DEV_B = FoundDevice("Govee_H6125_000B", "AA:BB:CC:DD:EE:0B", -70)


def make_discovering(results, statuses=None, picked=None, **kw):
    """results: list of scan outcomes popped per scan (a list of devices or an exception)."""
    scans = []

    async def discover():
        scans.append(1)
        r = results.pop(0) if results else []
        if isinstance(r, BaseException):
            raise r
        return r

    cb = (lambda s, m: statuses.append((s, m))) if statuses is not None else (lambda s, m: None)
    link = DeviceLink(None, client_factory=FakeClient, linger_s=0.05, on_status=cb,
                      discover=discover,
                      on_device=lambda d, o: picked.append((d, o)) if picked is not None else None,
                      **kw)
    return link, scans


async def test_discovery_picks_the_strongest_and_connects_to_it():
    picked = []
    link, scans = make_discovering([[DEV_A, DEV_B]], picked=picked)
    assert await link.send_transient([b"\x01"]) is True
    assert link.address == DEV_A.address
    assert picked == [(DEV_A, [DEV_B])]
    assert FakeClient.writes == [b"\x01"]
    # already known: the next connect does not scan again
    await asyncio.sleep(0.15)
    assert await link.send_transient([b"\x02"]) is True
    assert len(scans) == 1
    await link.close()


async def test_discovery_nothing_found_is_not_found_then_retries():
    statuses, picked = [], []
    link, scans = make_discovering([[], [DEV_B]], statuses, picked)
    assert await link.send_transient([b"\x01"]) is False
    assert link.status == "not_found"
    assert ("not_found", NOT_FOUND_MESSAGE) in statuses
    assert NOT_FOUND_MESSAGE == "没有找到 H6125 灯带，请确认灯带通电、手机 Govee App 已断开"
    assert FakeClient.connect_calls == 0
    assert await link.send_transient([b"\x02"]) is True
    assert link.address == DEV_B.address and picked == [(DEV_B, [])]
    await link.close()


async def test_discovery_held_link_retries_with_backoff():
    link, scans = make_discovering([[], [], [DEV_A]], backoff=(0.01, 0.02))
    await link.hold()
    assert link.status == "not_found"
    for _ in range(200):
        await asyncio.sleep(0.01)
        if link.status == "connected":
            break
    assert link.status == "connected"
    assert len(scans) == 3
    await link.close()


async def test_discovery_scan_error_bt_unavailable():
    from bleak.exc import BleakError
    statuses = []
    link, _ = make_discovering([BleakError("No Bluetooth adapter found")], statuses)
    assert await link.send_transient([b"\x01"]) is False
    assert link.status == "bt_unavailable"


async def test_discovery_empty_scan_asks_the_adapter_check():
    async def radio_off():
        return False
    link, _ = make_discovering([[]], adapter_check=radio_off)
    assert await link.send_transient([b"\x01"]) is False
    assert link.status == "bt_unavailable"


async def test_discovery_without_discover_function_is_not_found():
    link = DeviceLink(None, client_factory=FakeClient, linger_s=0.05)
    assert await link.send_transient([b"\x01"]) is False
    assert link.status == "not_found"


async def test_set_address_switches_device_and_held_link_reconnects():
    link = make()
    await link.hold()
    assert link.status == "connected"
    await link.set_address("AA:BB:CC:DD:EE:01")
    assert link.address == "AA:BB:CC:DD:EE:01"
    assert FakeClient.disconnects == 1
    assert link.status == "connected"
    assert link._client.address == "AA:BB:CC:DD:EE:01"
    await link.close()


async def test_set_address_none_rediscovers_on_next_connect():
    picked = []
    link, scans = make_discovering([[DEV_A], [DEV_B]], picked=picked)
    assert await link.send_transient([b"\x01"]) is True
    await link.set_address(None)
    assert link.address is None and link.status == "disconnected"
    assert await link.send_transient([b"\x02"]) is True
    assert link.address == DEV_B.address and len(scans) == 2
    await link.close()


async def test_set_same_address_keeps_connection():
    link = make()
    await link.hold()
    await link.set_address("AA")
    assert FakeClient.disconnects == 0 and link.status == "connected"
    await link.close()


async def test_saved_address_failing_repeatedly_is_reported_stale():
    from govee_win11.link import STALE_DEVICE_FAILURES
    link = make()
    FakeClient.connect_errors = [BleakDeviceNotFoundError("AA")] * STALE_DEVICE_FAILURES
    for i in range(STALE_DEVICE_FAILURES):
        assert link.device_stale is False
        assert await link.send_transient([b"\x01"]) is False
    assert link.status == "busy_elsewhere" and link.device_stale is True
    assert await link.send_transient([b"\x02"]) is True  # connects again: not stale
    assert link.device_stale is False and link.connect_failures == 0
    await link.close()


async def test_set_address_resets_the_failure_count():
    link = make()
    FakeClient.connect_errors = [BleakDeviceNotFoundError("AA")] * 3
    for _ in range(3):
        await link.send_transient([b"\x01"])
    assert link.device_stale
    await link.set_address("AA:BB:CC:DD:EE:01")
    assert link.connect_failures == 0 and not link.device_stale


async def test_unexpected_scan_error_is_not_found_and_keeps_retrying():
    link, scans = make_discovering([RuntimeError("winrt watcher aborted"), [DEV_A]],
                                   backoff=(0.01,))
    await link.hold()
    assert link.status == "not_found"
    for _ in range(100):
        await asyncio.sleep(0.01)
        if link.status == "connected":
            break
    assert link.status == "connected" and len(scans) == 2
    await link.close()
