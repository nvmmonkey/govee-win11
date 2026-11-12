"""Govee H6125 on-device effect formats: scene params (decode/encode) and DIY uploads.

Pure functions only; nothing here talks to Bluetooth. See docs/reference/govee-diy-format.md
for the evidence behind each format and the confidence of each part.

Three ways to get a custom effect running on the strip itself:

1. ``custom_scene_packets``  - upload an edited (re-coloured / re-timed) catalogue scene param
   with the same A3 transport + ``33 05 04`` selector that already plays official scenes.
2. ``diy_packets`` / ``diy_mix_packets`` - the Govee-app DIY "Type 04" body (family, variant,
   speed, palette), uploaded either as A3 subtype 0x04 + ``33 05 0a 18 00`` (modern firmware)
   or as A1 02 frames led by selector 0xFE + ``33 05 0a fe 00`` (older pact-1 firmware).
3. ``advanced_packets`` - a hand-built layered param uploaded as A3 subtype 0x02 and selected
   with the "advanced" scene code 402 (or the Workshop code 401 with scene-type byte 2).
"""
from __future__ import annotations

import json
import math
from typing import Any, Iterable, Sequence

from .protocol import calibrate

RGB = tuple[int, int, int]

# ---------------------------------------------------------------- framing

A3_CHUNK = 17
A1_CHUNK = 16

SUBTYPE_SCENE_TYPE1 = 0x01   # catalogue sceneType 1 (palette/step scenes)
SUBTYPE_LAYERED = 0x02       # catalogue sceneType 2 (layered scenes) / Workshop
SUBTYPE_PAINT = 0x03         # DIY "graffiti"/paint body
SUBTYPE_DIY = 0x04           # DIY Type 04 (single or mixed)

DIY_CODE_A3 = 24             # 33 05 0a 18 00 after an A3 subtype-04 upload
DIY_SELECTOR_A1 = 0xFE       # A1 body prefix and 33 05 0a fe 00 on the pact-1 route
ADVANCED_SCENE_CODE = 402    # 33 05 04 92 01 00 after an A3 subtype-02 upload
WORKSHOP_SCENE_CODE = 401    # 33 05 04 91 01 02 (scene-type byte 2)


def _xor_frame(frame: Sequence[int]) -> bytes:
    frame = list(frame)
    if len(frame) > 19:
        raise ValueError("frame longer than 19 bytes")
    frame += [0] * (19 - len(frame))
    x = 0
    for b in frame:
        x ^= b
    return bytes(frame + [x])


def command(cmd: int, payload: Sequence[int]) -> bytes:
    """A 20-byte ``33 cmd payload… xor`` command frame."""
    return _xor_frame([0x33, cmd, *payload])


def a3_packets(subtype: int, body: bytes) -> list[bytes]:
    """Fragment ``subtype + body`` into A3 frames.

    Payload = ``01 <frame count> <subtype> <body…>`` split into 17-byte chunks; frame indices
    are 0,1,2… with the last one replaced by 0xFF. A payload that fits in one chunk gets an
    extra empty ``A3 FF`` terminator (and the count includes it). For subtype 2 and a body
    longer than 14 bytes this is byte-identical to ``protocol.multi_packets``.
    """
    data = bytes([subtype]) + bytes(body)
    count = math.ceil((len(data) + 2) / A3_CHUNK)
    terminator = count == 1
    payload = bytes([0x01, count + (1 if terminator else 0)]) + data
    chunks = [payload[i:i + A3_CHUNK] for i in range(0, len(payload), A3_CHUNK)]
    out = []
    for i, chunk in enumerate(chunks):
        idx = 0xFF if (i == len(chunks) - 1 and not terminator) else i
        out.append(_xor_frame([0xA3, idx, *chunk]))
    if terminator:
        out.append(_xor_frame([0xA3, 0xFF]))
    return out


def a1_packets(body: bytes) -> list[bytes]:
    """Fragment a DIY body into ``A1 02`` frames: start (count), data 1..n (16 bytes each), end FF."""
    body = bytes(body)
    if not body:
        raise ValueError("empty A1 body")
    chunks = [body[i:i + A1_CHUNK] for i in range(0, len(body), A1_CHUNK)]
    if len(chunks) > 0xFE:
        raise ValueError("A1 body too long")
    return ([_xor_frame([0xA1, 0x02, 0x00, len(chunks)])]
            + [_xor_frame([0xA1, 0x02, i, *c]) for i, c in enumerate(chunks, start=1)]
            + [_xor_frame([0xA1, 0x02, 0xFF])])


