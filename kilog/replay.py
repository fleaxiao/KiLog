from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Any, Protocol

from .model import BoardSnapshot


class ReplayError(RuntimeError):
    pass


class ReplayAdapter(Protocol):
    def prepare_replay(self, initial_pcb_path: str) -> BoardSnapshot: ...

    def snapshot(self) -> BoardSnapshot: ...

    def restore_snapshot(self, target: BoardSnapshot, description: str = "") -> BoardSnapshot: ...

    def apply_change(self, change: dict[str, Any]) -> BoardSnapshot: ...


@dataclass(frozen=True)
class ReplayLog:
    path: Path
    initial_pcb_path: str
    changes: tuple[dict[str, Any], ...]


def load_replay_log(path: Path) -> ReplayLog:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayError(f"Could not read {path.name}: {exc}") from exc
    if not isinstance(document, dict):
        raise ReplayError("The log root must be a JSON object.")
    initial_path = document.get("initial_pcb_path")
    changes = document.get("changes")
    if not isinstance(initial_path, str) or not initial_path.strip():
        raise ReplayError("The log has no valid initial_pcb_path.")
    if not isinstance(changes, list):
        raise ReplayError("The log has no valid changes array.")
    validated: list[dict[str, Any]] = []
    for index, change in enumerate(changes, 1):
        if not isinstance(change, dict):
            raise ReplayError(f"Change #{index} must be a JSON object.")
        if not isinstance(change.get("item_uuid"), str) or not change["item_uuid"]:
            raise ReplayError(f"Change #{index} has no item_uuid.")
        if not isinstance(change.get("operation"), str) or not change["operation"]:
            raise ReplayError(f"Change #{index} has no operation.")
        validated.append(dict(change))
    return ReplayLog(path.resolve(), initial_path, tuple(validated))


class ReplayController:
    """Deterministic fixed-step playback over a KiLog JSON document."""

    def __init__(self, adapter: ReplayAdapter, step_seconds: float = 0.4):
        self.adapter = adapter
        self.step_seconds = step_seconds
        self.log: ReplayLog | None = None
        self.baseline: BoardSnapshot | None = None
        self._position_snapshots: list[BoardSnapshot] = []
        self.position = 0
        self.playing = False
        self.speed = 1.0
        self._next_step_at: float | None = None

    @property
    def total(self) -> int:
        return len(self.log.changes) if self.log else 0

    def load(self, path: Path) -> ReplayLog:
        log = load_replay_log(path)
        baseline = self.adapter.prepare_replay(log.initial_pcb_path)
        if log.changes and self._all_changes_already_applied(baseline, log.changes):
            raise ReplayError(
                "KiCad did not reach the saved initial PCB state; the board still looks "
                "fully replayed. Close other active tools, reload the PCB, and try again."
            )
        self.log = log
        self.baseline = baseline
        self._position_snapshots = [baseline]
        self.position = 0
        self.playing = False
        self._next_step_at = None
        return log

    @staticmethod
    def _all_changes_already_applied(
        snapshot: BoardSnapshot,
        changes: tuple[dict[str, Any], ...],
    ) -> bool:
        comparable = 0
        applied = 0
        for change in changes:
            item_uuid = change["item_uuid"]
            operation = change["operation"]
            state = snapshot.items.get(item_uuid)
            if operation == "footprint.move" and state is not None:
                comparable += 1
                if (
                    state.data.get("position") == change.get("position")
                    and state.data.get("orientation") == change.get("orientation")
                ):
                    applied += 1
            elif operation.endswith(".add"):
                comparable += 1
                applied += int(state is not None)
            elif operation.endswith(".remove"):
                comparable += 1
                applied += int(state is None)
        return comparable > 0 and applied == comparable

    def unload(self) -> None:
        self.pause()
        self.log = None
        self.baseline = None
        self._position_snapshots = []
        self.position = 0

    def set_speed(self, speed: float) -> None:
        if speed <= 0:
            raise ValueError("Playback speed must be greater than zero.")
        self.speed = speed
        if self.playing:
            self._next_step_at = time.monotonic() + self.step_seconds / speed

    def play(self, now: float | None = None) -> None:
        self._require_loaded()
        if self.position >= self.total:
            self.seek(0)
        self.playing = True
        clock = time.monotonic() if now is None else now
        self._next_step_at = clock

    def pause(self) -> None:
        self.playing = False
        self._next_step_at = None

    def toggle(self, now: float | None = None) -> None:
        if self.playing:
            self.pause()
        else:
            self.play(now)

    def tick(self, now: float | None = None) -> bool:
        if not self.playing:
            return False
        clock = time.monotonic() if now is None else now
        if self._next_step_at is not None and clock < self._next_step_at:
            return False
        try:
            changed = self.step_forward()
        except Exception:
            self.pause()
            raise
        if not changed or self.position >= self.total:
            self.pause()
        else:
            self._next_step_at = clock + self.step_seconds / self.speed
        return changed

    def step_forward(self) -> bool:
        log = self._require_loaded()
        if self.position >= len(log.changes):
            return False
        next_snapshot = self.adapter.apply_change(log.changes[self.position])
        self.position += 1
        del self._position_snapshots[self.position :]
        self._position_snapshots.append(next_snapshot)
        return True

    def step_back(self) -> bool:
        if self.position <= 0:
            return False
        self.seek(self.position - 1)
        return True

    def seek(self, position: int) -> None:
        log = self._require_loaded()
        target = max(0, min(int(position), len(log.changes)))
        if target == self.position:
            return
        was_playing = self.playing
        self.pause()

        if target < self.position:
            target_snapshot = self._position_snapshots[target]
            self.adapter.restore_snapshot(
                target_snapshot,
                f"KiLog: replay back to step {target}",
            )
            self.position = target
        else:
            start_position = self.position
            start_snapshot = self._position_snapshots[start_position]
            try:
                while self.position < target:
                    self.step_forward()
            except Exception:
                self.adapter.restore_snapshot(
                    start_snapshot,
                    f"KiLog: recover replay step {start_position}",
                )
                self.position = start_position
                del self._position_snapshots[start_position + 1 :]
                raise

        if was_playing and target < self.total:
            self.play()

    def skip(self, amount: int) -> None:
        self.seek(self.position + amount)

    def _require_loaded(self) -> ReplayLog:
        if self.log is None or self.baseline is None:
            raise ReplayError("Choose a KiLog JSON file first.")
        return self.log
