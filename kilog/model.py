from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import copy
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


def restored_item_data(state: ItemState, *, ignore_zone_fill: bool = False) -> dict:
    """Editable state, excluding regenerated child IDs and zone fill caches."""
    data = copy.deepcopy(dict(state.data))
    if state.kind == "zone":
        if ignore_zone_fill:
            data.pop("filled", None)
        data.pop("filled_polygons", None)
    if state.kind == "footprint":
        data.pop("definition", None)
        for name in ("reference_field", "value_field", "datasheet_field", "description_field"):
            field_data = data.get(name)
            if isinstance(field_data, dict):
                text = field_data.get("text")
                if isinstance(text, dict):
                    text.pop("id", None)
    return data


def snapshots_match_restored_state(
    restored: BoardSnapshot,
    target: BoardSnapshot,
    *,
    ignore_zone_fill: bool = False,
) -> bool:
    """Compare restored states using the semantics KiLog can replay.

    KiCad may repack a footprint's library definition when a complete
    ``FootprintInstance`` is sent through the IPC API. The repacked protobuf
    can differ in default fields or child ordering even though the instance's
    replayable state was restored correctly. Field text IDs may also be
    regenerated. The filled flag is an observable operation and must match
    unless explicitly tolerating fill invalidation during structural undo.
    """
    if restored.fingerprint == target.fingerprint:
        return True
    if set(restored.items) != set(target.items):
        return False

    for item_uuid, expected in target.items.items():
        actual = restored.items[item_uuid]
        if actual.kind != expected.kind or actual.type_name != expected.type_name:
            return False
        if restored_item_data(actual, ignore_zone_fill=ignore_zone_fill) != restored_item_data(
            expected, ignore_zone_fill=ignore_zone_fill
        ):
            return False
    return True
