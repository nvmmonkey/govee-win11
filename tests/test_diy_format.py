import base64
import json
from functools import lru_cache

import pytest

from govee_win11 import diy_format as df
from govee_win11.protocol import multi_packets, scene_packets
from govee_win11.scenes import DEFAULT_PATH


@lru_cache(maxsize=1)
def _effects():
    data = json.loads(DEFAULT_PATH.read_text(encoding="utf-8-sig"))["data"]
    out = []
    for cat in data["categories"]:
        for s in cat["scenes"]:
            for e in s.get("lightEffects") or []:
                out.append((s["sceneName"], e))
    return out


def _params(scene_type=None):
    for name, e in _effects():
        p = base64.b64decode(e.get("scenceParam") or "")
        if p and (scene_type is None or e["sceneType"] == scene_type):
            yield name, e, p


def _xor_ok(frame):
    x = 0
    for b in frame[:19]:
        x ^= b
    return len(frame) == 20 and x == frame[19]


# ------------------------------------------------------------ scene params

def test_every_library_param_round_trips():
    n = 0
    for name, e, p in _params():
        d = df.decode_scene_param(p, e["sceneType"])
        assert df.encode_scene_param(d) == p, name
        n += 1
    assert n >= 220


def test_layered_params_have_no_unexplained_bytes():
    layers = 0
    for name, e, p in _params(2):
        d = df.decode_scene_param(p, 2)
        assert d["padding"] == 0, name
        for L in d["layers"]:
            assert L["excess"] == b"", name
            layers += 1
    assert layers > 600


def test_scene_type_is_guessed():
    for name, e, p in _params():
        assert df.decode_scene_param(p)["scene_type"] == e["sceneType"], name


def test_type1_halloween_structure():
    _, e, p = next(x for x in _params(1) if x[0] == "Halloween")
    d = df.decode_scene_param(p, 1)
    assert d["brightness_flag"] and len(d["steps"]) == 6
    assert d["palette"][0] == (0xFF, 0x1E, 0x00)


def test_palettes_match_scene_names():
    deep = next(p for n, e, p in _params(2) if n == "Deep sea")
    blues = [c for pal in df.scene_palettes(deep) for c in pal]
    assert all(b >= r for r, g, b in blues)
    fire = [p for n, e, p in _params(2) if n == "Fire"]
    for p in fire:
        for pal in df.scene_palettes(p):
            for r, g, b in pal:
                assert r >= b or (r, g, b) in {(0, 0, 0), (255, 255, 255)}


def test_recolor_keeps_length_and_structure():
    for name, e, p in _params():
        q = df.recolor_scene_param(p, [(255, 0, 0), (0, 0, 255)], scene_type=e["sceneType"])
        assert len(q) == len(p)
        a, b = df.decode_scene_param(p, e["sceneType"]), df.decode_scene_param(q, e["sceneType"])
        if a["scene_type"] == 2:
            for la, lb in zip(a["layers"], b["layers"]):
                assert len(la["palette"]) == len(lb["palette"])
                assert la["colour_speed"] == lb["colour_speed"]
                for ca, cb in zip(la["palette"], lb["palette"]):
                    if ca == (0, 0, 0):
                        assert cb == (0, 0, 0)
                    else:
                        assert cb in {(255, 0, 0), (0, 0, 255)}


def test_speed_config_default_index_reproduces_catalogue_bytes():
    checked = same = 0
    for name, e in _effects():
        for sp in e.get("specialEffect") or []:
            info = sp.get("speedInfo") or {}
            if "H6125" not in (sp.get("supportSku") or []) or not info.get("supSpeed"):
                continue
            p = base64.b64decode(sp["scenceParam"])
            pages = json.loads(info["config"])
            for pg in pages:
                q = df.apply_speed_config(p, [pg], pg["defaultIndex"])
                checked += 1
                same += q == p
    assert checked > 400
    assert same / checked > 0.98


def test_speed_config_changes_speed_bytes():
    name, sp = next((n, sp) for n, e in _effects() for sp in e.get("specialEffect") or []
                    if "H6125" in (sp.get("supportSku") or []) and (sp.get("speedInfo") or {}).get("supSpeed"))
    p = base64.b64decode(sp["scenceParam"])
    cfg = sp["speedInfo"]["config"]
    assert df.apply_speed_config(p, cfg, 0) != df.apply_speed_config(p, cfg, 99)


