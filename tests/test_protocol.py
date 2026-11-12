from govee_win11.protocol import *

def test_power_on_matches_known_frame():
    assert power_packet(True).hex() == "3301010000000000000000000000000000000033"
    assert power_packet(False).hex() == "3301000000000000000000000000000000000032"

def test_segment_packet_layout():
    p = segment_packet((0, 0, 255), 0x7FFF)
    assert len(p) == 20 and p[:5] == bytes([0x33, 0x05, 0x15, 0x01, 0])
    assert p[5:7] == bytes([0, 255]) and p[12:14] == bytes([0xFF, 0x7F])
    x = 0
    for b in p[:19]: x ^= b
    assert p[19] == x

def test_payload_too_long():
    import pytest
    with pytest.raises(ValueError): ble_packet(1, [0] * 18)

def test_multi_packets_shape():
    data = bytes(range(40))  # 14 进首包, 17 进中间包, 9 进末包
    ps = multi_packets(data)
    assert [p[0] for p in ps] == [0xA3] * 3
    assert ps[0][1:5] == bytes([0x00, 0x01, 3, 0x02]) and ps[0][5:19] == data[:14]
    assert ps[1][1] == 0x01 and ps[1][2:19] == data[14:31]
    assert ps[2][1] == 0xFF and ps[2][2:11] == data[31:] and ps[2][11:19] == bytes(8)

def test_multi_packets_requires_more_than_first_packet():
    import pytest
    with pytest.raises(ValueError): multi_packets(bytes(14))   # 实际情景参数都 ≥ 43 字节；未知行为不猜

def test_scene_packets_without_param():
    ps = scene_packets(0x0104, b"")
    assert len(ps) == 1 and ps[0][:5] == bytes([0x33, 0x05, 0x04, 0x04, 0x01])


# ---- Task 18: 颜色校准（白平衡） — protocol.calibrate is the choke point ----------------

import pytest  # noqa: E402

from govee_win11 import protocol  # noqa: E402


def test_color_cal_identity_by_default():
    assert protocol.get_color_cal() == (100, 100, 100)
    for c in ((0, 0, 0), (255, 255, 255), (1, 128, 254)):
        assert protocol.calibrate(c) == c
    assert segment_packet((12, 34, 56), 0x7FFF)[4:7] == bytes([12, 34, 56])


def test_segment_packet_bytes_unchanged_at_100():
    """At 100/100/100 the bytes equal the uncalibrated layout built by hand."""
    for r in range(256):
        rgb = (r, 255 - r, (r * 7) % 256)
        raw = ble_packet(0x05, [0x15, 0x01, *rgb, 0, 0, 0, 0, 0, 0x34, 0x12])
        assert segment_packet(rgb, 0x1234) == raw


def test_calibrate_formula():
    protocol.set_color_cal(100, 80, 50)
    assert protocol.calibrate((255, 255, 255)) == (255, round(255 * 0.8), round(255 * 0.5))
    assert protocol.calibrate((200, 10, 3)) == (200, 8, 2)
    protocol.set_color_cal(0, 0, 0)
    assert protocol.calibrate((255, 128, 1)) == (0, 0, 0)


def test_segment_packet_is_calibrated_with_valid_checksum():
    protocol.set_color_cal(90, 70, 100)
    p = segment_packet((255, 255, 255), 0x7FFF)
    assert p[:4] == bytes([0x33, 0x05, 0x15, 0x01])
    assert p[4:7] == bytes([round(255 * 0.9), round(255 * 0.7), 255])
    assert p[12:14] == bytes([0xFF, 0x7F])
    x = 0
    for b in p[:19]:
        x ^= b
    assert p[19] == x


@pytest.mark.parametrize("gains", [(101, 100, 100), (-1, 100, 100), (100, 50.5, 100),
                                   (True, 100, 100), (100, 100, "100")])
def test_set_color_cal_validates(gains):
    with pytest.raises(ValueError):
        protocol.set_color_cal(*gains)
    assert protocol.get_color_cal() == (100, 100, 100)
