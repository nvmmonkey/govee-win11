"""Find H6125 strips nearby (BLE advertisements): no hard-coded address.

A strip advertises as ``Govee_H6125_XXXX`` (older firmware: ``ihoment_H6125_XXXX``).
``scan`` asks bleak for every advertiser seen within ``timeout`` and keeps the H6125s,
strongest signal first; ``select_devices`` does the filtering / ordering on plain data
so it is unit-tested without a radio.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable

NAME_PREFIXES = ("govee_h6125", "ihoment_h6125")  # compared case-insensitively
SCAN_TIMEOUT_S = 5.0
_MAC = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")


@dataclass(frozen=True)
class FoundDevice:
    name: str
    address: str
    rssi: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def is_h6125(name: str | None) -> bool:
    return isinstance(name, str) and name.strip().lower().startswith(NAME_PREFIXES)


def normalise_address(value: Any) -> str | None:
    """``"aa:bb:cc:dd:ee:ff"`` / ``"AA-BB-..."`` -> ``"AA:BB:CC:DD:EE:FF"``; else None."""
    if not isinstance(value, str):
        return None
    a = value.strip().upper().replace("-", ":")
    return a if _MAC.match(a) else None


def _rssi_key(d: FoundDevice) -> tuple[int, int]:
    return (0, -d.rssi) if isinstance(d.rssi, int) else (1, 0)  # unknown RSSI last


def select_devices(results: Iterable[tuple[str | None, str, int | None]]) -> list[FoundDevice]:
    """``(name, address, rssi)`` advertisements -> the H6125s, one per address (its
    strongest sighting), strongest signal first."""
    best: dict[str, FoundDevice] = {}
    for name, address, rssi in results:
        if not is_h6125(name):
            continue
        addr = normalise_address(address) or str(address)
        if isinstance(rssi, bool) or not isinstance(rssi, int):
            rssi = None
        d = FoundDevice(name.strip(), addr, rssi)
        old = best.get(addr)
        if old is None or _rssi_key(d) < _rssi_key(old):
            best[addr] = d
    return sorted(best.values(), key=_rssi_key)


def choose(devices: list[FoundDevice]) -> tuple[FoundDevice | None, list[FoundDevice]]:
    """The device to use (strongest signal) and the others (listed in the UI)."""
    if not devices:
        return None, []
    return devices[0], list(devices[1:])


async def scan(timeout: float = SCAN_TIMEOUT_S, scanner=None) -> list[FoundDevice]:
    """Scan for ``timeout`` seconds; H6125s strongest first. Raises bleak's errors when
    the PC's Bluetooth is unavailable (the link classifies them)."""
    if scanner is None:
        from bleak import BleakScanner as scanner  # noqa: N813
    found = await scanner.discover(timeout=timeout, return_adv=True)
    rows = []
    for device, adv in found.values():
        name = getattr(adv, "local_name", None) or getattr(device, "name", None)
        rows.append((name, device.address, getattr(adv, "rssi", None)))
    return select_devices(rows)
