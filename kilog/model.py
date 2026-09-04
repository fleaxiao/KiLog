from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ItemState:
    item_uuid: str
    kind: str
    type_name: str
    data: Mapping[str, Any]
    raw_item: Any = field(default=None, compare=False, repr=False)

    def log_value(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "type": self.type_name,
            "data": self.data,
        }


@dataclass(frozen=True)
class BoardSnapshot:
    board_name: str
    items: Mapping[str, ItemState]
    fingerprint: str
    captured_at: str

    @classmethod
    def create(
        cls,
        board_name: str,
        items: Mapping[str, ItemState],
        captured_at: str | None = None,
    ) -> "BoardSnapshot":
        comparable = {item_id: state.log_value() for item_id, state in sorted(items.items())}
        digest = hashlib.sha256(canonical_json(comparable).encode("utf-8")).hexdigest()
        return cls(
            board_name=board_name,
            items=dict(items),
            fingerprint=digest,
            captured_at=captured_at or utc_now(),
        )


def snapshots_match_restored_state(
    restored: BoardSnapshot,
    target: BoardSnapshot,
) -> bool:
    """Compare restored states using the semantics KiLog can replay.

    KiCad may repack a footprint's library definition when a complete
    ``FootprintInstance`` is sent through the IPC API. The repacked protobuf
    can differ in default fields or child ordering even though the instance's
    replayable state was restored correctly. Tracks, vias, zones, and board
    graphics remain exact because their complete definitions are replayed.
    """
    if restored.fingerprint == target.fingerprint:
        return True
    if set(restored.items) != set(target.items):
        return False

    for item_uuid, expected in target.items.items():
        actual = restored.items[item_uuid]
        if actual.kind != expected.kind or actual.type_name != expected.type_name:
            return False
        if expected.kind != "footprint":
            if actual.log_value() != expected.log_value():
                return False
            continue
        actual_instance = {
            key: value for key, value in actual.data.items() if key != "definition"
        }
        expected_instance = {
            key: value for key, value in expected.data.items() if key != "definition"
        }
        if actual_instance != expected_instance:
            return False
    return True