def scene_select(code: int, scene_type: int = 0) -> bytes:
    """``33 05 04 code_lo code_hi scene_type``."""
    return command(0x05, [0x04, code & 0xFF, (code >> 8) & 0xFF, scene_type & 0xFF])


def diy_select(code: int) -> bytes:
    """``33 05 0a code_lo code_hi`` - play the DIY stored under ``code``."""
    return command(0x05, [0x0A, code & 0xFF, (code >> 8) & 0xFF])


# ---------------------------------------------------------------- scene params

def _layer_decode(body: bytes) -> dict[str, Any]:
    b = body
    n_bri = b[5]
    i = 6
    bri = []
    for _ in range(n_bri):
        s0, s1, order, speed, r_hi, r_lo = b[i:i + 6]
        bri.append({"scope_start": s0, "scope_end": s1, "order": order, "speed": speed,
                    "brightest_retention": r_hi, "darkest_retention": r_lo})
        i += 6
    direction, colour_speed, colour_retention, n_pal = b[i:i + 4]
    i += 4
    palette = [tuple(b[i + 3 * k:i + 3 * k + 3]) for k in range(n_pal)]
    i += 3 * n_pal
    mv = []
    for _ in range(2):
        packed, interval, speed = b[i:i + 3]
        mv.append({"packed": packed, "enabled": bool(packed & 0x10), "enter_exit": bool(packed & 0x04),
                   "direction": packed & 0x03, "interval": interval, "speed": speed})
        i += 3
    priority = b[i]
    i += 1
    if i > len(b):
        raise ValueError("layer body truncated")
    return {
        "area": b[0], "area_width_tenths": b[0] >> 4, "area_index": b[0] & 0x0F,
        "select_type": b[1], "select_p1": b[2], "select_p2": b[3],
        "flags": b[4], "brightness_gradient": bool(b[4] & 0x02),
        "brightness": bri,
        "direction": direction, "backwards": bool(direction & 0x80), "distribution": direction & 0x0F,
        "colour_speed": colour_speed, "colour_retention": colour_retention,
        "palette": palette,
        "selected_movement": mv[0], "overall_movement": mv[1],
        "priority": priority,
        "excess": bytes(b[i:]),
    }


def _layer_encode(L: dict[str, Any]) -> bytes:
    out = [L["area"], L["select_type"], L["select_p1"], L["select_p2"], L["flags"], len(L["brightness"])]
    for br in L["brightness"]:
        out += [br["scope_start"], br["scope_end"], br["order"], br["speed"],
                br["brightest_retention"], br["darkest_retention"]]
    out += [L["direction"], L["colour_speed"], L["colour_retention"], len(L["palette"])]
    for rgb in L["palette"]:
        out += list(rgb)
    for key in ("selected_movement", "overall_movement"):
        m = L[key]
        out += [m["packed"], m["interval"], m["speed"]]
    out.append(L["priority"])
    return bytes(out) + bytes(L.get("excess", b""))


def decode_scene_param(data: bytes, scene_type: int | None = None) -> dict[str, Any]:
    """Best-effort structured view of a catalogue ``scenceParam`` (already base64-decoded).

    sceneType 2 (layered): ``<n layers> (<len> <layer body>)×n``.
    sceneType 1 (palette steps): ``<config> <n steps> (<rgb> <u16le>)×n <n palette> <rgb>×n``.
    ``scene_type`` None guesses: 0x80-flagged first byte or a non-parsing layer chain → type 1.
    """
    data = bytes(data)
    if not data:
        return {"scene_type": 0, "layers": [], "padding": 0}
    if scene_type is None:
        scene_type = 2
        try:
            _decode_layered(data)
        except (ValueError, IndexError):
            scene_type = 1
    if scene_type == 1:
        return _decode_type1(data)
    if scene_type != 2:
        raise ValueError(f"unsupported scene type {scene_type}")
    return _decode_layered(data)


def _decode_layered(data: bytes) -> dict[str, Any]:
    n = data[0]
    i = 1
    layers = []
    for _ in range(n):
        ln = data[i]
        body = data[i + 1:i + 1 + ln]
        if len(body) != ln:
            raise ValueError("layer length runs past the param")
        layers.append(_layer_decode(body))
        i += 1 + ln
    rest = data[i:]
    if any(rest):
        raise ValueError("non-zero bytes after the last layer")
    return {"scene_type": 2, "layers": layers, "padding": len(rest)}


