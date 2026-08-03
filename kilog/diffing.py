from __future__ import annotations

from collections import Counter
from typing import Any
from uuid import uuid4

from .model import BoardSnapshot, ItemState, utc_now


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


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

    return [_change("replace", path, item_uuid, kind, semantic_operation, before, after)]


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

    for item_uuid in sorted(set(before.items) | set(after.items)):
        pointer = f"/items/{_pointer_token(item_uuid)}"
        old: ItemState | None = before.items.get(item_uuid)
        new: ItemState | None = after.items.get(item_uuid)
        if old is None and new is not None:
            operation = _semantic_operation(new.kind, "add", None, new.data)
            changes.append(
                _change("add", pointer, item_uuid, new.kind, operation, after=new.log_value())
            )
        elif old is not None and new is None:
            operation = _semantic_operation(old.kind, "remove", old.data, None)
            changes.append(
                _change("remove", pointer, item_uuid, old.kind, operation, before=old.log_value())
            )
        elif old is not None and new is not None and old.log_value() != new.log_value():
            kind = new.kind
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
