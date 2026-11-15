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


def percentile_up(counts: list[int], percent: int) -> int:
    """Nearest-rank percentile: the value at rank ceil(percent / 100 * n) of the sorted counts (always one of
    the counts, so never rounded between two of them)."""
    v = sorted(counts)
    return v[max(1, (percent * len(v) + 99) // 100) - 1]


def start_day_windows(profile: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Demand windows (from, to, min, max) for the staffing of duties that start on one day.

    ``profile`` holds (from, to, min, max) segments in minutes since that day's midnight, sorted, without
    overlaps, max > 0 and min <= max; a segment may lie after midnight. Neighbours with the same min and max are
    merged. A window belongs to the day it starts, lasts at most 24 hours, and overlapping windows add up, min
    and max alike (as in Dispofold). No duty starts after midnight, so min and max only fall there; the segments
    from the last boundary before midnight on are written as windows that all start at that boundary, one for
    each step down. A step in which max falls less than min cannot be written (a window needs min <= max); the
    min of the earlier segment is lowered then, so the max stays exact.
    """
    head, layers = list(profile), []
    if any(a >= DAY for a, _, _, _ in profile):
        p = max(a for a, _, _, _ in profile if a < DAY)
        head = [x for x in profile if x[0] < p]
        steps = [x for x in profile if x[0] >= p]
        below_min = below_max = 0  # what the later (longer) layers add up to
        for a, b, lo, hi in reversed(steps):
            layer_max = hi - below_max
            layer_min = min(lo - below_min, layer_max)
            if layer_max > 0:
                layers.append((p, b, layer_min, layer_max))
            below_min, below_max = below_min + layer_min, below_max + layer_max
        layers.reverse()
    windows = []
    for a, b, lo, hi in head:
        if windows and windows[-1][1] == a and windows[-1][2:] == (lo, hi) and b - windows[-1][0] <= DAY:
            windows[-1] = (windows[-1][0], b, lo, hi)
        else:
            windows.append((a, b, lo, hi))
    for i in reversed(range(len(layers))):  # the longest window first
        a, b, lo, hi = layers[i]
        if windows and windows[-1][1] == a and windows[-1][2:] == (lo, hi) and b - windows[-1][0] <= DAY:
            windows[-1] = (windows[-1][0], b, lo, hi)
            del layers[i]
            break
    return sorted(windows + layers)
