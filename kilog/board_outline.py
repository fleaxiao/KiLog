from __future__ import annotations

from collections.abc import Iterable
import math


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


def point_in_polygon(point: Point, polygon: list[Point]) -> bool:
    """Return whether a point is inside a polygon using an even-odd ray cast."""
    x, y = point
    inside = False
    for left, right in zip(polygon, [*polygon[1:], polygon[0]]):
        if (left[1] > y) == (right[1] > y):
            continue
        crossing_x = left[0] + (y - left[1]) * (right[0] - left[0]) / (
            right[1] - left[1]
        )
        if x < crossing_x:
            inside = not inside
    return inside


def point_inside_board(point: Point, loops: list[list[Point]]) -> bool:
    """Return whether a point is in the outer board polygon and outside cut-outs."""
    if not loops:
        return False
    on_outer_edge = any(
        point_segment_distance(point, start, end) <= 1.0
        for start, end in zip(loops[0], [*loops[0][1:], loops[0][0]])
    )
    if not on_outer_edge and not point_in_polygon(point, loops[0]):
        return False
    return not any(
        point_in_polygon(point, hole)
        or any(
            point_segment_distance(point, start, end) <= 1.0
            for start, end in zip(hole, [*hole[1:], hole[0]])
        )
        for hole in loops[1:]
    )


def point_segment_distance(point: Point, start: Point, end: Point) -> float:
    """Return the shortest distance between a point and a line segment."""
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    if dx == 0 and dy == 0:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    ratio = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / (
        dx * dx + dy * dy
    )
    ratio = max(0.0, min(1.0, ratio))
    nearest = (start[0] + ratio * dx, start[1] + ratio * dy)
    return math.hypot(point[0] - nearest[0], point[1] - nearest[1])


def circle_inside_board(center: Point, radius: float, loops: list[list[Point]]) -> bool:
    """Return whether a circle is fully inside the board and outside cut-outs."""
    if not point_inside_board(center, loops):
        return False
    return all(
        point_segment_distance(center, start, end) >= radius
        for loop in loops
        for start, end in zip(loop, [*loop[1:], loop[0]])
    )