def _decode_type1(data: bytes) -> dict[str, Any]:
    config, n_steps = data[0], data[1]
    stride, layout = config & 0x07, (config >> 4) & 0x07
    if layout != 0 or stride != 3:
        return {"scene_type": 1, "config": config, "raw": data}
    i = 2
    steps = []
    for _ in range(n_steps):
        steps.append({"rgb": tuple(data[i:i + 3]), "value": data[i + 3] | (data[i + 4] << 8)})
        i += 5
    n_pal = data[i]
    i += 1
    palette = [tuple(data[i + 3 * k:i + 3 * k + 3]) for k in range(n_pal)]
    i += 3 * n_pal
    return {"scene_type": 1, "config": config, "brightness_flag": bool(config & 0x80),
            "steps": steps, "palette": palette, "padding": len(data) - i}


def encode_scene_param(decoded: dict[str, Any]) -> bytes:
    """Inverse of ``decode_scene_param``."""
    st = decoded["scene_type"]
    if st == 0:
        return b""
    if st == 1:
        if "raw" in decoded:
            return bytes(decoded["raw"])
        out = [decoded["config"], len(decoded["steps"])]
        for s in decoded["steps"]:
            out += [*s["rgb"], s["value"] & 0xFF, s["value"] >> 8]
        out.append(len(decoded["palette"]))
        for rgb in decoded["palette"]:
            out += list(rgb)
        return bytes(out) + bytes(decoded.get("padding", 0))
    out = bytearray([len(decoded["layers"])])
    for L in decoded["layers"]:
        body = _layer_encode(L)
        out.append(len(body))
        out += body
    return bytes(out) + bytes(decoded.get("padding", 0))


def scene_palettes(data: bytes, scene_type: int | None = None) -> list[list[RGB]]:
    d = decode_scene_param(data, scene_type)
    if d["scene_type"] == 2:
        return [list(L["palette"]) for L in d["layers"]]
    if d["scene_type"] == 1 and "palette" in d:
        return [[s["rgb"] for s in d["steps"]], list(d["palette"])]
    return []


def recolor_scene_param(data: bytes, colors: Sequence[RGB], *, keep_black: bool = True,
                        scene_type: int | None = None) -> bytes:
    """Replace every palette colour with the user's colours, keeping the scene's structure.

    Colours are assigned cyclically per layer (layer k starts at colour k, so overlapping
    layers differ). Pure black entries are gaps/"off" in many scenes and are kept by default.
    """
    if not colors:
        raise ValueError("need at least one colour")
    # user colours go on the wire: 白平衡 (protocol.calibrate); kept black stays black
    cols = [calibrate(tuple(int(c) & 0xFF for c in rgb)) for rgb in colors]
    d = decode_scene_param(data, scene_type)

    def remap(pal: list[RGB], start: int) -> list[RGB]:
        out, j = [], start
        for rgb in pal:
            if keep_black and tuple(rgb) == (0, 0, 0):
                out.append((0, 0, 0))
            else:
                out.append(cols[j % len(cols)])
                j += 1
        return out

    if d["scene_type"] == 2:
        for k, L in enumerate(d["layers"]):
            L["palette"] = remap(L["palette"], k)
    elif d["scene_type"] == 1 and "steps" in d:
        rgbs = remap([s["rgb"] for s in d["steps"]], 0)
        for s, rgb in zip(d["steps"], rgbs):
            s["rgb"] = rgb
        d["palette"] = remap(d["palette"], 1)
    return encode_scene_param(d)


def apply_speed_config(data: bytes, config: str | list, index: int) -> bytes:
    """Apply a catalogue ``speedInfo.config`` level (as the Govee app does) to a layered param.

    Each config page targets layer ``page``: ``color``→colour_speed, ``moveIn``→selected-area
    movement speed, ``moveAll``→overall movement speed, ``bright[i]``→brightness block
    ``brightPage`` speed. Values ascend with ``index``; ``defaultIndex`` reproduces the
    catalogue bytes (verified on 470/474 fields of the H6125 library).
    """
    pages = json.loads(config) if isinstance(config, str) else config
    d = decode_scene_param(data, 2)
    for pg in pages:
        L = d["layers"][pg["page"]]

        def pick(arr):
            return arr[max(0, min(index, len(arr) - 1))]

        if "color" in pg:
            L["colour_speed"] = pick(pg["color"])
        if "moveIn" in pg:
            L["selected_movement"]["speed"] = pick(pg["moveIn"])
        if "moveAll" in pg:
            L["overall_movement"]["speed"] = pick(pg["moveAll"])
        for br in pg.get("bright", []):
            L["brightness"][int(br["brightPage"])]["speed"] = pick(br["brightValue"])
    return encode_scene_param(d)