def test_set_speed_bytes_only_touches_nonzero():
    _, e, p = next(x for x in _params(2) if x[0] == "Aurora" and x[1]["cmdVersion"] == 1)
    q = df.set_speed_bytes(p, 250)
    for la, lb in zip(df.decode_scene_param(p, 2)["layers"], df.decode_scene_param(q, 2)["layers"]):
        assert lb["colour_speed"] == (250 if la["colour_speed"] else 0)
        assert la["palette"] == lb["palette"]


# ------------------------------------------------------------ framing

def test_a3_matches_verified_multi_packets():
    for name, e, p in _params(2):
        if len(p) > 14:
            assert df.a3_packets(2, p) == multi_packets(p), name


def test_custom_scene_packets_equal_official_scene_packets_when_unedited():
    for name, e, p in _params(2):
        if len(p) > 14:
            assert df.custom_scene_packets(p, e["sceneCode"]) == scene_packets(e["sceneCode"], p)


def test_custom_scene_type1_uses_subtype_1():
    _, e, p = next(x for x in _params(1))
    pk = df.custom_scene_packets(p, e["sceneCode"], 1)
    assert pk[0][:5] == bytes([0xA3, 0x00, 0x01, len(pk) - 1, 0x01])


def test_a3_single_chunk_gets_terminator():
    pk = df.a3_packets(4, bytes([0, 2, 50, 3, 255, 0, 0]))
    assert len(pk) == 2
    assert pk[0][:6] == bytes([0xA3, 0x00, 0x01, 0x02, 0x04, 0x00])
    assert pk[1][:2] == bytes([0xA3, 0xFF]) and not any(pk[1][2:19])
    assert all(_xor_ok(f) for f in pk)


def test_a3_multi_chunk_last_is_ff():
    body = bytes(range(40))
    pk = df.a3_packets(4, body)
    assert [f[1] for f in pk] == [0, 1, 0xFF]
    assert pk[0][3] == 3
    joined = b"".join(f[2:19] for f in pk)
    assert joined[:3 + len(body)] == bytes([1, 3, 4]) + body


# ------------------------------------------------------------ DIY

def test_h6127_published_single_diy_frame():
    # BeauJBurroughs/Govee-H6127-Reverse-Engineering: "a102 01 0a 03 03 2b 18 ff0000 ff7f00 ffff00 00ff 1b"
    cols = [(255, 0, 0), (255, 127, 0), (255, 255, 0), (0, 255, 0), (0, 0, 255), (0, 255, 255),
            (139, 0, 255), (255, 255, 255)]
    pk = df.a1_packets(bytes([0x0A]) + df.diy_body((3, 3), cols, 0x2B))
    assert pk[1] == bytes.fromhex("a102010a03032b18ff0000ff7f00ffff0000ff1b")


def test_h6127_published_combo_stream():
    expected = [
        "a1020003000000000000000000000000000000a0",
        "a102013bff006218ffffffff0000ffffffff001c",
        "a10202" "00ffffffff0000ffffffff0000080000a9",
        "a1020301000200030300000000000000000000a3",
        "a102ff000000000000000000000000000000005c",
    ]
    cols = [(255, 255, 255), (255, 0, 0)] * 4
    body = df.diy_mix_body([(0, 0), (1, 0), (2, 0), (3, 3)], cols, 0x62)
    pk = df.a1_packets(bytes([0x3B]) + body)
    assert [f.hex() for f in pk] == expected
    assert df.diy_select(0).hex() == "33050a000000000000000000000000000000003c"


def test_diy_packets_a3_route():
    pk = df.diy_packets("淡入淡出3", [(0xFF, 0x50, 0x00), (0xF2, 0x0D, 0x0D)], 50)
    assert pk[0][:10] == bytes([0xA3, 0x00, 0x01, 0x02, 0x04, 0x00, 0x02, 50, 6, 0xFF])
    assert pk[-1][:5] == bytes([0x33, 0x05, 0x0A, 24, 0x00])
    assert all(_xor_ok(f) for f in pk)


def test_diy_packets_a1_route():
    pk = df.diy_packets("fade3", [(1, 2, 3)], 10, transport="a1")
    assert pk[0][:4] == bytes([0xA1, 0x02, 0x00, 0x01])
    assert pk[1][:10] == bytes([0xA1, 0x02, 0x01, 0xFE, 0x00, 0x02, 10, 3, 1, 2])
    assert pk[-1][:5] == bytes([0x33, 0x05, 0x0A, 0xFE, 0x00])


