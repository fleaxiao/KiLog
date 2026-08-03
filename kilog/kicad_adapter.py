from __future__ import annotations

import copy
from pathlib import Path
import time

from google.protobuf.json_format import MessageToDict
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
from kipy.proto.common.commands.editor_commands_pb2 import RAS_OK
from kipy.proto.common.types import KiCadObjectType

from .model import BoardSnapshot, ItemState
from .recorder import RecorderError


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


class KiCadBoardAdapter:
    """Adapter over the official KiCad 9/10 IPC API.

    All snapshots are collected from the live editor model, so the board does not need to be
    saved before an operation is visible to KiLog.
    """

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
                raw_item=copy.deepcopy(item),
            )
        return BoardSnapshot.create(self.board.name or "<untitled>", states)

    def save_copy(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.board.save_as(str(path), overwrite=False, include_project=False)

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

    def _restore_exactly(self, target: BoardSnapshot) -> BoardSnapshot:
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

        commit = self.board.begin_commit()
        try:
            if remove_ids:
                self.board.remove_items_by_id(remove_ids)
            if create_items:
                self.board.create_items(create_items)
            if update_items:
                self.board.update_items(update_items)
            self.board.push_commit(commit, "KiLog: undo recorded operation")
        except Exception:
            self.board.drop_commit(commit)
            raise

        restored = self._snapshot_with_retry()
        if restored.fingerprint != target.fingerprint:
            raise RecorderError("The restored object snapshot still differs from the target state.")
        return restored