def set_speed_bytes(data: bytes, value: int) -> bytes:
    """Experimental: overwrite every non-zero speed byte of a layered param with ``value``.

    Zero speeds (static parts) are left alone. Catalogue speed tables suggest larger = faster
    with useful values roughly 200..255 - unverified on the strip.
    """
    v = max(0, min(255, int(value)))
    d = decode_scene_param(data, 2)
    for L in d["layers"]:
        if L["colour_speed"]:
            L["colour_speed"] = v
        for br in L["brightness"]:
            if br["speed"]:
                br["speed"] = v
        for key in ("selected_movement", "overall_movement"):
            if L[key]["speed"]:
                L[key]["speed"] = v
    return encode_scene_param(d)


def custom_scene_packets(param: bytes, code: int, scene_type: int = 2) -> list[bytes]:
    """Upload an (edited) scene param and select it, exactly like an official scene.

    ``scene_type`` is the A3 subtype byte = catalogue sceneType (1 or 2). ``code`` is the
    catalogue sceneCode of the template (known to play) or ADVANCED_SCENE_CODE.
    """
    return [*a3_packets(scene_type, param), scene_select(code)]


def advanced_packets(param: bytes, *, workshop: bool = False) -> list[bytes]:
    """Upload a layered param and select it with the H617A-family "advanced"/Workshop codes."""
    if workshop:
        return [*a3_packets(SUBTYPE_LAYERED, param), scene_select(WORKSHOP_SCENE_CODE, 2)]
    return [*a3_packets(SUBTYPE_LAYERED, param), scene_select(ADVANCED_SCENE_CODE, 0)]


def custom_scene_from_template(effect: dict, colors: Sequence[RGB] | None = None,
                               speed_index: int | None = None, speed_config: str | None = None,
                               *, keep_black: bool = True) -> list[bytes]:
    """Convenience: re-colour / re-time a library ``lightEffects`` entry and build its packets."""
    import base64
    param = base64.b64decode(effect.get("scenceParam") or "")
    st = effect.get("sceneType") or 2
    if colors:
        param = recolor_scene_param(param, colors, keep_black=keep_black, scene_type=st)
    if speed_index is not None and speed_config:
        param = apply_speed_config(param, speed_config, speed_index)
    return custom_scene_packets(param, effect.get("sceneCode") or 0, st)


# ---------------------------------------------------------------- DIY (Type 04)

# Govee-app DIY grid order (docs/reference/govee-app-diy-effects.png) → (family, variant).
# Family/variant numbers: teh-hippo/ha-govee-led-ble H6125_DIY_FAMILIES (app-order mapping of
# stream/flow/chase to 流水/彩虹/next tile is an inference from identical ordering).
DIY_EFFECTS: dict[str, tuple[int, int]] = {
    "fade1": (0, 0), "fade2": (0, 1), "fade3": (0, 2),
    "jump1": (1, 0), "jump2": (1, 2),
    "blink1": (2, 0), "blink2": (2, 1), "blink3": (2, 2),
    "marquee1": (3, 3), "marquee2": (3, 4), "marquee3": (3, 5),
    "stream1": (8, 9), "stream2": (8, 10),
    "rainbow1": (9, 9), "rainbow2": (9, 10),
    "chase": (10, 0),
    "music1": (4, 8), "music2": (4, 6), "music3": (4, 7),
}
DIY_ALIASES = {
    "淡入淡出1": "fade1", "淡入淡出2": "fade2", "淡入淡出3": "fade3",
    "跳跃1": "jump1", "跳跃2": "jump2",
    "闪烁1": "blink1", "闪烁2": "blink2", "闪烁3": "blink3",
    "跑马灯1": "marquee1", "跑马灯2": "marquee2", "跑马灯3": "marquee3",
    "流水1": "stream1", "流水2": "stream2",
    "彩虹1": "rainbow1", "彩虹2": "rainbow2",
}
# App effect ids (effects.EFFECTS) -> DIY_EFFECTS keys. Absent (aurora) = no device equivalent.
APP_TO_DIY: dict[str, str] = {
    "fade1": "fade1", "fade2": "fade2", "fade3": "fade3",
    "jump1": "jump1", "jump2": "jump2",
    "flash1": "blink1", "flash2": "blink2", "flash3": "blink3",
    "marquee1": "marquee1", "marquee2": "marquee2", "marquee3": "marquee3",
    "flow1": "stream1", "flow2": "stream2",
    "rainbow1": "rainbow1", "rainbow2": "rainbow2",
}


def diy_key_for_app_effect(effect_id: str) -> str | None:
    """DIY_EFFECTS key the strip can run for an app effect id, or None (PC-rendered only)."""
    return APP_TO_DIY.get(effect_id)


