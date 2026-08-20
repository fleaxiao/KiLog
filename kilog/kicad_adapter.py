from __future__ import annotations

import copy
import os
from pathlib import Path
import time

from google.protobuf.json_format import MessageToDict, ParseDict
from kipy.board import Board
from kipy.board_types import (
    ArcTrack,
    BoardShape,
    BoardText,
    BoardTextBox,
    Dimension,
    FootprintInstance,
    Track,
    Via,
    Zone,
)
from kipy.kicad import KiCad
from kipy.geometry import Angle, Vector2
from kipy.proto.common.commands.editor_commands_pb2 import RAS_OK
from kipy.proto.common import types as common_types
from kipy.proto.common.types import KiCadObjectType

from .model import BoardSnapshot, ItemState
from .recorder import RecorderError
from .replay import ReplayError


SNAPSHOT_TYPES = (
    KiCadObjectType.KOT_PCB_FOOTPRINT,
    KiCadObjectType.KOT_PCB_TRACE,
    KiCadObjectType.KOT_PCB_ARC,
    KiCadObjectType.KOT_PCB_VIA,
    KiCadObjectType.KOT_PCB_ZONE,
    KiCadObjectType.KOT_PCB_SHAPE,
    KiCadObjectType.KOT_PCB_TEXT,
    KiCadObjectType.KOT_PCB_TEXTBOX,
    KiCadObjectType.KOT_PCB_DIMENSION,
)

ITEM_KINDS = (
    (FootprintInstance, "footprint"),
    ((Track, ArcTrack), "track"),
    (Via, "via"),
    (Zone, "zone"),
    (BoardShape, "shape"),
    ((BoardText, BoardTextBox), "text"),
    (Dimension, "dimension"),
)

REPLAY_ITEM_TYPES = (
    FootprintInstance,
    Track,
    ArcTrack,
    Via,
    Zone,
    BoardShape,
    BoardText,
    BoardTextBox,
    Dimension,
)


