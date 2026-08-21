from __future__ import annotations

from collections import Counter
import math
from typing import Any
from uuid import uuid4

from .model import BoardSnapshot, ItemState, utc_now


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _point(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        return (
            float(value.get("x_nm", value.get("x"))),
            float(value.get("y_nm", value.get("y"))),
        )
    except (TypeError, ValueError):
        return None


def _arc_points(value: dict[str, Any], steps: int = 32) -> list[tuple[float, float]]:
    start = _point(value.get("start"))
    mid = _point(value.get("mid"))
    end = _point(value.get("end"))
    if start is None or mid is None or end is None:
        return []
    x1, y1 = start
    x2, y2 = mid
    x3, y3 = end
    divisor = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(divisor) < 1e-9:
        return [start, end]
    ux = (
        (x1 * x1 + y1 * y1) * (y2 - y3)
        + (x2 * x2 + y2 * y2) * (y3 - y1)
        + (x3 * x3 + y3 * y3) * (y1 - y2)
    ) / divisor
    uy = (
        (x1 * x1 + y1 * y1) * (x3 - x2)
        + (x2 * x2 + y2 * y2) * (x1 - x3)
        + (x3 * x3 + y3 * y3) * (x2 - x1)
    ) / divisor
    angles = [math.atan2(y - uy, x - ux) for x, y in (start, mid, end)]
    start_angle, mid_angle, end_angle = angles
    tau = 2 * math.pi
    ccw_span = (end_angle - start_angle) % tau
    mid_span = (mid_angle - start_angle) % tau
    span = ccw_span if mid_span <= ccw_span else ccw_span - tau
    radius = math.hypot(x1 - ux, y1 - uy)
    return [
        (
            ux + radius * math.cos(start_angle + span * index / steps),
            uy + radius * math.sin(start_angle + span * index / steps),
        )
        for index in range(steps + 1)
    ]


def _pairs(points: list[tuple[float, float]], closed: bool = False):
    if closed and len(points) > 2:
        points = [*points, points[0]]
    return list(zip(points, points[1:]))


def edge_segments(snapshot: BoardSnapshot) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for item in snapshot.items.values():
        if item.kind != "shape" or item.data.get("layer") != "BL_Edge_Cuts":
            continue
        shape = item.data.get("shape", {})
        if "segment" in shape:
            start = _point(shape["segment"].get("start"))
            end = _point(shape["segment"].get("end"))
            if start is not None and end is not None:
                segments.append((start, end))
        elif "rectangle" in shape:
            top_left = _point(shape["rectangle"].get("top_left"))
            bottom_right = _point(shape["rectangle"].get("bottom_right"))
            if top_left is not None and bottom_right is not None:
                x1, y1 = top_left
                x2, y2 = bottom_right
                segments.extend(_pairs([(x1, y1), (x2, y1), (x2, y2), (x1, y2)], True))
        elif "arc" in shape:
            segments.extend(_pairs(_arc_points(shape["arc"])))
        elif "circle" in shape:
            center = _point(shape["circle"].get("center"))
            radius_point = _point(shape["circle"].get("radius_point"))
            if center is not None and radius_point is not None:
                radius = math.dist(center, radius_point)
                points = [
                    (
                        center[0] + radius * math.cos(2 * math.pi * index / 64),
                        center[1] + radius * math.sin(2 * math.pi * index / 64),
                    )
                    for index in range(64)
                ]
                segments.extend(_pairs(points, True))
        elif "polygon" in shape:
            for polygon in shape["polygon"].get("polygons", []):
                nodes = polygon.get("outline", {}).get("nodes", [])
                points = [_point(node.get("point")) for node in nodes]
                segments.extend(_pairs([point for point in points if point is not None], True))
    return segments


def _point_on_segment(
    point: tuple[float, float],
    segment: tuple[tuple[float, float], tuple[float, float]],
) -> bool:
    (x, y), ((x1, y1), (x2, y2)) = point, segment
    cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
    length = math.hypot(x2 - x1, y2 - y1)
    tolerance = 1.0
    return (
        abs(cross) <= tolerance * max(1.0, length)
        and min(x1, x2) - tolerance <= x <= max(x1, x2) + tolerance
        and min(y1, y2) - tolerance <= y <= max(y1, y2) + tolerance
    )


def _footprint_is_on_board(item: ItemState, edge_segments) -> bool:
    if item.kind != "footprint" or not edge_segments:
        return True
    position = _point(item.data.get("position"))
    if position is None:
        return True
    if any(_point_on_segment(position, segment) for segment in edge_segments):
        return True
    x, y = position
    crossings = 0
    for (x1, y1), (x2, y2) in edge_segments:
        if (y1 > y) != (y2 > y):
            intersection_x = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if intersection_x > x:
                crossings += 1
    return crossings % 2 == 1


def _footprint_transform(item: ItemState) -> dict[str, Any]:
    return {
        "position": item.data.get("position"),
        "orientation": item.data.get("orientation"),
    }


def _semantic_operation(kind: str, op: str, before: Any, after: Any) -> str:
    semantic_kind = "routing" if kind == "track" else kind
    if op == "add":
        return f"{semantic_kind}.add"
    if op == "remove":
        return f"{semantic_kind}.remove"

    before_map = before if isinstance(before, dict) else {}
    after_map = after if isinstance(after, dict) else {}
    if kind == "footprint":
        moved = before_map.get("position") != after_map.get("position")
        rotated = before_map.get("orientation") != after_map.get("orientation")
        if moved and rotated:
            return "footprint.move_rotate"
        if moved:
            return "footprint.move"
        if rotated:
            return "footprint.rotate"
        return "footprint.modify"
    if kind == "track":
        return "routing.modify"
    if kind == "zone":
        before_keys = set(before_map)
        after_keys = set(after_map)
        changed = {
            key for key in before_keys | after_keys if before_map.get(key) != after_map.get(key)
        }
        if changed and all("fill" in key.lower() for key in changed):
            return "zone.refill"
        return "zone.modify"
    return f"{semantic_kind}.modify"


def _field_changes(
    before: Any,
    after: Any,
    path: str,
    item_uuid: str,
    kind: str,
    semantic_operation: str,
) -> list[dict[str, Any]]:
    if before == after:
        return []

    if isinstance(before, dict) and isinstance(after, dict):
        changes: list[dict[str, Any]] = []
        for key in sorted(set(before) | set(after)):
            child_path = f"{path}/{_pointer_token(str(key))}"
            if key not in before:
                changes.append(
                    _change("add", child_path, item_uuid, kind, semantic_operation, after=after[key])
                )
            elif key not in after:
                changes.append(
                    _change("remove", child_path, item_uuid, kind, semantic_operation, before=before[key])
                )
            else:
                changes.extend(
                    _field_changes(
                        before[key], after[key], child_path, item_uuid, kind, semantic_operation
                    )
                )
        return changes

    if isinstance(before, list) and isinstance(after, list):
        changes = []
        shared_length = min(len(before), len(after))
        for index in range(shared_length):
            changes.extend(
                _field_changes(
                    before[index],
                    after[index],
                    f"{path}/{index}",
                    item_uuid,
                    kind,
                    semantic_operation,
                )
            )
        for index in range(len(before) - 1, shared_length - 1, -1):
            changes.append(
                _change(
                    "remove", f"{path}/{index}", item_uuid, kind, semantic_operation,
                    before=before[index],
                )
            )
        for index in range(shared_length, len(after)):
            changes.append(
                _change(
                    "add", f"{path}/{index}", item_uuid, kind, semantic_operation,
                    after=after[index],
                )
            )
        return changes

    return [_change("replace", path, item_uuid, kind, semantic_operation, before, after)]


def _footprint_transform_change(
    before: ItemState,
    after: ItemState,
    pointer: str,
    operation: str,
) -> dict[str, Any]:
    """Fuse position and angle into one transform change."""
    before_transform = {
        "position": before.data.get("position"),
        "orientation": before.data.get("orientation"),
    }
    after_transform = {
        "position": after.data.get("position"),
        "orientation": after.data.get("orientation"),
    }
    return _change(
        "replace",
        f"{pointer}/data/transform",
        after.item_uuid,
        after.kind,
        operation,
        before_transform,
        after_transform,
    )


def _change(
    op: str,
    path: str,
    item_uuid: str,
    kind: str,
    semantic_operation: str,
    before: Any = None,
    after: Any = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "change_uuid": str(uuid4()),
        "item_uuid": item_uuid,
        "item_kind": kind,
        "operation": semantic_operation,
        "op": op,
        "path": path,
    }
    if op in ("remove", "replace"):
        result["before"] = before
    if op in ("add", "replace"):
        result["after"] = after
    return result


def build_event(
    before: BoardSnapshot,
    after: BoardSnapshot,
    sequence: int,
    session_uuid: str,
) -> dict[str, Any] | None:
    changes: list[dict[str, Any]] = []
    before_edges = edge_segments(before)
    after_edges = edge_segments(after)

    for item_uuid in sorted(set(before.items) | set(after.items)):
        pointer = f"/items/{_pointer_token(item_uuid)}"
        old: ItemState | None = before.items.get(item_uuid)
        new: ItemState | None = after.items.get(item_uuid)
        if old is not None and not _footprint_is_on_board(old, before_edges):
            old = None
        if new is not None and not _footprint_is_on_board(new, after_edges):
            new = None
        if old is None and new is not None:
            operation = (
                "footprint.move"
                if new.kind == "footprint"
                else _semantic_operation(new.kind, "add", None, new.data)
            )
            changes.append(
                _change(
                    "add",
                    pointer,
                    item_uuid,
                    new.kind,
                    operation,
                    after=_footprint_transform(new) if new.kind == "footprint" else new.log_value(),
                )
            )
        elif old is not None and new is None:
            if old.kind == "footprint":
                continue
            operation = _semantic_operation(old.kind, "remove", old.data, None)
            changes.append(
                _change(
                    "remove",
                    pointer,
                    item_uuid,
                    old.kind,
                    operation,
                    before=_footprint_transform(old) if old.kind == "footprint" else old.log_value(),
                )
            )
        elif old is not None and new is not None and old.log_value() != new.log_value():
            kind = new.kind
            if kind == "footprint":
                before_transform = _footprint_transform(old)
                after_transform = _footprint_transform(new)
                if before_transform != after_transform:
                    changes.append(
                        _footprint_transform_change(old, new, pointer, "footprint.move")
                    )
            else:
                operation = _semantic_operation(kind, "replace", old.data, new.data)
                changes.extend(
                    _field_changes(
                        old.log_value(), new.log_value(), pointer, item_uuid, kind, operation
                    )
                )

    if not changes:
        return None

    counts = Counter(change["operation"] for change in changes)
    return {
        "$schema": "https://kilog.local/schemas/operation-log-v1.json",
        "schema_version": 1,
        "event_uuid": str(uuid4()),
        "session_uuid": session_uuid,
        "sequence": sequence,
        "timestamp": utc_now(),
        "source": {
            "application": "KiCad PCB Editor",
            "board": after.board_name,
            "state": "live_unsaved_memory",
        },
        "base_fingerprint": before.fingerprint,
        "result_fingerprint": after.fingerprint,
        "summary": {
            "change_count": len(changes),
            "item_count": len({change["item_uuid"] for change in changes}),
            "operations": dict(sorted(counts.items())),
        },
        "changes": changes,
    }
