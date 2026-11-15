"""Turn per-shift staffing into per-area demand windows."""

from itertools import pairwise

from .timeutil import DAY


def coverage_segments(today: list[tuple[int, int, int]],
                      yesterday: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    """Staffing over one calendar day as (from, to, count) segments between shift boundaries.

    ``today`` and ``yesterday`` hold (start, end, count) in minutes since the respective midnight; the
    part of yesterday's shifts after midnight counts for today.
    """
    spans = list(today) + [(s - DAY, e - DAY, k) for s, e, k in yesterday if e > DAY]
    points = sorted({0, DAY} | {min(max(x, 0), DAY) for s, e, _ in spans for x in (s, e)})
    segments = []
    for a, b in pairwise(points):
        c = sum(k for s, e, k in spans if s <= a and b <= e)
        if c:
            segments.append((a, b, c))
    return segments


def median_up(counts: list[int]) -> int:
    """Median of the counts; halfway between two counts (x.5) is rounded up."""
    v = sorted(counts)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2] + 1) // 2


def start_day_windows(profile: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    """Demand windows (from, to, min) for the staffing of duties that start on one day.

    ``profile`` holds (from, to, count) segments in minutes since that day's midnight, sorted, without overlaps,
    count > 0; a segment may lie after midnight. Neighbours with the same count are merged. A window belongs to
    the day it starts, lasts at most 24 hours, and overlapping windows add up (as in Dispofold). No duty starts
    after midnight, so the count only falls there; the segments from the last boundary before midnight on are
    written as windows that all start at that boundary, one for each step down.
    """
    head, layers = list(profile), []
    if any(a >= DAY for a, _, _ in profile):
        p = max(a for a, _, _ in profile if a < DAY)
        head = [x for x in profile if x[0] < p]
        steps = [x for x in profile if x[0] >= p]
        for i, (_, b, c) in enumerate(steps):
            below = steps[i + 1][2] if i + 1 < len(steps) else 0
            if c > below:
                layers.append((p, b, c - below))
    windows = []
    for a, b, c in head:
        if windows and windows[-1][1] == a and windows[-1][2] == c and b - windows[-1][0] <= DAY:
            windows[-1] = (windows[-1][0], b, c)
        else:
            windows.append((a, b, c))
    for i in reversed(range(len(layers))):  # the longest window first
        a, b, c = layers[i]
        if windows and windows[-1][1] == a and windows[-1][2] == c and b - windows[-1][0] <= DAY:
            windows[-1] = (windows[-1][0], b, c)
            del layers[i]
            break
    return sorted(windows + layers)
