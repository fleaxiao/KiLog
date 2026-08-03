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
