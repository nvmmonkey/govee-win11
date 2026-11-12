"""Device discovery (no hard-coded address): selection logic on fake scan results; no
real radio is used anywhere here."""
import asyncio
from types import SimpleNamespace

import pytest

from govee_win11 import discovery
from govee_win11.discovery import FoundDevice, choose, is_h6125, normalise_address, select_devices


@pytest.mark.parametrize("name", ["Govee_H6125_1A2B", "ihoment_H6125_FFFF", "GOVEE_H6125",
                                  "govee_h6125_abcd", " Govee_H6125_0001 "])
def test_h6125_names(name):
    assert is_h6125(name)


@pytest.mark.parametrize("name", [None, "", "Govee_H6126_1A2B", "Govee_H6199", "H6125",
                                  "ihoment_H6160_1234", "My Govee_H6125"])
def test_other_names(name):
    assert not is_h6125(name)


def test_normalise_address():
    assert normalise_address("aa:bb:cc:dd:ee:ff") == "AA:BB:CC:DD:EE:FF"
    assert normalise_address(" AA-BB-CC-DD-EE-01 ") == "AA:BB:CC:DD:EE:01"
    for bad in (None, 1, "", "AA:BB:CC:DD:EE", "AA:BB:CC:DD:EE:GG", "AABBCCDDEEFF"):
        assert normalise_address(bad) is None


def test_select_filters_dedupes_and_sorts_by_rssi():
    rows = [
        ("Govee_H6125_0001", "AA:BB:CC:DD:EE:01", -80),
        ("Some TV", "11:22:33:44:55:66", -30),
        ("ihoment_H6125_0002", "aa:bb:cc:dd:ee:02", -50),
        ("Govee_H6125_0001", "AA:BB:CC:DD:EE:01", -60),  # stronger sighting of the same
        (None, "AA:BB:CC:DD:EE:09", -10),
        ("Govee_H6125_0003", "AA:BB:CC:DD:EE:03", None),
    ]
    assert select_devices(rows) == [
        FoundDevice("ihoment_H6125_0002", "AA:BB:CC:DD:EE:02", -50),
        FoundDevice("Govee_H6125_0001", "AA:BB:CC:DD:EE:01", -60),
        FoundDevice("Govee_H6125_0003", "AA:BB:CC:DD:EE:03", None),  # unknown RSSI last
    ]


def test_choose_exactly_one():
    d = FoundDevice("Govee_H6125_0001", "AA:BB:CC:DD:EE:01", -70)
    assert choose([d]) == (d, [])


def test_choose_several_takes_the_strongest_and_lists_the_others():
    devs = select_devices([("Govee_H6125_A", "AA:BB:CC:DD:EE:0A", -90),
                           ("Govee_H6125_B", "AA:BB:CC:DD:EE:0B", -40),
                           ("Govee_H6125_C", "AA:BB:CC:DD:EE:0C", -65)])
    chosen, others = choose(devs)
    assert chosen.address == "AA:BB:CC:DD:EE:0B"
    assert [d.address for d in others] == ["AA:BB:CC:DD:EE:0C", "AA:BB:CC:DD:EE:0A"]


def test_choose_none():
    assert choose([]) == (None, [])


def test_to_dict():
    assert FoundDevice("Govee_H6125_1", "AA:BB:CC:DD:EE:FF", -55).to_dict() == \
        {"name": "Govee_H6125_1", "address": "AA:BB:CC:DD:EE:FF", "rssi": -55}


def test_scan_uses_local_name_or_device_name_and_rssi():
    calls = []

    class FakeScanner:
        @staticmethod
        async def discover(timeout, return_adv):
            calls.append((timeout, return_adv))
            dev = lambda a, n: SimpleNamespace(address=a, name=n)  # noqa: E731
            adv = lambda n, r: SimpleNamespace(local_name=n, rssi=r)  # noqa: E731
            return {
                "1": (dev("AA:BB:CC:DD:EE:01", None), adv("Govee_H6125_0001", -70)),
                "2": (dev("AA:BB:CC:DD:EE:02", "Govee_H6125_0002"), adv(None, -40)),
                "3": (dev("AA:BB:CC:DD:EE:03", "Speaker"), adv("Speaker", -20)),
            }

    found = asyncio.run(discovery.scan(timeout=1.5, scanner=FakeScanner))
    assert calls == [(1.5, True)]
    assert [(d.name, d.address, d.rssi) for d in found] == [
        ("Govee_H6125_0002", "AA:BB:CC:DD:EE:02", -40),
        ("Govee_H6125_0001", "AA:BB:CC:DD:EE:01", -70),
    ]
