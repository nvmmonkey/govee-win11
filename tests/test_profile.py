import json

from govee_win11.profile import DeviceProfile, load_profile


def test_defaults_when_missing(tmp_path):
    p = tmp_path / "p.json"
    p.write_text("{}", encoding="utf-8")
    prof = load_profile(p)
    assert prof == DeviceProfile(15, "start_at_plug", 255, 20, 0)


def test_full_mask():
    assert DeviceProfile(segment_count=14).full_mask == 0x3FFF


def test_partial_override_and_unknown_keys(tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"segment_count": 12, "junk": 1}), encoding="utf-8")
    prof = load_profile(p)
    assert prof.segment_count == 12 and prof.brightness_max == 255


def test_default_file_loads():
    assert load_profile().segment_count >= 1
