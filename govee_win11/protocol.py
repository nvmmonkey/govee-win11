"""Govee BLE 20-byte frame builders."""
from __future__ import annotations

RGB = tuple[int, int, int]

WRITE_CHAR = "00010203-0405-0607-0809-0a0b0c0d2b11"


def _with_xor(frame: list[int]) -> bytes:
    x = 0
    for b in frame:
        x ^= b
    return bytes(frame + [x])


def ble_packet(cmd: int, payload: list[int]) -> bytes:
    if len(payload) > 17:
        raise ValueError("payload longer than 17 bytes")
    frame = [0x33, cmd, *payload]
    frame += [0] * (19 - len(frame))
    return _with_xor(frame)


def power_packet(on: bool) -> bytes:
    return ble_packet(0x01, [1 if on else 0])


def brightness_packet(raw: int) -> bytes:
    return ble_packet(0x04, [raw])


# ---- colour calibration (白平衡) -- THE choke point for every app-chosen colour ----
# Every RGB the app puts into a packet for the strip passes through ``calibrate``:
# ``segment_packet`` (all segment colour commands: static colours, the engine planner for
# effects / screen sync / music / previews / restores, test colours),
# ``diy_format._colors`` (the palette inside on-device DIY uploads) and
# ``diy_format.recolor_scene_param`` (user colours written into a scene param; no app
# path uses it today). Left uncalibrated on purpose: catalogue scene codes / params
# (firmware content), and ``diy_format.custom_scene_packets`` / ``advanced_packets``,
# which upload an already-built param as raw bytes -- recolour it first.
# Callers (planner, UI preview) keep working with the intended colour; only the bytes
# on the wire change. Gains are percent 0..100 per channel; 100/100/100 is the identity
# (byte-identical packets). Module state, set by Api (``set_color_cal``), so a running
# mode picks a change up with its next packet.
_color_cal: tuple[int, int, int] = (100, 100, 100)


def set_color_cal(r: int, g: int, b: int) -> None:
    gains = (r, g, b)
    if any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 100 for v in gains):
        raise ValueError(f"colour calibration gains must be ints 0..100, got {gains!r}")
    global _color_cal
    _color_cal = gains


def get_color_cal() -> tuple[int, int, int]:
    return _color_cal


def calibrate(rgb: RGB) -> RGB:
    """``out = round(c * gain / 100)`` per channel (gains only ever reduce a channel)."""
    gr, gg, gb = _color_cal
    if gr == gg == gb == 100:
        return (rgb[0], rgb[1], rgb[2])
    return (round(rgb[0] * gr / 100), round(rgb[1] * gg / 100), round(rgb[2] * gb / 100))


def segment_packet(rgb: RGB, mask: int) -> bytes:
    r, g, b = calibrate(rgb)
    return ble_packet(0x05, [0x15, 0x01, r, g, b, 0, 0, 0, 0, 0, mask & 0xFF, (mask >> 8) & 0xFF])


def multi_packets(data: bytes, header: int = 0x02) -> list[bytes]:
    if len(data) <= 14:
        raise ValueError("multi-packet data must be longer than 14 bytes")
    rest = data[14:]
    chunks = [rest[i:i + 17] for i in range(0, len(rest), 17)]
    total = 1 + len(chunks)
    packets = [_with_xor([0xA3, 0x00, 0x01, total, header, *data[:14]])]
    for i, chunk in enumerate(chunks, start=1):
        idx = 0xFF if i == len(chunks) else i
        packets.append(_with_xor([0xA3, idx, *chunk, *[0] * (17 - len(chunk))]))
    return packets


def scene_packets(code: int, param: bytes, scene_type: int = 2) -> list[bytes]:
    """``scene_type`` is the A3 header byte = the catalogue's ``sceneType`` (1 or 2)."""
    packets = multi_packets(param, header=scene_type) if param else []
    packets.append(ble_packet(0x05, [0x04, code & 0xFF, code >> 8]))
    return packets
