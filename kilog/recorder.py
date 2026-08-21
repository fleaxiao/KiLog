from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Protocol
from uuid import uuid4

from .diffing import build_event
from .model import BoardSnapshot
from .storage import (
    normalize_stem,
    snapshot_path,
    write_json_atomic,
    write_json_new,
)


class RecorderError(RuntimeError):
    pass


class LogFileExistsError(RecorderError):
    pass


class BoardAdapter(Protocol):
    @property
    def output_directory(self) -> Path: ...

    @property
    def board_path(self) -> Path | None: ...

    def snapshot(self) -> BoardSnapshot: ...

    def prepare_recording(self) -> BoardSnapshot: ...

    def save_copy(self, path: Path) -> None: ...

    def fill_board_copper(self, net_name: str, layer_names: tuple[str, ...]) -> int: ...

    def fanout_net(self, net_name: str, default_width_mm: float | str = 0.5) -> int: ...

    def undo_to(self, target: BoardSnapshot) -> tuple[BoardSnapshot, str]: ...

    def restore_snapshot(self, target: BoardSnapshot, description: str = "") -> BoardSnapshot: ...


@dataclass(frozen=True)
class RecorderConfig:
    pcb_stem: str = "ref"
    settle_seconds: float = 0.45
    overwrite_existing: bool = False


