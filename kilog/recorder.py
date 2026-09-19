from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TYPE_CHECKING
from uuid import uuid4

from .diffing import build_event
from .model import BoardSnapshot, snapshots_match_restored_state
from .storage import (
    normalize_stem,
    snapshot_path,
    write_json_atomic,
    write_json_new,
)

if TYPE_CHECKING:
    from .replay import ReplayBranch


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

    def fill_board_copper(self, net_name: str | None, layer_names: tuple[str, ...]) -> int: ...

    def refill_board_copper(self) -> None: ...

    def fanout_net(
        self,
        net_name: str,
        default_width_mm: float | str = 0.4,
        via_diameter_mm: float | str = 0.5,
        via_drill_mm: float | str = 0.3,
    ) -> int: ...

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
        self.initial_pcb_path: str | None = None

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
        log_path = output / f"{pcb_stem}.json"
        if log_path.exists() and not config.overwrite_existing:
            raise LogFileExistsError(
                f"{log_path.name} already exists in the PCB directory."
            )
        baseline = self.adapter.prepare_recording()
        self.session_uuid = str(uuid4())
        self.events = []
        self.initial_pcb_path = str(
            getattr(self.adapter, "board_path", None) or baseline.board_name
        )
        self.log_path = log_path
        self.baseline = baseline
        self.pending = None
        self.history.clear()
        self.preview_position = None
        self.recording = True
        return self.baseline

    def resume(self, branch: ReplayBranch) -> BoardSnapshot:
        """Truncate a replay at its current position and continue recording there."""
        if self.recording:
            raise RecorderError("Recording is already running.")
        if len(branch.snapshots) != len(branch.steps) + 1:
            raise RecorderError("The replay branch has incomplete board history.")

        baseline = branch.snapshots[-1]
        current = self.adapter.snapshot()
        if not snapshots_match_restored_state(current, baseline):
            raise RecorderError("The PCB no longer matches the selected replay position.")

        events = [
            {
                "sequence": index,
                "event_uuid": step["step_uuid"],
                "changes": [],
                "persisted_changes": copy.deepcopy(step["changes"]),
            }
            for index, step in enumerate(branch.steps, 1)
        ]
        config = RecorderConfig(pcb_stem=branch.path.stem, overwrite_existing=True)
        self.config = config
        self.session_uuid = str(uuid4())
        self.initial_pcb_path = branch.initial_pcb_path
        self.log_path = branch.path
        self.events = events
        self.history = list(branch.snapshots[:-1])
        self.baseline = baseline
        self.pending = None
        self.preview_position = None
        self.recording = True
        return baseline

    def _log_document(
        self,
        baseline: BoardSnapshot,
        events: list[dict] | None = None,
    ) -> dict:
        recorded_events = self.events if events is None else events
        board_path = self.initial_pcb_path
        if board_path is None:
            board_path = getattr(self.adapter, "board_path", None) or baseline.board_name
        steps = []
        for event in recorded_events:
            persisted_changes = event.get("persisted_changes")
            if persisted_changes is not None:
                changes = copy.deepcopy(persisted_changes)
            else:
                changes = []
                for change in event["changes"]:
                    persisted = self._persisted_change(change)
                    if persisted is not None:
                        changes.append(persisted)
            if changes:
                steps.append(
                    {
                        "step": event["sequence"],
                        "step_uuid": event["event_uuid"],
                        "changes": changes,
                    }
                )
        return {
            "initial_pcb_path": str(board_path),
            "steps": steps,
        }

    @staticmethod
    def _finalize_document(
        document: dict, final_snapshot: BoardSnapshot, initial_snapshot: BoardSnapshot,
    ) -> dict:
        """Compact the saved log only; leave live history available for undo."""
        result = copy.deepcopy(document)
        merged = []
        previous_transform_id = None
        for step in result["steps"]:
            changes = step["changes"]
            transform_id = (
                changes[0].get("id")
                if len(changes) == 1 and changes[0].get("operation") == "footprint.move"
                else None
            )
            if transform_id is not None and transform_id == previous_transform_id:
                merged[-1]["changes"] = changes
            else:
                merged.append(step)
            previous_transform_id = transform_id

        # Classify standalone silk text/graphics across their entire lifecycle,
        # including items created and removed within the recording.
        silk_items = {}
        for snapshot in (initial_snapshot, final_snapshot):
            for item_id, state in snapshot.items.items():
                if state.kind in {"text", "shape"}:
                    silk_items[item_id] = silk_items.get(item_id, True) and (
                        state.data.get("layer") in {"BL_F_SilkS", "BL_B_SilkS"})
        for step in merged:
            for change in step["changes"]:
                if str(change.get("operation", "")).split(".")[0] not in {"text", "shape"}:
                    continue
                data = change.get("item", {}).get("data", {})
                item_id = change.get("id") or data.get("id", {}).get("value")
                if data or change.get("path", "").endswith("/layer"):
                    layer = data.get("layer") if data else change.get("value")
                    silk_items[item_id] = silk_items.get(item_id, True) and (
                        layer in {"BL_F_SilkS", "BL_B_SilkS"})

        regular_steps = []
        silk_changes = {}
        silk_item_changes = []
        silk_step_uuid = None
        for step in merged:
            regular_changes = []
            for change in step["changes"]:
                if change.get("operation") != "footprint.field.modify":
                    item_id = change.get("id") or change.get("item", {}).get(
                        "data", {}).get("id", {}).get("value")
                    if silk_items.get(item_id, False):
                        silk_item_changes.append(change)
                    else:
                        regular_changes.append(change)
                    continue
                item_id, path = change["id"], change["path"]
                state = final_snapshot.items.get(item_id)
                if state is None:
                    continue
                # A later footprint move/rotation can also move its silk fields.
                # Use the final board value rather than an earlier absolute value.
                tokens = [part.replace("~1", "/").replace("~0", "~")
                          for part in path.split("/")[1:]]
                if tokens[:2] != ["items", item_id]:
                    raise RecorderError("Invalid silkscreen field path in recording.")
                value = state.log_value()
                try:
                    for token in tokens[2:]:
                        value = value[int(token)] if isinstance(value, list) else value[token]
                except (KeyError, IndexError, TypeError, ValueError):
                    change.pop("value", None)
                    change["delete"] = True
                else:
                    change["value"] = copy.deepcopy(value)
                    change.pop("delete", None)
                silk_changes[(item_id, path)] = change
            if regular_changes:
                step["changes"] = regular_changes
                regular_steps.append(step)
            else:
                silk_step_uuid = step["step_uuid"]
        if silk_changes or silk_item_changes:
            regular_steps.append({
                "step_uuid": silk_step_uuid or str(uuid4()),
                # Parent field replacements precede any nested field updates.
                "changes": silk_item_changes + sorted(
                    silk_changes.values(), key=lambda c: c["path"].count("/")),
            })
        for sequence, step in enumerate(regular_steps, 1):
            step["step"] = sequence
        result["steps"] = regular_steps
        return result

    @staticmethod
    def _persisted_change(change: dict) -> dict | None:
        if change.get("operation") == "footprint.move":
            transform = change.get("after")
            if not isinstance(transform, dict):
                return None
            return {
                "id": change["item_uuid"],
                "operation": "footprint.move",
                "position": transform.get("position"),
                "orientation": transform.get("orientation"),
            }

        operation = change.get("operation")
        item_uuid = change.get("item_uuid")
        path = change.get("path")
        op = change.get("op")
        if not all(isinstance(value, str) and value for value in (
            item_uuid,
            operation,
            path,
        )):
            return None

        persisted = {
            "operation": operation,
        }
        root_item_change = len(path.split("/")) == 3
        if root_item_change:
            if op in {"add", "replace"}:
                item = change.get("after")
                type_name = item.get("type") if isinstance(item, dict) else None
                data = item.get("data") if isinstance(item, dict) else None
                if not isinstance(type_name, str) or not isinstance(data, dict):
                    return None
                data = copy.deepcopy(data)
                native_id = data.get("id")
                if native_id is None:
                    data["id"] = {"value": item_uuid}
                elif not isinstance(native_id, dict):
                    return None
                elif native_id.get("value") is None:
                    native_id["value"] = item_uuid
                elif native_id.get("value") != item_uuid:
                    return None
                persisted["item"] = {
                    "type": type_name,
                    "data": data,
                }
            else:
                persisted["id"] = item_uuid
            return persisted

        persisted["id"] = item_uuid
        persisted["path"] = path
        if op in {"add", "replace"} and "after" in change:
            persisted["value"] = change["after"]
        elif op == "remove":
            persisted["delete"] = True
        else:
            return None
        return persisted

    def poll(self, now: float | None = None) -> dict | None:
        if not self.recording or self.baseline is None or self.preview_position is not None:
            return None
        current = self.adapter.snapshot()
        if current.fingerprint == self.baseline.fingerprint:
            self.pending = None
            return None
        return self._commit(current)

    def flush(self, *, single_step: bool = False) -> dict | None:
        """Record the live board, optionally keeping every change in one step."""
        if not self.recording or self.baseline is None or self.preview_position is not None:
            return None
        current = self.adapter.snapshot()
        if current.fingerprint == self.baseline.fingerprint:
            self.pending = None
            return None
        return self._commit(current, single_step=single_step)

    def _commit(
        self,
        current: BoardSnapshot,
        *,
        single_step: bool = False,
    ) -> dict | None:
        """Record one observed state without inventing transaction boundaries.

        single_step remains accepted for callers that submit one IPC commit.
        All observations now stay intact; IPC snapshots do not expose KiCad's
        undo transaction IDs and cannot identify missed intermediate commits.
        """
        assert self.baseline is not None
        if self._rewind_to_recorded_state(current):
            return None
        if snapshots_match_restored_state(current, self.baseline):
            self.baseline = current
            self.pending = None
            return None
        event = build_event(
            self.baseline, current,
            sequence=self.event_count + 1, session_uuid=self.session_uuid,
        )
        self.pending = None
        if event is None:
            self.baseline = current
            return None
        assert self.log_path is not None
        events = [*self.events, event]
        self.history.append(self.baseline)
        self.events = events
        self.baseline = current
        return event

    def _rewind_to_recorded_state(self, current: BoardSnapshot) -> bool:
        """Follow an editor undo back to a state already observed in this session.

        IPC has no undo notifications. Matching the complete board state lets
        polling and Stop recognize a return to history without logging an
        inverse edit or issuing another undo command to KiCad.
        """
        # Prefer exact fill-state history. Only tolerate fill invalidation when
        # item membership changed, as when undo removes fanout or created zones.
        structural_change = self.baseline is not None and set(current.items) != set(self.baseline.items)
        for ignore_fill in ((False, True) if structural_change else (False,)):
            for position in range(len(self.history) - 1, -1, -1):
                if snapshots_match_restored_state(
                    current, self.history[position], ignore_zone_fill=ignore_fill
                ):
                    del self.events[position:]
                    del self.history[position:]
                    self.baseline = current
                    self.pending = None
                    return True
        return False

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
        # Capture any edit not yet observed by the polling timer before undoing.
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
        if len(self.history) < target:
            raise RecorderError(
                "KiCad's native Undo crossed the selected recording position. "
                "Its actual result was kept; recording positions are not native transactions."
            )
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
        if not snapshots_match_restored_state(restored, snapshot):
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
        if not snapshots_match_restored_state(current, snapshot):
            raise RecorderError("The PCB no longer matches the selected record preview.")
        assert self.log_path is not None
        remaining_events = self.events[:target]
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
        if not snapshots_match_restored_state(restored, target):
            # One native undo may span several observations or stop at a state
            # missed between polls. Keep its actual result; never force target.
            if self._rewind_to_recorded_state(restored):
                assert self.log_path is not None
                return self.log_path, strategy
            self._commit(restored)
            raise RecorderError(
                "KiCad performed one native Undo, but its result is between recorded "
                "positions. The actual result was recorded without changing the board. "
                "IPC recording positions are not native undo transactions."
            )
        assert self.log_path is not None
        remaining_events = self.events[:-1]
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
        assert self.log_path is not None
        assert self.baseline is not None
        try:
            writer = write_json_atomic if self.config.overwrite_existing else write_json_new
            initial = self.history[0] if self.history else self.baseline
            document = self._finalize_document(
                self._log_document(self.baseline), self.baseline, initial,
            )
            writer(self.log_path, document)
        except FileExistsError as exc:
            raise LogFileExistsError(
                f"{self.log_path.name} appeared during recording; it was not overwritten. "
                "The recording is still in memory."
            ) from exc
        self.recording = False
        self.pending = None
        return event
