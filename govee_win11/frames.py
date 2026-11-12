"""Frame -> segment packets within a packet budget, tracking what the strip shows.

The strip can take far fewer packets per second than a 15-segment gradient needs to
refresh every segment every frame (measured ~25 packets/s). So instead of averaging
the whole frame into ``max_packets`` colour blocks (which never converges and shows
only a couple of flat blocks), each frame fixes the segments that are *most wrong*:

1. diff the target against what the strip shows (``prev``);
2. group changed segments with (near-)identical target colours -- one packet each;
3. send the ``max_packets`` groups with the biggest error reduction in their exact
   colour, leave the rest showing ``prev`` for the next frames;
4. every other changed segment rides along in whichever of those packets brings it
   closest to its target (free: the mask is part of the packet anyway).

Static content therefore converges to the exact colours within
``changed segments / packets-per-second`` and then sends nothing at all.

Per-segment rate cap: every segment command makes the H6125 firmware run its own short
fade to the new colour. Measured on the real strip (whole strip alternating red/blue):
2/s and 5/s switch cleanly, 10/s gets stuck mid-fade, 20/s blanks the strip. Many
*different* segments at 25 packets/s are fine -- the limit is how often the *same*
segment is re-commanded. So no segment is sent a new colour more often than once per
``MIN_SEGMENT_INTERVAL``: a segment commanded more recently simply keeps showing what
it shows and is reconsidered next frame with its newest target (nothing is lost; the
strip still converges to the latest frame).
"""
from __future__ import annotations

import math

from .protocol import RGB, segment_packet

Frame = list[RGB]

_QUANT = 8          # colours in the same 8-step bucket on every channel are "equal"
MERGE_DIST = 30.0   # over budget, groups closer than this (RGB distance) share a packet
MIN_SEGMENT_INTERVAL = 0.2  # s between two commands to the same segment (5/s, see above)
_TIME_EPS = 1e-6    # float drift of summed frame periods: 0.19999999 counts as 0.2
_STALE_DIST = 255.0 * math.sqrt(3.0)  # error assumed for a ``stale`` segment (max RGB distance)


def _changed(a: RGB, b: RGB, threshold: int) -> bool:
    return any(abs(x - y) > threshold for x, y in zip(a, b))


def _clamp(v: float) -> int:
    return max(0, min(255, int(round(v))))


def _dist(a, b) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _rgb(colour) -> RGB:
    return (_clamp(colour[0]), _clamp(colour[1]), _clamp(colour[2]))