def test_mix_body_layout():
    body = df.diy_mix_body(["彩虹1", "彩虹2"], [(1, 1, 1), (2, 2, 2)], 45)
    assert body == bytes([0xFF, 0x00, 45, 6, 1, 1, 1, 2, 2, 2, 4, 9, 9, 9, 10])


def test_user_presets_build():
    for name in df.USER_PRESETS:
        for tr in ("a3", "a1"):
            pk = df.preset_packets(name, tr)
            assert all(_xor_ok(f) for f in pk), name
            assert pk[-1][:3] == bytes([0x33, 0x05, 0x0A])


@pytest.mark.parametrize("bad", [
    dict(effect="nope", colors=[(1, 2, 3)], speed_pct=1),
    dict(effect="fade1", colors=[], speed_pct=1),
    dict(effect="fade1", colors=[(1, 2, 3)] * 9, speed_pct=1),
    dict(effect="fade1", colors=[(1, 2, 3)], speed_pct=101),
])
def test_diy_validation(bad):
    with pytest.raises(ValueError):
        df.diy_packets(**bad)


def test_mix_rejects_unmixable_and_too_many():
    with pytest.raises(ValueError):
        df.diy_mix_body(["chase"], [(1, 1, 1)], 5)
    with pytest.raises(ValueError):
        df.diy_mix_body(["fade1"] * 5, [(1, 1, 1)], 5)


def test_advanced_packets_select_codes():
    _, e, p = next(x for x in _params(2) if x[0] == "Aurora" and x[1]["cmdVersion"] == 1)
    assert df.advanced_packets(p)[-1][:6] == bytes([0x33, 0x05, 0x04, 0x92, 0x01, 0x00])
    assert df.advanced_packets(p, workshop=True)[-1][:6] == bytes([0x33, 0x05, 0x04, 0x91, 0x01, 0x02])


def test_app_effect_mapping_table():
    from govee_win11.effects import EFFECTS
    expected = {"fade1": "fade1", "fade2": "fade2", "fade3": "fade3", "jump1": "jump1",
                "jump2": "jump2", "flash1": "blink1", "flash2": "blink2", "flash3": "blink3",
                "marquee1": "marquee1", "marquee2": "marquee2", "marquee3": "marquee3",
                "flow1": "stream1", "flow2": "stream2", "rainbow1": "rainbow1",
                "rainbow2": "rainbow2"}
    for app_id in EFFECTS:
        assert df.diy_key_for_app_effect(app_id) == expected.get(app_id)
    assert df.diy_key_for_app_effect("aurora") is None
    assert all(v in df.DIY_EFFECTS for v in expected.values())


def test_user_presets_use_on_device_speeds():
    assert [df.USER_PRESETS[n]["speed"] for n in
            ("orange blur", "yellow blue", "pink blue", "pink yellow")] == [40, 40, 32, 53]


# ---- Task 18: 颜色校准 applies to the colours inside DIY uploads ---------------------

def test_diy_upload_colours_are_calibrated():
    from govee_win11 import protocol
    cols = [(255, 255, 255), (200, 100, 50)]
    plain = df.diy_packets("fade3", cols, 50)
    plain_mix = df.diy_mix_packets(["fade1", "fade3"], cols, 50)
    protocol.set_color_cal(100, 80, 60)
    cal = df.diy_packets("fade3", cols, 50)
    cal_mix = df.diy_mix_packets(["fade1", "fade3"], cols, 50)
    expected = [(255, 204, 153), (200, 80, 30)]
    protocol.set_color_cal(100, 100, 100)
    assert df.diy_packets("fade3", cols, 50) == plain          # identity: unchanged bytes
    assert cal != plain and cal_mix != plain_mix
    assert cal == df.diy_packets("fade3", expected, 50)
    assert cal_mix == df.diy_mix_packets(["fade1", "fade3"], expected, 50)
    assert all(_xor_ok(f) for f in cal + cal_mix)


def test_recolored_scene_colours_are_calibrated():
    from govee_win11 import protocol
    name, e = next((n, e) for n, e in _effects() if e.get("sceneType") == 2
                   and e.get("scenceParam"))
    p = base64.b64decode(e["scenceParam"])
    want = df.recolor_scene_param(p, [(255, 204, 153)], scene_type=2)
    protocol.set_color_cal(100, 80, 60)
    got = df.recolor_scene_param(p, [(255, 255, 255)], scene_type=2)
    assert got == want, name
