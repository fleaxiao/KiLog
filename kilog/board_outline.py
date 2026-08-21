from __future__ import annotations

from collections.abc import Iterable


Point = tuple[float, float]
Segment = tuple[Point, Point]


def _near(left: Point, right: Point, tolerance: float) -> bool:
    return abs(left[0] - right[0]) <= tolerance and abs(left[1] - right[1]) <= tolerance


def closed_outline_loops(
    segments: Iterable[Segment],
    tolerance: float = 1.0,
) -> list[list[Point]]:
    """Join unordered Edge.Cuts segments into closed polygon loops."""
    remaining = list(segments)
    loops: list[list[Point]] = []
    while remaining:
        start, end = remaining.pop(0)
        loop = [start, end]
        while not _near(loop[-1], loop[0], tolerance):
            for index, (candidate_start, candidate_end) in enumerate(remaining):
                if _near(loop[-1], candidate_start, tolerance):
                    loop.append(candidate_end)
                    remaining.pop(index)
                    break
                if _near(loop[-1], candidate_end, tolerance):
                    loop.append(candidate_start)
                    remaining.pop(index)
                    break
            else:
                return []
        loop.pop()
        if len(loop) >= 3:
            loops.append(loop)
    return loops


def signed_area(points: list[Point]) -> float:
    return sum(
        left[0] * right[1] - right[0] * left[1]
        for left, right in zip(points, [*points[1:], points[0]])
    ) / 2.0


def ordered_board_loops(segments: Iterable[Segment]) -> list[list[Point]]:
    """Return the largest board outline first, followed by internal cut-outs."""
    loops = closed_outline_loops(segments)
    return sorted(loops, key=lambda loop: abs(signed_area(loop)), reverse=True)
