"""On-demand BLE connection to the strip: coalescing, linger, auto-reconnect.

No address yet (``address=None``, nothing chosen in 设置): the first connect scans for
H6125 strips (``discover``) and uses the strongest; ``on_device`` reports the choice so
the Api can save it. Nothing found -> status ``not_found`` (the reconnect loop / next
command retries with the usual backoff).
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Awaitable, Callable, Literal

from bleak import BleakClient
from bleak.exc import BleakError

try:  # bleak >= 2.0
    from bleak.exc import BleakBluetoothNotAvailableError
except ImportError:  # pragma: no cover - older bleak
    class BleakBluetoothNotAvailableError(BleakError):  # type: ignore[no-redef]
        pass

from .discovery import FoundDevice, choose
from .i18n import MESSAGES
from .protocol import WRITE_CHAR

log = logging.getLogger(__name__)

Status = Literal["disconnected", "connecting", "connected", "busy_elsewhere",
                 "bt_unavailable", "not_found"]

# status messages passed to on_status (Chinese; the Api words them in the UI language
# from the status itself: i18n "link.<status>")
BUSY_MESSAGE = MESSAGES["zh"]["link.busy_elsewhere"]
BT_UNAVAILABLE_MESSAGE = MESSAGES["zh"]["link.bt_unavailable"]
NOT_FOUND_MESSAGE = MESSAGES["zh"]["link.not_found"]
VISIBLE_LINGER_S = 60.0  # window open: keep the link this long after the last command
HIDDEN_LINGER_S = 3.0    # minimised / in tray
PACED_GAP_S = 0.04        # between packets of a paced upload (on-device DIY)
PACED_LAST_GAP_S = 0.15   # before the final (select) packet, so the strip has stored the body
DEFAULT_BACKOFF = (1, 2, 4, 8, 10)
# consecutive failed connects to a saved address after which the UI also suggests
# choosing another strip (the saved one may have been replaced / reset)
STALE_DEVICE_FAILURES = 3
ADAPTER_CHECK_TIMEOUT_S = 2.0

# ---- why a connect failed: the PC's Bluetooth vs. the strip being busy elsewhere ----
# bleak (WinRT) starts a scanner inside connect(); with no adapter (e.g. Device Manager
# code 10), a classic-only adapter or the radio switched off it raises
# BleakBluetoothNotAvailableError ("No Bluetooth adapter found" / "Bluetooth radio is
# not powered on...") or, when the watcher aborts, BleakError("Failed to start scanner.
# Is Bluetooth turned on?"). A WinRT call on an adapter that is not ready surfaces as
# OSError 0x800710DF (device not ready for use). Everything else -- timeouts, device
# not found because a phone holds it, and 0x8007048F "device not connected", which
# WinRT GATT raises for the *remote* device when the strip drops / is grabbed mid-
# connect -- stays busy_elsewhere unless the adapter check says the radio is gone.
_BT_MESSAGE_HINTS = (
    "no bluetooth adapter",
    "bluetooth radio is not powered",
    "is bluetooth turned on",
    "bluetooth is turned off",
    "bluetooth device is turned off",
    "bluetooth adapter is turned off",
    "'central' role not supported",
)
_BT_WIN32_ERRORS = {
    4319,  # ERROR_DEVICE_NOT_AVAILABLE "The device is not ready for use" (0x800710DF)
}
# 1167 ERROR_DEVICE_NOT_CONNECTED (0x8007048F) is deliberately absent: ambiguous, so it
# goes through DeviceLink's adapter check like any other unexplained failure.


_WINERROR_TEXT = re.compile(r"\[WinError (-?\d+)\]")


def _win32_code(exc: OSError) -> int | None:
    code = getattr(exc, "winerror", None)
    if not isinstance(code, int):  # re-raised / wrapped: "[WinError -2147023729] ..."
        m = _WINERROR_TEXT.search(str(exc))
        if not m:
            return None
        code = int(m.group(1))
    code &= 0xFFFFFFFF
    if code & 0xFFFF0000 == 0x80070000:  # HRESULT_FROM_WIN32
        code &= 0xFFFF
    return code


def classify_connect_error(exc: BaseException) -> Status:
    """``bt_unavailable`` when ``exc`` (or an exception it was raised from) says the PC's
    Bluetooth is missing / off / broken, else ``busy_elsewhere``."""
    seen = 0
    e: BaseException | None = exc
    while e is not None and seen < 8:
        if isinstance(e, BleakBluetoothNotAvailableError):
            return "bt_unavailable"
        text = " ".join(str(a) for a in e.args).lower() if e.args else str(e).lower()
        if any(h in text for h in _BT_MESSAGE_HINTS):
            return "bt_unavailable"
        if isinstance(e, OSError) and _win32_code(e) in _BT_WIN32_ERRORS:
            return "bt_unavailable"
        e = e.__cause__ or e.__context__
        seen += 1
    return "busy_elsewhere"


async def winrt_adapter_available() -> bool | None:
    """Ask Windows whether the default Bluetooth adapter exists and its radio is on.
    True / False, or None when it cannot tell (no WinRT projection, call failed)."""
    try:
        from winrt.windows.devices.bluetooth import BluetoothAdapter
        from winrt.windows.devices.radios import RadioState
    except Exception:
        return None
    try:
        adapter = await BluetoothAdapter.get_default_async()
        if adapter is None:
            return False
        radio = await adapter.get_radio_async()
        if radio is None:
            return None  # radio access not granted (policy): cannot tell, not "off"
        return radio.state == RadioState.ON
    except Exception:
        return None


class DeviceLink:
    def __init__(self, address: str | None, *, client_factory=BleakClient,
                 linger_s: float = 3.0,
                 on_status: Callable[[Status, str | None], None] = lambda s, m: None,
                 backoff: tuple[float, ...] = DEFAULT_BACKOFF,
                 pace_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 adapter_check: Callable[[], Awaitable[bool | None]] | None = None,
                 discover: Callable[[], Awaitable[list[FoundDevice]]] | None = None,
                 on_device: Callable[[FoundDevice, list[FoundDevice]], None]
                 = lambda d, others: None):
        """``adapter_check`` (optional, e.g. ``winrt_adapter_available``): asked after a
        connect failure the exception alone does not explain; False = the PC's
        Bluetooth is unavailable.

        ``address`` None: ``discover()`` (e.g. ``discovery.scan``) finds the strip on the
        next connect; ``on_device(chosen, others)`` is called (loop thread) with the
        strongest H6125 and the other ones seen."""
        self._address = address
        self.connect_failures = 0  # consecutive failed connects to the current address
        self._discover = discover
        self._on_device = on_device
        self._adapter_check = adapter_check
        self._factory = client_factory
        self._linger_s = linger_s
        self._on_status = on_status
        self._backoff = backoff or DEFAULT_BACKOFF
        self.status: Status = "disconnected"
        self._client = None
        self._lock = asyncio.Lock()
        self._held = False
        self._linger_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        # key -> [latest packets, futures awaiting that write]
        self._pending: dict[object, list] = {}
        self._anon = 0
        self._linger_busy = False
        self._pace_sleep = pace_sleep

    # ---- status / helpers ----
    def _set(self, status: Status, message: str | None = None) -> None:
        self.status = status
        try:
            self._on_status(status, message)
        except Exception:
            pass

    @property
    def address(self) -> str | None:
        """The strip's address (None until discovery picks one)."""
        return self._address

    def _connected(self) -> bool:
        return self._client is not None and bool(self._client.is_connected)

    def _cancel_linger(self) -> None:
        # only cancel while still sleeping; once it owns the lock the disconnect must finish
        if self._linger_task and not self._linger_task.done() and not self._linger_busy:
            self._linger_task.cancel()
        self._linger_task = None

    def set_linger(self, seconds: float) -> None:
        """Change the idle linger (call on the loop thread). A pending linger restarts
        with the new duration; nothing connects, and held links are left alone."""
        self._linger_s = seconds
        pending = (self._linger_task is not None and not self._linger_task.done()
                   and not self._linger_busy)
        if pending and not self._held:
            self._start_linger()

    def _start_linger(self) -> None:
        self._cancel_linger()
        self._linger_task = asyncio.ensure_future(self._linger())

    async def _linger(self) -> None:
        await asyncio.sleep(self._linger_s)
        async with self._lock:
            self._linger_busy = True
            try:
                if not self._held and not self._pending and self._client is not None:
                    await self._disconnect_locked()
            finally:
                self._linger_busy = False

    # ---- connection (call with lock held) ----
    async def _connect_locked(self) -> bool:
        if self._connected():
            return True
        self._client = None
        self._set("connecting")
        if self._address is None:
            try:
                found = await self._discover_locked()
            except BaseException:
                self._set("disconnected")  # cancelled mid-scan: never stay "connecting"
                raise
            if not found:
                return False
        client = self._factory(self._address)
        try:
            await client.connect()
        except (BleakError, asyncio.TimeoutError, TimeoutError, OSError) as e:
            status = classify_connect_error(e)
            if status == "busy_elsewhere" and await self._adapter_unavailable():
                status = "bt_unavailable"
            if status == "busy_elsewhere":
                self.connect_failures += 1
            # the reconnect loop / next command keeps retrying with the usual backoff
            self._set(status, BT_UNAVAILABLE_MESSAGE if status == "bt_unavailable"
                      else BUSY_MESSAGE)
            return False
        except BaseException:
            # cancelled (release()/close() stops a reconnect mid-connect) or a surprise
            # error: never leave the UI on "connecting" -- nothing will reset it later
            self._set("disconnected")
            raise
        self._client = client
        self.connect_failures = 0
        self._set("connected")
        return True

    @property
    def device_stale(self) -> bool:
        """A saved address keeps failing although the PC's Bluetooth works."""
        return (self._address is not None and self.status == "busy_elsewhere"
                and self.connect_failures >= STALE_DEVICE_FAILURES)

    async def _discover_locked(self) -> bool:
        """Scan for H6125 strips and adopt the strongest. False (status set) when none
        is found or the PC's Bluetooth cannot scan."""
        devices: list[FoundDevice] = []
        status: Status | None = None
        if self._discover is not None:
            try:
                devices = list(await self._discover())
            except Exception as e:  # BleakError, OSError, WinRT surprises: never end
                # the reconnect loop (CancelledError is not an Exception: it propagates)
                if classify_connect_error(e) == "bt_unavailable":
                    status = "bt_unavailable"
                log.info("device scan failed: %r", e)
        chosen, others = choose(devices)
        if chosen is None:
            if status is None:
                status = "bt_unavailable" if await self._adapter_unavailable() else "not_found"
            self._set(status, BT_UNAVAILABLE_MESSAGE if status == "bt_unavailable"
                      else NOT_FOUND_MESSAGE)
            return False
        log.info("discovered %s (%s), %d other H6125 nearby",
                 chosen.name, chosen.address, len(others))
        self._address = chosen.address
        try:
            self._on_device(chosen, others)
        except Exception:
            log.exception("on_device callback failed")
        return True

    async def set_address(self, address: str | None) -> None:
        """Use another strip (None = discover again on the next connect). The current
        connection is dropped; a held link (running mode) reconnects at once."""
        async with self._lock:
            changed = address != self._address
            self._address = address
            if changed:
                self.connect_failures = 0
            if changed:
                await self._disconnect_locked()
            if not (changed and self._held):
                return
            ok = await self._connect_locked()
        if not ok:
            self._ensure_reconnect()

    async def _adapter_unavailable(self) -> bool:
        if self._adapter_check is None:
            return False
        try:
            ok = await asyncio.wait_for(self._adapter_check(), ADAPTER_CHECK_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return ok is False

    async def _disconnect_locked(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        if self.status != "disconnected":
            self._set("disconnected")

    async def _write_locked(self, packets: list[bytes], paced: bool = False) -> bool:
        try:
            last = len(packets) - 1
            for i, p in enumerate(packets):
                if paced and i > 0:
                    await self._pace_sleep(PACED_LAST_GAP_S if i == last else PACED_GAP_S)
                await self._client.write_gatt_char(WRITE_CHAR, p, response=False)
        except (OSError, BleakError):
            await self._disconnect_locked()
            return False
        return True

    # ---- transient ----
    async def send_transient(self, packets: list[bytes], key: str | None = None,
                             paced: bool = False) -> bool:
        fut = asyncio.get_running_loop().create_future()
        if key is None:
            self._anon += 1
            k: object = ("anon", self._anon)
        else:
            k = key
        entry = self._pending.get(k)
        if entry is None:
            self._pending[k] = [packets, [fut], paced]
        else:
            entry[0] = packets
            entry[2] = paced
            entry[1].append(fut)
        self._cancel_linger()
        batch: dict = {}
        try:
            async with self._lock:
                try:
                    if self._pending:
                        ok = await self._connect_locked()
                        # take the batch after connecting so calls made meanwhile coalesce
                        await asyncio.sleep(0)
                        batch, self._pending = self._pending, {}
                        for pk, futs, pc in batch.values():
                            res = ok and await self._write_locked(pk, pc)
                            for f in futs:
                                if not f.done():
                                    f.set_result(res)
                            if ok and not res:
                                ok = False
                finally:
                    # never leave other callers' futures hanging
                    for _, futs, _pc in batch.values():
                        for f in futs:
                            if not f.done():
                                f.set_result(False)
                    if not self._held and self._client is not None:
                        self._start_linger()
        except asyncio.CancelledError:
            raise
        return await fut

    # ---- persistent mode ----
    async def hold(self) -> None:
        self._held = True
        self._cancel_linger()
        async with self._lock:
            ok = await self._connect_locked()
        if not ok:
            self._ensure_reconnect()

    async def release(self) -> None:
        self._held = False
        self._cancel_reconnect()
        self._start_linger()

    async def send(self, packets: list[bytes]) -> bool:
        async with self._lock:
            if not self._connected():
                ok = False
            else:
                ok = await self._write_locked(packets)
        if not ok and self._held:
            self._ensure_reconnect()
        return ok

    def _ensure_reconnect(self) -> None:
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.ensure_future(self._reconnect_loop())

    def _cancel_reconnect(self) -> None:
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        self._reconnect_task = None

    async def _reconnect_loop(self) -> None:
        i = 0
        while self._held:
            delay = self._backoff[min(i, len(self._backoff) - 1)]
            await asyncio.sleep(delay)
            if not self._held:
                return
            async with self._lock:
                if await self._connect_locked():
                    return
            i += 1

    async def close(self) -> None:
        self._held = False
        self._cancel_reconnect()
        self._cancel_linger()
        async with self._lock:
            await self._disconnect_locked()