MIXABLE_FAMILIES = {0, 1, 2, 3, 8, 9}
MAX_COLORS = 8
MIX_FAMILY = 0xFF


def _effect(effect: str | tuple[int, int]) -> tuple[int, int]:
    if isinstance(effect, tuple):
        return effect
    key = DIY_ALIASES.get(effect, effect)
    if key not in DIY_EFFECTS:
        raise ValueError(f"unknown DIY effect {effect!r}")
    return DIY_EFFECTS[key]


def _colors(colors: Iterable[RGB]) -> list[int]:
    cols = [tuple(int(c) for c in rgb) for rgb in colors]
    if not 1 <= len(cols) <= MAX_COLORS:
        raise ValueError(f"DIY needs 1..{MAX_COLORS} colours")
    flat = []
    for rgb in cols:
        if len(rgb) != 3 or any(not 0 <= c <= 255 for c in rgb):
            raise ValueError(f"bad colour {rgb}")
        flat += list(calibrate(rgb))  # 白平衡: see protocol.calibrate (the choke point)
    return flat


def _speed(speed_pct: int) -> int:
    if not 0 <= int(speed_pct) <= 100:
        raise ValueError("speed must be 0..100")
    return int(speed_pct)


def diy_body(effect: str | tuple[int, int], colors: Sequence[RGB], speed_pct: int) -> bytes:
    """Type 04 single body: ``family variant speed 3n rgb×n``."""
    family, variant = _effect(effect)
    pal = _colors(colors)
    return bytes([family, variant, _speed(speed_pct), len(pal), *pal])


def diy_mix_body(effects: Sequence[str | tuple[int, int]], colors: Sequence[RGB], speed_pct: int) -> bytes:
    """Type 04 mixed ("混合") body: ``FF 00 speed 3n rgb×n 2m (family variant)×m``, m = 1..4."""
    pairs = [_effect(e) for e in effects]
    if not 1 <= len(pairs) <= 4:
        raise ValueError("mix needs 1..4 effects")
    for fam, _ in pairs:
        if fam not in MIXABLE_FAMILIES:
            raise ValueError(f"family {fam} cannot be mixed")
    pal = _colors(colors)
    flat = [x for p in pairs for x in p]
    return bytes([MIX_FAMILY, 0x00, _speed(speed_pct), len(pal), *pal, len(flat), *flat])


def _diy_upload(body: bytes, transport: str, code: int | None) -> list[bytes]:
    if transport == "a3":
        return [*a3_packets(SUBTYPE_DIY, body), diy_select(DIY_CODE_A3 if code is None else code)]
    if transport == "a1":
        sel = DIY_SELECTOR_A1 if code is None else code
        return [*a1_packets(bytes([sel & 0xFF]) + body), diy_select(sel)]
    raise ValueError("transport must be 'a3' or 'a1'")


def diy_packets(effect: str | tuple[int, int], colors: Sequence[RGB], speed_pct: int,
                transport: str = "a3", code: int | None = None) -> list[bytes]:
    """Upload + play a single-effect DIY. ``transport`` 'a3' (modern firmware, code 24) or 'a1'."""
    return _diy_upload(diy_body(effect, colors, speed_pct), transport, code)


def diy_mix_packets(effects: Sequence[str | tuple[int, int]], colors: Sequence[RGB], speed_pct: int,
                    transport: str = "a3", code: int | None = None) -> list[bytes]:
    return _diy_upload(diy_mix_body(effects, colors, speed_pct), transport, code)


def hex_rgb(s: str) -> RGB:
    s = s.lstrip("#")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


# Example DIYs as the Govee app stores them (the built-in presets, config.py).
USER_PRESETS: dict[str, dict[str, Any]] = {
    "orange blur": {"effects": ["fade3"], "colors": ["#ff5000", "#f20d0d"], "speed": 40},
    "yellow blue": {"effects": ["fade3"], "colors": ["#ffe000", "#3ecfe0"], "speed": 40},
    "pink blue": {"effects": ["rainbow1", "rainbow2"],
                  "colors": ["#f040b0", "#0a78c8", "#0a10b0", "#c0084f"], "speed": 32},
    "pink yellow": {"effects": ["rainbow1", "rainbow2"],
                    "colors": ["#ff8a00", "#8a10c0", "#50087e", "#d86a00"], "speed": 53},
}


def preset_packets(name: str, transport: str = "a3") -> list[bytes]:
    p = USER_PRESETS[name]
    cols = [hex_rgb(c) for c in p["colors"]]
    if len(p["effects"]) == 1:
        return diy_packets(p["effects"][0], cols, p["speed"], transport)
    return diy_mix_packets(p["effects"], cols, p["speed"], transport)
