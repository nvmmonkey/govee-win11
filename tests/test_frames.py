import pytest

from govee_win11.frames import encode_frame, plan_frame
from govee_win11.protocol import segment_packet

N = 14
FULL = (1 << N) - 1


def decode(pkt: bytes):
    """-> (rgb, mask) of a segment packet."""
    assert pkt[0] == 0x33 and pkt[1] == 0x05 and pkt[2] == 0x15 and pkt[3] == 0x01
    return (pkt[4], pkt[5], pkt[6]), pkt[12] | (pkt[13] << 8)


def distinct_colors(n):
    # well separated colors, all channels differ by > 8 steps
    return [((i * 37) % 256, (i * 91 + 40) % 256, (255 - i * 17) % 256) for i in range(n)]


def test_single_color_one_packet():
    frame = [(10, 200, 30)] * N
    pkts = encode_frame(frame, None, max_packets=10)
    assert pkts == [segment_packet((10, 200, 30), 0x3FFF)]


def test_unchanged_frame_sends_nothing():
    frame = distinct_colors(N)
    assert encode_frame(frame, list(frame), max_packets=10) == []


def test_within_threshold_counts_as_unchanged():
    prev = [(100, 100, 100)] * N
    frame = [(106, 94, 100)] * N
    assert encode_frame(frame, prev, max_packets=10, threshold=6) == []
    frame2 = [(107, 100, 100)] * N
    assert len(encode_frame(frame2, prev, max_packets=10, threshold=6)) == 1


def test_only_changed_segments():
    prev = [(0, 0, 0)] * N
    frame = list(prev)
    frame[2] = (255, 0, 0)
    frame[5] = (255, 0, 0)
    frame[9] = (0, 0, 255)
    pkts = [decode(p) for p in encode_frame(frame, prev, max_packets=10)]
    assert sorted(pkts) == sorted([((255, 0, 0), (1 << 2) | (1 << 5)), ((0, 0, 255), 1 << 9)])


def test_quantized_colors_group_together():
    frame = [(16, 16, 16)] * 7 + [(18, 20, 22)] * 7  # same 8-step bucket
    pkts = [decode(p) for p in encode_frame(frame, None, max_packets=10)]
    assert len(pkts) == 1
    assert pkts[0][1] == FULL


def test_budget_merges_closest():
    frame = distinct_colors(N)
    pkts = [decode(p) for p in encode_frame(frame, None, max_packets=4)]
    assert len(pkts) == 4
    union = 0
    for _, mask in pkts:
        assert union & mask == 0
        union |= mask
    assert union == FULL


def test_budget_merges_closest_pair_weighted():
    # two near reds (3 + 1 segments) and one far blue -> reds merge, weighted avg
    frame = [(200, 0, 0)] * 3 + [(240, 0, 0)] + [(0, 0, 255)] * (N - 4)
    pkts = sorted(decode(p) for p in encode_frame(frame, None, max_packets=2))
    assert pkts == sorted([((210, 0, 0), 0b1111), ((0, 0, 255), FULL & ~0b1111)])


def test_max_packets_floor_is_one():
    pkts = encode_frame(distinct_colors(N), None, max_packets=0)
    assert len(pkts) == 1
    assert decode(pkts[0])[1] == FULL


def test_reverse_bit_order():
    frame = [(0, 0, 0)] * N
    frame[0] = (255, 0, 0)
    prev = [(0, 0, 0)] * N
    pkts = [decode(p) for p in encode_frame(frame, prev, max_packets=10, reverse=True)]
    assert pkts == [((255, 0, 0), 1 << 13)]


def test_plan_frame_reports_what_strip_shows():
    frame = [(200, 0, 0)] * 3 + [(240, 0, 0)] + [(0, 0, 255)] * (N - 4)
    pkts, shown = plan_frame(frame, None, max_packets=2)
    assert len(pkts) == 2
    assert shown == [(210, 0, 0)] * 4 + [(0, 0, 255)] * (N - 4)
    # unchanged segments keep the previously shown colour
    prev = [(1, 2, 3)] * N
    frame2 = list(prev)
    frame2[0] = (90, 90, 90)
    _, shown2 = plan_frame(frame2, prev, max_packets=5)
    assert shown2 == [(90, 90, 90)] + [(1, 2, 3)] * (N - 1)


# ---- over budget with a known strip state (final review critical 1) ----

def _run_static(frame, prev, budget, frames=20):
    sent = []
    for _ in range(frames):
        pkts, prev = plan_frame(frame, prev, budget)
        sent.append(len(pkts))
    return prev, sent


