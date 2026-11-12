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
