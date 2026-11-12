from govee_win11.scenes import Scene, categories, load_scenes, packets_for


def _by_name(name):
    return next(s for s in load_scenes() if s.name == name)


def test_loads_12_categories():
    scenes = load_scenes()
    assert len(categories(scenes)) == 12
    assert len(scenes) == 149


def test_sunrise_no_param_single_packet():
    pk = packets_for(_by_name("Sunrise"), 0)
    assert len(pk) == 1 and len(pk[0]) == 20
    assert pk[0][:3] == bytes([0x33, 0x05, 0x04])


def test_aurora_variant_selection():
    a = _by_name("Aurora")
    p1 = packets_for(a, 1)
    assert p1[-1][:5] == bytes([0x33, 0x05, 0x04, 0xF1, 0x08])
    assert len(p1) > 2
    p0 = packets_for(a, 0)
    assert p0[-1][:5] == bytes([0x33, 0x05, 0x04, 215, 0])
    assert len(p0) != len(p1)


def test_unknown_cmd_version_falls_back_to_first():
    a = _by_name("Aurora")
    assert packets_for(a, 99) == packets_for(a, a.effects[0]["cmdVersion"])


def test_no_effects_uses_scene_code():
    s = Scene(id=1, name="x", category="c", hint="", effects=[])
    assert len(packets_for(s, 0)) == 1


def _scene_type_byte(packets):
    first = packets[0]
    assert first[0] == 0xA3 and first[1] == 0x00
    return first[4]


def test_scene_upload_uses_catalogue_scene_type():
    # Halloween code 110 and Sweet code 107 are sceneType 1; the others stay 2
    halloween = _by_name("Halloween")
    a = next(e for e in halloween.effects if e.get("sceneCode") == 110)
    only_a = Scene(id=1, name="h", category="c", hint="", effects=[a])
    pk = packets_for(only_a, a["cmdVersion"])
    assert pk[-1][:5] == bytes([0x33, 0x05, 0x04, 110, 0])
    assert _scene_type_byte(pk) == 1

    sweet = _by_name("Sweet")
    e = next(x for x in sweet.effects if x.get("sceneCode") == 107)
    pk = packets_for(Scene(id=2, name="s", category="c", hint="", effects=[e]), e["cmdVersion"])
    assert pk[-1][:5] == bytes([0x33, 0x05, 0x04, 107, 0])
    assert _scene_type_byte(pk) == 1

    aurora = packets_for(_by_name("Aurora"), 1)
    assert _scene_type_byte(aurora) == 2