def test_over_budget_sends_exact_colours_of_worst_groups():
    prev = [(0, 0, 0)] * N
    frame = distinct_colors(N)
    pkts, shown = plan_frame(frame, prev, max_packets=2)
    assert 1 <= len(pkts) <= 2
    decoded = [decode(p) for p in pkts]
    # every packet carries a colour that some segment actually wants (no averages)
    for rgb, _ in decoded:
        assert rgb in frame
    # segments either moved closer to their target or kept what they showed
    for i in range(N):
        d = lambda c: sum((a - b) ** 2 for a, b in zip(c, frame[i]))
        assert d(shown[i]) <= d(prev[i])


def test_static_frame_converges_exactly_then_stops():
    frame = distinct_colors(N)
    shown, sent = _run_static(frame, [(0, 0, 0)] * N, budget=2)
    assert shown == frame
    k = sent.index(0)
    assert all(s == 0 for s in sent[k:])  # nothing resent once converged
    assert k <= N  # at most one packet per distinct colour


def test_unknown_strip_covers_every_segment_then_refines():
    frame = distinct_colors(N)
    pkts, shown = plan_frame(frame, None, max_packets=2)
    union = 0
    for _, mask in map(decode, pkts):
        union |= mask
    assert union == FULL  # whole strip roughly right at once
    shown, _ = _run_static(frame, shown, budget=2)
    assert shown == frame


def test_near_identical_groups_share_a_packet_over_budget():
    prev = [(0, 0, 0)] * N
    frame = [(200, 0, 0)] * 5 + [(210, 8, 0)] * 5 + [(0, 0, 250)] * (N - 10)
    pkts, shown = plan_frame(frame, prev, max_packets=2)
    assert len(pkts) == 2
    assert all(c != (0, 0, 0) for c in shown)  # both reds and the blue covered


def test_changed_segments_ride_along_when_closer():
    prev = [(0, 0, 0)] * N
    # big error: 10 white segments; small: 4 light grey ones that white brings closer
    frame = [(255, 255, 255)] * 10 + [(150, 150, 150)] * (N - 10)
    pkts, shown = plan_frame(frame, prev, max_packets=1)
    assert len(pkts) == 1
    assert decode(pkts[0]) == ((255, 255, 255), FULL)
    assert shown == [(255, 255, 255)] * N


def test_never_moves_a_segment_away_from_its_target():
    prev = [(100, 100, 100)] * N
    frame = [(255, 0, 0)] * 10 + [(100, 100, 100)] * (N - 10)
    frame[12] = (0, 0, 255)  # blue: red would be worse than the grey it shows
    pkts, shown = plan_frame(frame, prev, max_packets=1)
    assert shown[12] == (100, 100, 100)
    assert shown[:10] == [(255, 0, 0)] * 10


# ---- per-segment rate cap (the firmware fades each command; see MIN_SEGMENT_INTERVAL) ----

def test_rate_cap_holds_back_recently_sent_segments():
    from govee_win11.frames import MIN_SEGMENT_INTERVAL
    assert MIN_SEGMENT_INTERVAL == 0.2
    sent_at = [float("-inf")] * N
    prev = [(0, 0, 0)] * N
    red = [(255, 0, 0)] * N
    pkts, shown = plan_frame(red, prev, 10, now=0.0, sent_at=sent_at, min_interval=0.2)
    assert shown == red and sent_at == [0.0] * N
    blue = [(0, 0, 255)] * N
    # 0.1 s later: every segment was commanded too recently -> nothing is sent
    pkts, shown2 = plan_frame(blue, shown, 10, now=0.1, sent_at=sent_at, min_interval=0.2)
    assert pkts == [] and shown2 == red and sent_at == [0.0] * N
    # once 0.2 s have passed the newest target goes out
    pkts, shown3 = plan_frame(blue, shown2, 10, now=0.2, sent_at=sent_at, min_interval=0.2)
    assert shown3 == blue and sent_at == [0.2] * N


def test_rate_cap_is_per_segment():
    sent_at = [float("-inf")] * N
    prev = [(0, 0, 0)] * N
    f1 = list(prev)
    f1[0] = (255, 0, 0)
    _, shown = plan_frame(f1, prev, 10, now=0.0, sent_at=sent_at, min_interval=0.2)
    f2 = list(f1)
    f2[0] = (0, 255, 0)   # segment 0 again: too soon
    f2[5] = (0, 0, 255)   # segment 5: never sent, goes out at once
    pkts, shown = plan_frame(f2, shown, 10, now=0.05, sent_at=sent_at, min_interval=0.2)
    assert [decode(p) for p in pkts] == [((0, 0, 255), 1 << 5)]
    assert shown[0] == (255, 0, 0) and shown[5] == (0, 0, 255)