def _bucket(frame: Frame, todo: list[int]) -> list[list]:
    """Group ``todo`` by quantised colour -> [[mean colour (floats), count, indices]]."""
    buckets: dict[RGB, list] = {}
    for i in todo:
        c = frame[i]
        key = (c[0] // _QUANT, c[1] // _QUANT, c[2] // _QUANT)
        g = buckets.setdefault(key, [0, 0, 0, 0, []])
        g[0] += c[0]
        g[1] += c[1]
        g[2] += c[2]
        g[3] += 1
        g[4].append(i)
    return [[(g[0] / g[3], g[1] / g[3], g[2] / g[3]), g[3], g[4]] for g in buckets.values()]


def _merge(groups: list[list], limit: int, max_dist: float = math.inf) -> list[list]:
    """Merge the closest pair of groups (count-weighted mean) until at most ``limit``
    remain or the closest pair is further apart than ``max_dist``."""
    groups = list(groups)
    while len(groups) > limit:
        best = None
        for a in range(len(groups)):
            ca = groups[a][0]
            for b in range(a + 1, len(groups)):
                d = _dist(ca, groups[b][0])
                if best is None or d < best[0]:
                    best = (d, a, b)
        d, a, b = best
        if d > max_dist:
            break
        (ca, na, ia), (cb, nb, ib) = groups[a], groups[b]
        tot = na + nb
        groups[a] = [tuple((x * na + y * nb) / tot for x, y in zip(ca, cb)), tot, ia + ib]
        del groups[b]
    return groups


def _packets(groups: list[tuple[RGB, list[int]]], shown: Frame, n: int,
             reverse: bool, now: float | None = None,
             sent_at: list[float] | None = None, interval: float = 0.0,
             stale: set[int] | None = None) -> list[bytes]:
    out = []
    for rgb, idx in sorted(groups, key=lambda g: min(g[1])):
        mask = 0
        for i in idx:
            mask |= 1 << (n - 1 - i if reverse else i)
            shown[i] = rgb
            if stale is not None:
                stale.discard(i)
            if sent_at is not None and now is not None:
                # the segment's send slot advances by the interval (not to ``now`` when
                # the send came up to ``slack`` early), so the average stays <= 1/interval
                sent_at[i] = max(now, sent_at[i] + interval)
        out.append(segment_packet(rgb, mask))
    return out


def plan_frame(frame: Frame, prev: Frame | None, max_packets: int, threshold: int = 6,
               reverse: bool = False, *, now: float | None = None,
               sent_at: list[float] | None = None,
               min_interval: float = 0.0, slack: float = 0.0,
               stale: set[int] | None = None) -> tuple[list[bytes], Frame]:
    """Plan at most ``max_packets`` segment packets moving the strip from ``prev`` (what
    it shows now) toward ``frame``. Returns ``(packets, shown)`` where ``shown`` is what
    the strip displays once they land; feed it back as the next ``prev``.

    ``prev is None`` (unknown, e.g. just connected): every segment is covered, merging
    the closest colours into ``max_packets`` blocks, so the whole strip is roughly right
    at once; later frames refine it. Otherwise segments within ``threshold`` (per
    channel) of ``prev`` are left alone and the groups with the largest error reduction
    (segments x distance) are sent in their exact colours; other changed segments join
    the packet that brings them closest (only if that is closer than ``prev``).

    Rate cap: with ``now`` and ``sent_at`` (per-segment send slot, updated in place), a
    segment is left alone this frame until ``now >= slot + min_interval - slack``; a send
    then moves its slot to ``max(now, slot + min_interval)``. ``slack`` (the engine uses
    half a frame period) lets frame timing jitter through -- at 10/12 fps a strict
    0.2 s threshold falls to the next frame and gives 3-4/s -- while the slot schedule
    keeps the long-run rate <= 1 / min_interval. ``prev is None`` still covers every
    segment (the strip state is unknown).

    ``stale`` (set of segment indices, updated in place): segments whose shown colour is
    right in ``prev`` but whose bytes on the strip are out of date (the colour
    calibration changed). They count as changed with the largest possible error, so
    they are re-sent in their exact colour -- within the packet budget and the rate cap,
    like any other change -- and leave the set once commanded."""
    n = len(frame)
    limit = max(1, max_packets)
    capped = sent_at is not None and now is not None and len(sent_at) == n
    if not capped:
        sent_at = None
    if prev is None or len(prev) != n:
        shown = list(frame)
        groups = _merge(_bucket(frame, list(range(n))), limit)
        return _packets([(_rgb(c), idx) for c, _, idx in groups], shown, n, reverse,
                        now, sent_at, min_interval, stale), shown

    shown = list(prev)
    stale_now = stale if stale else set()

    def here(i: int) -> float:  # how wrong segment i is now
        return _STALE_DIST if i in stale_now else _dist(frame[i], prev[i])

    todo = [i for i in range(n)
            if (i in stale_now or _changed(frame[i], prev[i], threshold))
            and (not capped or now - sent_at[i] >= min_interval - slack - _TIME_EPS)]
    if not todo:
        return [], shown
    groups = _bucket(frame, todo)
    if len(groups) > limit:
        groups = _merge(groups, limit, MERGE_DIST)
    scored = []
    for colour, _, idx in groups:
        rgb = _rgb(colour)
        gain, members = 0.0, []
        for i in idx:
            g = here(i) - _dist(frame[i], rgb)
            if g > 0:  # never move a segment further from its target
                gain += g
                members.append(i)
        if members:
            scored.append((gain, rgb, members))
    scored.sort(key=lambda s: -s[0])
    chosen = [(rgb, list(members)) for _, rgb, members in scored[:limit]]
    if not chosen:
        return [], shown
    covered = {i for _, members in chosen for i in members}
    for i in todo:
        if i in covered:
            continue
        best = min(chosen, key=lambda c: _dist(frame[i], c[0]))
        if _dist(frame[i], best[0]) < here(i):
            best[1].append(i)
    return _packets(chosen, shown, n, reverse, now, sent_at, min_interval, stale), shown


def encode_frame(frame: Frame, prev: Frame | None, max_packets: int, threshold: int = 6,
                 reverse: bool = False) -> list[bytes]:
    """Encode ``frame`` (``frame[0]`` = segment at the plug) into at most
    ``max_packets`` segment packets, skipping segments within ``threshold`` of
    ``prev`` on every channel. ``reverse`` maps segment i to bit ``n-1-i``."""
    return plan_frame(frame, prev, max_packets, threshold, reverse)[0]