class Recorder:
    def __init__(self, adapter: BoardAdapter):
        self.adapter = adapter
        self.recording = False
        self.session_uuid = ""
        self.config = RecorderConfig()
        self.baseline: BoardSnapshot | None = None
        self.pending: BoardSnapshot | None = None
        self.pending_since = 0.0
        self.history: list[BoardSnapshot] = []
        self.events: list[dict] = []
        self.log_path: Path | None = None
        self.preview_position: int | None = None

    @property
    def event_count(self) -> int:
        return len(self.history)

    @property
    def recorded_position(self) -> int:
        """Record step corresponding to the current board state."""
        return self.event_count

    def start(self, config: RecorderConfig) -> BoardSnapshot:
        if self.recording:
            raise RecorderError("Recording is already running.")
        pcb_stem = normalize_stem(config.pcb_stem, ".json")
        self.config = RecorderConfig(
            pcb_stem,
            config.settle_seconds,
            config.overwrite_existing,
        )
        output = self.adapter.output_directory
        output.mkdir(parents=True, exist_ok=True)
        log_path = output / f"{pcb_stem}.json"
        if log_path.exists() and not config.overwrite_existing:
            raise LogFileExistsError(
                f"{log_path.name} already exists in the PCB directory."
            )
        baseline = self.adapter.prepare_recording()
        self.session_uuid = str(uuid4())
        self.events = []
        try:
            if config.overwrite_existing:
                write_json_atomic(log_path, self._log_document(baseline))
            else:
                write_json_new(log_path, self._log_document(baseline))
        except FileExistsError as exc:
            raise LogFileExistsError(
                f"{log_path.name} already exists in the PCB directory."
            ) from exc
        self.log_path = log_path
        self.baseline = baseline
        self.pending = None
        self.history.clear()
        self.preview_position = None
        self.recording = True
        return self.baseline

    def _log_document(
        self, baseline: BoardSnapshot, events: list[dict] | None = None
    ) -> dict:
        recorded_events = self.events if events is None else events
        board_path = getattr(self.adapter, "board_path", None)
        if board_path is None:
            board_path = baseline.board_name
        changes = []
        for event in recorded_events:
            for change in event["changes"]:
                persisted = self._persisted_change(change)
                if persisted is not None:
                    persisted["record_step"] = event["sequence"]
                    changes.append(persisted)
        return {
            "initial_pcb_path": str(board_path),
            "changes": changes,
        }

    @staticmethod
    def _persisted_change(change: dict) -> dict | None:
        if change.get("item_kind") == "footprint":
            transform = change.get("after")
            if not isinstance(transform, dict):
                return None
            return {
                "change_uuid": change["change_uuid"],
                "item_uuid": change["item_uuid"],
                "operation": "footprint.move",
                "position": transform.get("position"),
                "orientation": transform.get("orientation"),
            }
        return {key: value for key, value in change.items() if key != "op"}

    def poll(self, now: float | None = None) -> dict | None:
        if not self.recording or self.baseline is None or self.preview_position is not None:
            return None
        current = self.adapter.snapshot()
        clock = time.monotonic() if now is None else now
        if current.fingerprint == self.baseline.fingerprint:
            self.pending = None
            return None
        if self.pending is None or current.fingerprint != self.pending.fingerprint:
            self.pending = current
            self.pending_since = clock
            return None
        if clock - self.pending_since < self.config.settle_seconds:
            return None
        return self._commit(current)

    def flush(self) -> dict | None:
        if not self.recording or self.baseline is None or self.preview_position is not None:
            return None
        current = self.adapter.snapshot()
        if current.fingerprint == self.baseline.fingerprint:
            self.pending = None
            return None
        return self._commit(current)

    def _commit(self, current: BoardSnapshot) -> dict | None:
        assert self.baseline is not None
        event = build_event(
            self.baseline,
            current,
            sequence=self.event_count + 1,
            session_uuid=self.session_uuid,
        )
        self.pending = None
        if event is None:
            self.baseline = current
            return None
        assert self.log_path is not None
        coalesce = self._can_coalesce_transform(event)
        if coalesce:
            original = self.history[-1]
            merged = build_event(
                original,
                current,
                sequence=self.event_count,
                session_uuid=self.session_uuid,
            )
            assert merged is not None
            event = merged
            events = [*self.events[:-1], event]
        else:
            events = [*self.events, event]
        write_json_atomic(self.log_path, self._log_document(self.baseline, events))
        self.events = events
        if not coalesce:
            self.history.append(self.baseline)
        self.baseline = current
        return event

    def _can_coalesce_transform(self, event: dict) -> bool:
        if not self.history or not self.events:
            return False

        previous_changes = self.events[-1].get("changes", [])
        current_changes = event.get("changes", [])
        if len(previous_changes) != 1 or len(current_changes) != 1:
            return False
        previous = previous_changes[0]
        current = current_changes[0]
        transform_operations = {
            "footprint.move",
        }
        return (
            previous.get("operation") in transform_operations
            and current.get("operation") in transform_operations
            and previous.get("item_uuid") == current.get("item_uuid")
        )

    def note(self) -> Path:
        if not self.recording:
            raise RecorderError("Click Start before creating a reference snapshot.")
        if self.preview_position is not None:
            raise RecorderError("Confirm or cancel the record preview before marking it.")
        self.flush()
        path = snapshot_path(
            self.adapter.output_directory,
            self.config.pcb_stem,
            self.recorded_position,
        )
        if path.exists():
            raise RecorderError(
                f"Recorded position {self.recorded_position} is already marked as {path.name}."
            )
        self.adapter.save_copy(path)
        return path

    def undo(self) -> tuple[Path, str]:
        if not self.recording:
            raise RecorderError("Click Start before using Undo.")
        if self.preview_position is not None:
            return self.confirm_preview(), "preview"
        # A user can click undo before the debounce window writes the newest operation.  Flush it
        # first so the PCB action and the JSON file always refer to the same history entry.
        self.flush()
        self.pending = None
        return self._undo_last()

    def undo_to(self, position: int) -> tuple[Path, tuple[str, ...]]:
        """Undo recorded events until the requested history position is reached."""
        if not self.recording:
            raise RecorderError("Click Start before using Undo.")
        self.flush()
        self.pending = None
        target = int(position)
        if target < 0 or target >= len(self.history):
            raise RecorderError(
                f"Choose a recorded position from 0 to {max(0, len(self.history) - 1)}."
            )
        path: Path | None = None
        strategies: list[str] = []
        while len(self.history) > target:
            path, strategy = self._undo_last()
            strategies.append(strategy)
        assert path is not None
        return path, tuple(strategies)

    def preview(self, position: int) -> int:
        """Show a prior recorded state without truncating the log yet."""
        if not self.recording or self.baseline is None:
            raise RecorderError("Click Start before previewing recorded positions.")
        if self.preview_position is None:
            self.flush()
        target = max(0, min(int(position), len(self.history)))
        snapshot = self.baseline if target == len(self.history) else self.history[target]
        restored = self.adapter.restore_snapshot(
            snapshot,
            f"KiLog: preview recorded position {target}",
        )
        if restored.fingerprint != snapshot.fingerprint:
            raise RecorderError(f"KiCad could not preview recorded position {target}.")
        self.pending = None
        self.preview_position = None if target == len(self.history) else target
        return target

    def confirm_preview(self) -> Path:
        """Keep the previewed PCB state and discard all later recorded events."""
        if self.preview_position is None:
            raise RecorderError("Choose an earlier record position first.")
        target = self.preview_position
        snapshot = self.history[target]
        current = self.adapter.snapshot()
        if current.fingerprint != snapshot.fingerprint:
            raise RecorderError("The PCB no longer matches the selected record preview.")
        assert self.log_path is not None
        remaining_events = self.events[:target]
        write_json_atomic(
            self.log_path,
            self._log_document(snapshot, remaining_events),
        )
        self.events = remaining_events
        del self.history[target:]
        self.baseline = snapshot
        self.preview_position = None
        return self.log_path

    def _undo_last(self) -> tuple[Path, str]:
        if not self.history:
            raise RecorderError("There are no recorded operations to undo.")
        target = self.history[-1]
        restored, strategy = self.adapter.undo_to(target)
        if restored.fingerprint != target.fingerprint:
            log_name = self.log_path.name if self.log_path else "the log file"
            raise RecorderError(f"KiCad could not be restored; {log_name} was left unchanged.")
        assert self.log_path is not None
        remaining_events = self.events[:-1]
        try:
            write_json_atomic(
                self.log_path,
                self._log_document(target, remaining_events),
            )
        except OSError as exc:
            self.baseline = restored
            raise RecorderError(
                f"The PCB was undone, but {self.log_path.name} could not be updated: {exc}"
            ) from exc
        self.events = remaining_events
        self.history.pop()
        self.baseline = restored
        return self.log_path, strategy

    def end(self) -> dict | None:
        if not self.recording:
            return None
        if self.preview_position is not None:
            raise RecorderError("Confirm or cancel the record preview before ending recording.")
        event = self.flush()
        self.recording = False
        self.pending = None
        return event
