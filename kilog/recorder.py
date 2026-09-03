from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Protocol, TYPE_CHECKING
from uuid import uuid4

from .diffing import build_event
from .model import BoardSnapshot
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
        output.mkdir(parents=True, exist_ok=True)
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

    def resume(self, branch: ReplayBranch) -> BoardSnapshot:
        """Truncate a replay at its current position and continue recording there."""
        if self.recording:
            raise RecorderError("Recording is already running.")
        if len(branch.snapshots) != len(branch.steps) + 1:
            raise RecorderError("The replay branch has incomplete board history.")

        baseline = branch.snapshots[-1]
        current = self.adapter.snapshot()
        if current.fingerprint != baseline.fingerprint:
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
        config = RecorderConfig(pcb_stem=branch.path.stem)
        self.config = config
        self.session_uuid = str(uuid4())
        self.initial_pcb_path = branch.initial_pcb_path
        self.log_path = branch.path
        write_json_atomic(branch.path, self._log_document(baseline, events))
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
        *,
        silkscreen_last: bool = False,
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
        if silkscreen_last:
            steps = self._move_silkscreen_changes_to_last_step(steps, baseline)
        return {
            "initial_pcb_path": str(board_path),
            "steps": steps,
        }

    @classmethod
    def _move_silkscreen_changes_to_last_step(
        cls,
        steps: list[dict],
        final_snapshot: BoardSnapshot,
    ) -> list[dict]:
        """Collect footprint silk-field edits into one final replay step."""
        regular_steps = []
        silkscreen_changes: dict[tuple[str, str], dict] = {}
        unused_step_uuids = []

        for step in steps:
            regular_changes = []
            for change in step["changes"]:
                if change.get("operation") != "footprint.field.modify":
                    regular_changes.append(change)
                    continue
                final_change = cls._change_with_final_snapshot_value(
                    change,
                    final_snapshot,
                )
                key = (str(change.get("id", "")), str(change.get("path", "")))
                silkscreen_changes[key] = final_change

            if regular_changes:
                regular_steps.append(
                    {
                        "step": len(regular_steps) + 1,
                        "step_uuid": step["step_uuid"],
                        "changes": regular_changes,
                    }
                )
            else:
                unused_step_uuids.append(step["step_uuid"])

        if silkscreen_changes:
            regular_steps.append(
                {
                    "step": len(regular_steps) + 1,
                    "step_uuid": (
                        unused_step_uuids[-1] if unused_step_uuids else str(uuid4())
                    ),
                    "changes": list(silkscreen_changes.values()),
                }
            )
        return regular_steps

    @staticmethod
    def _change_with_final_snapshot_value(
        change: dict,
        final_snapshot: BoardSnapshot,
    ) -> dict:
        """Retarget a deferred silk edit to the recording's final board state."""
        item_uuid = change.get("id")
        path = change.get("path")
        state = final_snapshot.items.get(item_uuid)
        if state is None or not isinstance(path, str):
            return copy.deepcopy(change)

        parts = [
            token.replace("~1", "/").replace("~0", "~")
            for token in path.split("/")[1:]
        ]
        if len(parts) < 3 or parts[:2] != ["items", item_uuid]:
            return copy.deepcopy(change)

        value = state.log_value()
        try:
            for token in parts[2:]:
                value = value[int(token)] if isinstance(value, list) else value[token]
        except (KeyError, IndexError, TypeError, ValueError):
            result = copy.deepcopy(change)
            result.pop("value", None)
            result["delete"] = True
            return result

        result = copy.deepcopy(change)
        result["value"] = copy.deepcopy(value)
        result.pop("delete", None)
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
        previous = self.baseline
        new_events = []
        new_history = []
        for target in self._recording_targets(self.baseline, current):
            event = build_event(
                previous,
                target,
                sequence=self.event_count + len(new_events) + 1,
                session_uuid=self.session_uuid,
            )
            if event is not None:
                new_history.append(previous)
                new_events.append(event)
            previous = target
        self.pending = None
        if not new_events:
            self.baseline = current
            return None
        assert self.log_path is not None
        reroute = self._coalesced_reroute_event(current, new_events)
        if reroute is not None:
            events = [*self.events[:-1], reroute]
            write_json_atomic(self.log_path, self._log_document(self.baseline, events))
            self.events = events
            self.baseline = current
            return reroute

        event = new_events[-1]
        coalesce = len(new_events) == 1 and self._can_coalesce_transform(event)
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
            events = [*self.events, *new_events]
        write_json_atomic(self.log_path, self._log_document(self.baseline, events))
        self.events = events
        if not coalesce:
            self.history.extend(new_history)
        self.baseline = current
        return event

    def _coalesced_reroute_event(
        self,
        current: BoardSnapshot,
        new_events: list[dict],
    ) -> dict | None:
        """Fuse a settled delete followed by redrawing the same connected path."""
        if not self.history or not self.events or len(new_events) != 1:
            return None

        previous_changes = self.events[-1].get("changes", [])
        current_changes = new_events[0].get("changes", [])
        if not previous_changes or not current_changes:
            return None
        if not all(
            change.get("item_kind") == "track"
            and change.get("operation") == "routing.remove"
            for change in previous_changes
        ):
            return None
        if not all(
            change.get("item_kind") == "track"
            and change.get("operation") in {"routing.add", "routing.modify"}
            for change in current_changes
        ) or not any(
            change.get("operation") == "routing.add" for change in current_changes
        ):
            return None

        original = self.history[-1]
        if len(self._recording_targets(original, current)) != 1:
            return None
        merged = build_event(
            original,
            current,
            sequence=self.event_count,
            session_uuid=self.session_uuid,
        )
        if merged is None:
            return None
        operations = {change.get("operation") for change in merged.get("changes", [])}
        if not {"routing.add", "routing.remove"} <= operations or not all(
            change.get("item_kind") == "track" for change in merged.get("changes", [])
        ):
            return None
        return merged

    @staticmethod
    def _recording_targets(
        before: BoardSnapshot,
        after: BoardSnapshot,
    ) -> list[BoardSnapshot]:
        """Build intermediate states with one connected routing edit each."""
        changed_tracks = []
        for item_uuid in sorted(set(before.items) | set(after.items)):
            old = before.items.get(item_uuid)
            new = after.items.get(item_uuid)
            kind = new.kind if new is not None else old.kind if old is not None else None
            if kind != "track":
                continue
            if old is None or new is None or old.log_value() != new.log_value():
                changed_tracks.append(item_uuid)

        track_groups = Recorder._connected_track_groups(
            before,
            after,
            changed_tracks,
        )
        if len(track_groups) <= 1:
            return [after]

        track_ids = set(changed_tracks)
        items = {
            item_uuid: state
            for item_uuid, state in after.items.items()
            if item_uuid not in track_ids
        }
        items.update(
            {
                item_uuid: state
                for item_uuid, state in before.items.items()
                if item_uuid in track_ids
            }
        )

        targets = []
        for group in track_groups:
            for item_uuid in group:
                target_state = after.items.get(item_uuid)
                if target_state is None:
                    items.pop(item_uuid, None)
                else:
                    items[item_uuid] = target_state
            targets.append(
                BoardSnapshot.create(
                    after.board_name,
                    items,
                    captured_at=after.captured_at,
                )
            )
        return targets

    @staticmethod
    def _connected_track_groups(
        before: BoardSnapshot,
        after: BoardSnapshot,
        track_ids: list[str],
    ) -> list[list[str]]:
        """Group changed segments that form one logical before/after path.

        KiCad commonly represents a path edit by removing the old segments and
        adding new UUIDs.  Shared endpoints in either state, including the fixed
        endpoints that join the old and new paths, identify those changes as one
        routing action.
        """
        if not track_ids:
            return []

        def point(value):
            if not isinstance(value, dict):
                return None
            x = value.get("x_nm", value.get("x"))
            y = value.get("y_nm", value.get("y"))
            if x is None or y is None:
                return None
            try:
                return int(x), int(y)
            except (TypeError, ValueError):
                return None

        endpoints = {}
        nets = {}
        for item_uuid in track_ids:
            states = (
                state
                for state in (before.items.get(item_uuid), after.items.get(item_uuid))
                if state is not None
            )
            item_endpoints = set()
            item_nets = set()
            for state in states:
                for name in ("start", "end"):
                    if (endpoint := point(state.data.get(name))) is not None:
                        item_endpoints.add(endpoint)
                net = state.data.get("net")
                if isinstance(net, dict) and isinstance(net.get("name"), str):
                    item_nets.add(net["name"].casefold())
            endpoints[item_uuid] = item_endpoints
            nets[item_uuid] = item_nets

        remaining = set(track_ids)
        groups = []
        for first in track_ids:
            if first not in remaining:
                continue
            remaining.remove(first)
            group = [first]
            pending = [first]
            while pending:
                current = pending.pop()
                connected = [
                    candidate
                    for candidate in sorted(remaining)
                    if nets[current] & nets[candidate]
                    and endpoints[current] & endpoints[candidate]
                ]
                for candidate in connected:
                    remaining.remove(candidate)
                    group.append(candidate)
                    pending.append(candidate)
            groups.append(sorted(group))
        return groups

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
        assert self.log_path is not None
        assert self.baseline is not None
        write_json_atomic(
            self.log_path,
            self._log_document(self.baseline, silkscreen_last=True),
        )
        self.recording = False
        self.pending = None
        return event