def test_rate_cap_held_segment_does_not_ride_along():
    sent_at = [float("-inf")] * N
    prev = [(0, 0, 0)] * N
    _, shown = plan_frame([(255, 0, 0)] + prev[1:], prev, 10, now=0.0, sent_at=sent_at,
                          min_interval=0.2)
    frame = [(255, 255, 255)] * N  # segment 0 wants white too, but was just sent
    pkts, shown = plan_frame(frame, shown, 1, now=0.1, sent_at=sent_at, min_interval=0.2)
    assert decode(pkts[0]) == ((255, 255, 255), FULL & ~1)
    assert shown[0] == (255, 0, 0) and sent_at[0] == 0.0


def test_rate_cap_unknown_strip_still_covers_everything():
    sent_at = [0.0] * N
    pkts, shown = plan_frame([(9, 9, 9)] * N, None, 10, now=0.05, sent_at=sent_at,
                             min_interval=0.2)
    # covered anyway; the send slots advance one interval (keeps the average rate)
    assert shown == [(9, 9, 9)] * N and sent_at == [0.2] * N


def test_rate_cap_slack_absorbs_jitter_but_keeps_5_per_s_on_average():
    # slack (half a frame period, from the engine) lets a send that is a little early go
    # out; the segment's slot then advances by exactly the interval, not to "now", so
    # the long-run rate stays <= 1 / interval at any frame rate
    sent_at = [float("-inf")] * N
    prev = [(0, 0, 0)] * N
    sends, shown = [], prev
    fps = 12.0
    for k in range(int(10 * fps)):
        now = k / fps
        colour = ((k * 40) % 256, 0, 255 - (k * 40) % 256)  # a new colour every frame
        pkts, shown = plan_frame([colour] * N, shown, 10, now=now, sent_at=sent_at,
                                 min_interval=0.2, slack=0.5 / fps)
        if pkts:
            sends.append(now)
    gaps = [b - a for a, b in zip(sends, sends[1:])]
    assert min(gaps) >= 0.2 - 0.5 / fps - 1e-9
    assert len(sends) / 10.0 >= 4.6   # ~5/s (4.8 measured: frame granularity), not 4/s
    assert len(sends) <= 10 * 5 + 1


# ---- Task 18: 颜色校准 — packets calibrated, the planner's view stays intended ---------

def test_plan_frame_packets_calibrated_shown_intended():
    from govee_win11 import protocol
    frame = [(255, 255, 255)] * 15
    plain, shown_plain = plan_frame(frame, None, 3)
    protocol.set_color_cal(100, 90, 80)
    cal, shown = plan_frame(frame, None, 3)
    protocol.set_color_cal(100, 100, 100)
    assert shown == shown_plain == frame                      # UI preview: intended colour
    assert cal == [segment_packet((255, 230, 204), 0x7FFF)]   # wire: calibrated
    assert cal != plain


def test_stale_segments_resent_exactly_and_cleared():
    frame = [(255, 255, 255)] * 10 + [(255, 0, 0)] * 5
    stale = set(range(15))
    packets, shown = plan_frame(frame, list(frame), 5, stale=stale)
    assert shown == frame
    assert sorted(packets) == sorted([segment_packet((255, 255, 255), 0x03FF),
                                      segment_packet((255, 0, 0), 0x7C00)])
    assert stale == set()
    assert plan_frame(frame, list(frame), 5, stale=stale)[0] == []   # then quiet again


def test_stale_segments_obey_budget_and_rate_cap():
    frame = [(i * 17, 0, 0) for i in range(15)]                      # 15 distinct colours
    stale = set(range(15))
    sent_at = [float("-inf")] * 15
    sent_at[3] = 9.9                                                 # commanded 0.1 s ago
    packets, shown = plan_frame(frame, list(frame), 4, now=10.0, sent_at=sent_at,
                                min_interval=0.2, stale=stale)
    assert len(packets) == 4                                         # packet budget holds
    assert stale == {3}                                              # cap holds it back
    assert sent_at[3] == 9.9 and shown[3] == frame[3]
    # the others went out (exact or riding along in the nearest packet); the next frames
    # refine the approximate ones as ordinary changes
    later, _ = plan_frame(frame, shown, 4, now=10.3, sent_at=sent_at, min_interval=0.2,
                          stale=stale)
    assert stale == set() and later