class KiCadBoardAdapter:
    """Adapter over the official KiCad 9/10 IPC API.

    All snapshots are collected from the live editor model, so the board does not need to be
    saved before an operation is visible to KiLog.
    """

    REVERT_SETTLE_SECONDS = 0.65

    def __init__(self, kicad: KiCad, board: Board):
        self.kicad = kicad
        self.board = board

    @property
    def output_directory(self) -> Path:
        board_path = self.board_path
        if board_path is not None:
            return board_path.parent
        project_directory = self._project_directory()
        return project_directory or Path.cwd()

    @property
    def board_path(self) -> Path | None:
        """Best available absolute path of the PCB open in the editor."""
        name = (self.board.name or "").strip()
        project_directory = self._project_directory()

        if name:
            board_path = Path(name).expanduser()
            if board_path.is_absolute():
                return board_path.resolve()
            if project_directory is not None:
                return (project_directory / board_path).resolve()

        # KiCad 10 may clear board_filename when it populates project.path in
        # DocumentSpecifier because both currently share a protobuf oneof.
        if project_directory is not None:
            try:
                project_name = (self.board.document.project.name or "").strip()
            except (AttributeError, ValueError):
                project_name = ""
            if project_name:
                return (project_directory / f"{project_name}.kicad_pcb").resolve()
        return None

    def _project_directory(self) -> Path | None:
        """Return KiCad's directory for the board when its filename is relative."""
        try:
            project_path = (self.board.document.project.path or "").strip()
        except (AttributeError, ValueError):
            project_path = ""

        if project_path:
            directory = Path(project_path).expanduser()
            if directory.is_absolute():
                return directory.resolve()

        try:
            project = self.board.get_project()
            expanded = project.expand_text_variables("${KIPRJMOD}").strip()
        except Exception:
            return None

        # KiCad leaves an unknown variable untouched.  Do not mistake that for
        # a real relative directory and accidentally resolve it below the plugin.
        if not expanded or expanded == "${KIPRJMOD}":
            return None
        directory = Path(expanded).expanduser()
        return directory.resolve() if directory.is_absolute() else None

    @staticmethod
    def _kind(item) -> str:
        for item_type, kind in ITEM_KINDS:
            if isinstance(item, item_type):
                return kind
        return "board_item"

    @staticmethod
    def _clone_item(item):
        """Clone through the wrapper constructor to preserve nested proto references."""
        try:
            return type(item)(item.proto)
        except TypeError:
            return copy.deepcopy(item)

    def snapshot(self) -> BoardSnapshot:
        states: dict[str, ItemState] = {}
        for item in self.board.get_items(types=SNAPSHOT_TYPES):
            proto = item.proto
            item_uuid = proto.id.value
            if not item_uuid:
                continue
            data = MessageToDict(
                proto,
                preserving_proto_field_name=True,
                use_integers_for_enums=False,
                always_print_fields_with_no_presence=True,
            )
            states[item_uuid] = ItemState(
                item_uuid=item_uuid,
                kind=self._kind(item),
                type_name=proto.DESCRIPTOR.full_name,
                data=data,
                raw_item=self._clone_item(item),
            )
        return BoardSnapshot.create(self.board.name or "<untitled>", states)

    def save_copy(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.board.save_as(str(path), overwrite=False, include_project=False)

    def prepare_replay(self, initial_pcb_path: str) -> BoardSnapshot:
        """Reset the matching open board to its saved on-disk replay baseline."""
        current_path = self.board_path
        expected_path = Path(initial_pcb_path).expanduser().resolve()
        if current_path is None or os.path.normcase(str(current_path)) != os.path.normcase(
            str(expected_path)
        ):
            current_label = str(current_path) if current_path else "<untitled>"
            raise ReplayError(
                f"This log belongs to {expected_path}, but KiCad currently has "
                f"{current_label} open. Open the logged PCB first."
            )
        try:
            self.board.revert()
        except Exception as exc:
            raise ReplayError(f"Could not restore {expected_path.name}: {exc}") from exc
        # RevertDocument returns before PCB Editor has necessarily replaced its live
        # model.  Reading immediately can therefore capture the old, fully-replayed
        # state and make every subsequent move a no-op.
        time.sleep(self.REVERT_SETTLE_SECONDS)
        return self._snapshot_with_retry()

    def _snapshot_with_retry(self, attempts: int = 6) -> BoardSnapshot:
        last_error: Exception | None = None
        for _ in range(attempts):
            try:
                return self.snapshot()
            except Exception as exc:  # KiCad may report AS_BUSY during an interactive tool.
                last_error = exc
                time.sleep(0.08)
        raise RecorderError(f"Could not read the PCB state after Undo: {last_error}") from last_error

    def undo_to(self, target: BoardSnapshot) -> tuple[BoardSnapshot, str]:
        """Use KiCad's undo stack first, then exactly restore from memory if needed."""
        response = self.kicad.run_action("common.Interactive.undo")
        if response.status == RAS_OK:
            current = self._snapshot_with_retry()
            if current.fingerprint == target.fingerprint:
                return current, "native"

        restored = self._restore_exactly(target)
        return restored, "snapshot"

    def _restore_exactly(
        self,
        target: BoardSnapshot,
        description: str = "KiLog: undo recorded operation",
    ) -> BoardSnapshot:
        current = self._snapshot_with_retry()
        current_ids = set(current.items)
        target_ids = set(target.items)

        remove_ids = [current.items[item_id].raw_item.id for item_id in current_ids - target_ids]
        create_items = [target.items[item_id].raw_item for item_id in target_ids - current_ids]
        update_items = [
            target.items[item_id].raw_item
            for item_id in current_ids & target_ids
            if current.items[item_id].log_value() != target.items[item_id].log_value()
        ]

        if not remove_ids and not create_items and not update_items:
            return current

        commit = self.board.begin_commit()
        try:
            if remove_ids:
                self.board.remove_items_by_id(remove_ids)
            if create_items:
                self.board.create_items(create_items)
            if update_items:
                self.board.update_items(update_items)
            self.board.push_commit(commit, description)
        except Exception:
            self.board.drop_commit(commit)
            raise

        restored = self._snapshot_with_retry()
        if restored.fingerprint != target.fingerprint:
            raise RecorderError("The restored object snapshot still differs from the target state.")
        return restored

    def restore_snapshot(
        self,
        target: BoardSnapshot,
        description: str = "KiLog: restore replay position",
    ) -> BoardSnapshot:
        """Restore an in-memory snapshot as one undoable KiCad commit."""
        return self._restore_exactly(target, description)

    @staticmethod
    def _pointer_parts(path: str) -> list[str]:
        return [
            token.replace("~1", "/").replace("~0", "~")
            for token in path.split("/")[1:]
        ]

    @staticmethod
    def _set_pointer_value(document, parts: list[str], value, remove: bool = False) -> None:
        parent = document
        for token in parts[:-1]:
            parent = parent[int(token)] if isinstance(parent, list) else parent[token]
        token = parts[-1]
        if isinstance(parent, list):
            index = int(token)
            if remove:
                parent.pop(index)
            elif index == len(parent):
                parent.append(value)
            else:
                parent[index] = value
        elif remove:
            parent.pop(token, None)
        else:
            parent[token] = value

    @staticmethod
    def _replay_position(value) -> Vector2:
        if not isinstance(value, dict):
            raise ReplayError("footprint.move has no valid position.")
        normalized = dict(value)
        if "x_nm" not in normalized and "x" in normalized:
            normalized["x_nm"] = normalized.pop("x")
        if "y_nm" not in normalized and "y" in normalized:
            normalized["y_nm"] = normalized.pop("y")
        proto = common_types.Vector2()
        try:
            ParseDict(normalized, proto)
        except Exception as exc:
            raise ReplayError(f"Invalid footprint position: {value}") from exc
        return Vector2(proto)

    @staticmethod
    def _replay_orientation(value) -> Angle:
        if isinstance(value, (int, float)):
            return Angle.from_degrees(float(value))
        if not isinstance(value, dict):
            raise ReplayError("footprint.move has no valid orientation.")
        proto = common_types.Angle()
        try:
            ParseDict(value, proto)
        except Exception as exc:
            raise ReplayError(f"Invalid footprint orientation: {value}") from exc
        return Angle(proto)

    @classmethod
    def _apply_footprint_transform(cls, footprint, change: dict) -> None:
        """Use kipy setters so footprint children follow the anchor transform."""
        position = change.get("position")
        orientation = change.get("orientation")
        if position is not None:
            footprint.position = cls._replay_position(position)
        if orientation is not None:
            footprint.orientation = cls._replay_orientation(orientation)

    def apply_change(self, change: dict) -> BoardSnapshot:
        """Apply one persisted KiLog change to the live PCB."""
        item_uuid = change["item_uuid"]
        operation = change["operation"]
        current = self._snapshot_with_retry()
        state = current.items.get(item_uuid)

        if operation.endswith(".remove"):
            if state is None:
                raise ReplayError(f"PCB item {item_uuid} does not exist.")
            commit = self.board.begin_commit()
            try:
                self.board.remove_items_by_id([state.raw_item.id])
                self.board.push_commit(commit, f"KiLog replay: {operation}")
            except Exception:
                self.board.drop_commit(commit)
                raise
            return self._snapshot_with_retry()

        if state is None and operation.endswith(".add"):
            after = change.get("after")
            type_name = after.get("type") if isinstance(after, dict) else None
            data = after.get("data") if isinstance(after, dict) else None
            if not isinstance(type_name, str) or not isinstance(data, dict):
                raise ReplayError(f"{operation} does not contain a complete PCB item.")
            new_item = None
            for item_type in REPLAY_ITEM_TYPES:
                candidate = item_type()
                if candidate.proto.DESCRIPTOR.full_name == type_name:
                    new_item = candidate
                    break
            if new_item is None:
                raise ReplayError(f"Unsupported PCB item type: {type_name}")
            ParseDict(data, new_item.proto)
            commit = self.board.begin_commit()
            try:
                self.board.create_items([new_item])
                self.board.push_commit(commit, f"KiLog replay: {operation}")
            except Exception:
                self.board.drop_commit(commit)
                raise
            return self._snapshot_with_retry()

        if state is None:
            raise ReplayError(
                f"PCB item {item_uuid} required by {operation} was not found. "
                "Open the PCB referenced by the log and try again."
            )

        updated = self._clone_item(state.raw_item)
        if operation == "footprint.move":
            if not isinstance(updated, FootprintInstance):
                raise ReplayError(f"PCB item {item_uuid} is not a footprint.")
            self._apply_footprint_transform(updated, change)
        else:
            path = change.get("path")
            if not isinstance(path, str):
                raise ReplayError(f"{operation} has no replayable JSON path.")
            parts = self._pointer_parts(path)
            if len(parts) < 2 or parts[0] != "items" or parts[1] != item_uuid:
                raise ReplayError(f"Invalid change path: {path}")
            data = copy.deepcopy(state.log_value())
            relative = parts[2:]
            if not relative:
                after = change.get("after")
                if not isinstance(after, dict) or "data" not in after:
                    raise ReplayError(f"{operation} cannot create or replace {item_uuid}.")
                data = after
            else:
                if "after" in change:
                    self._set_pointer_value(data, relative, change["after"])
                elif "before" in change:
                    self._set_pointer_value(data, relative, None, remove=True)
                else:
                    raise ReplayError(f"{operation} has neither before nor after value.")
            ParseDict(data["data"], updated.proto)

        commit = self.board.begin_commit()
        try:
            self.board.update_items([updated])
            self.board.push_commit(commit, f"KiLog replay: {operation}")
        except Exception:
            self.board.drop_commit(commit)
            raise
        return self._snapshot_with_retry()
