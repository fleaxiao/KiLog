from __future__ import annotations

import copy
import math
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
    BoardLayer,
    Dimension,
    FootprintInstance,
    PadType,
    Track,
    Via,
    Zone,
)
from kipy.kicad import KiCad
from kipy.geometry import Angle, PolygonWithHoles, PolyLine, PolyLineNode, Vector2
from kipy.proto.common.commands.editor_commands_pb2 import RAS_OK
from kipy.proto.common import types as common_types
from kipy.proto.common.types import KiCadObjectType

from .board_outline import (
    circle_inside_board,
    ordered_board_loops,
    point_inside_board,
    point_segment_distance,
)
from .diffing import edge_segments
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
    FANOUT_LENGTH_NM = 1_000_000
    FANOUT_DEFAULT_TRACK_WIDTH_MM = 0.5
    FANOUT_VIA_DIAMETER_NM = 600_000
    FANOUT_VIA_DRILL_NM = 300_000
    FANOUT_PAD_CLEARANCE_NM = 200_000
    FANOUT_EDGE_CLEARANCE_NM = 200_000
    FANOUT_SEARCH_STEP_NM = 500_000

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

    def prepare_recording(self) -> BoardSnapshot:
        """Restore the PCB file on disk before capturing the recording baseline."""
        board_path = self.board_path
        if board_path is None:
            raise RecorderError("Save the PCB file before starting a recording.")
        try:
            self.board.revert()
        except Exception as exc:
            raise RecorderError(
                f"Could not restore the initial PCB state from {board_path.name}: {exc}"
            ) from exc
        time.sleep(self.REVERT_SETTLE_SECONDS)
        return self._snapshot_with_retry()

    @staticmethod
    def _zone_outline(loops: list[list[tuple[float, float]]]) -> PolygonWithHoles:
        polygon = PolygonWithHoles()
        outer = PolyLine()
        for x, y in loops[0]:
            outer.append(PolyLineNode.from_xy(round(x), round(y)))
        polygon.outline = outer
        for loop in loops[1:]:
            hole = PolyLine()
            for x, y in loop:
                hole.append(PolyLineNode.from_xy(round(x), round(y)))
            polygon.add_hole(hole)
        return polygon

    def fill_board_copper(self, net_name: str, layer_names: tuple[str, ...]) -> int:
        """Create one unfilled full-board copper zone on each requested layer."""
        requested_net = net_name.strip()
        if not requested_net:
            raise RecorderError("Enter a network name for the copper fill.")
        if not layer_names:
            raise RecorderError("Select at least one copper layer.")

        nets = list(self.board.get_nets())
        net = next((value for value in nets if value.name == requested_net), None)
        if net is None:
            net = next(
                (value for value in nets if value.name.casefold() == requested_net.casefold()),
                None,
            )
        if net is None:
            raise RecorderError(f"Network {requested_net!r} does not exist on this board.")

        layer_map = {
            "F.Cu": BoardLayer.BL_F_Cu,
            "B.Cu": BoardLayer.BL_B_Cu,
        }
        try:
            layers = [layer_map[name] for name in layer_names]
        except KeyError as exc:
            raise RecorderError(f"Unsupported copper layer: {exc.args[0]}") from exc

        loops = ordered_board_loops(edge_segments(self.snapshot()))
        if not loops:
            raise RecorderError("Edge.Cuts does not contain a closed board outline.")

        zones = []
        for layer in layers:
            zone = Zone()
            zone.net = net
            zone.layers = [layer]
            zone.outline = self._zone_outline(loops)
            zones.append(zone)

        commit = self.board.begin_commit()
        try:
            self.board.create_items(zones)
            self.board.push_commit(commit, f"KiLog: create board zones for {net.name}")
        except Exception:
            self.board.drop_commit(commit)
            raise
        return len(zones)

    def fanout_net(
        self,
        net_name: str,
        default_width_mm: float | str = FANOUT_DEFAULT_TRACK_WIDTH_MM,
    ) -> int:
        """Fan out on-board SMD pads while keeping vias clear of pads and edges."""
        requested_net = net_name.strip()
        if not requested_net:
            raise RecorderError("Enter a network name for fanout.")

        nets = list(self.board.get_nets())
        net = next(
            (value for value in nets if value.name.casefold() == requested_net.casefold()),
            None,
        )
        if net is None:
            raise RecorderError(f"Network {requested_net!r} does not exist on this board.")
        try:
            default_width_nm = round(float(default_width_mm) * 1_000_000)
        except (TypeError, ValueError) as exc:
            raise RecorderError("Fanout Width must be a number in millimetres.") from exc
        if default_width_nm <= 0:
            raise RecorderError("Fanout Width must be greater than zero.")

        snapshot = self.snapshot()
        items = [state.raw_item for state in snapshot.items.values()]
        loops = ordered_board_loops(edge_segments(snapshot))
        if not loops:
            raise RecorderError("Edge.Cuts does not contain a closed board outline.")
        footprints = [
            item
            for item in items
            if isinstance(item, FootprintInstance)
            and point_inside_board((item.position.x, item.position.y), loops)
        ]
        pad_obstacles = [
            (pad.position.x, pad.position.y, self._pad_radius(pad), pad)
            for footprint in footprints
            for pad in footprint.definition.pads
        ]
        via_obstacles = [
            (item.position.x, item.position.y, self._via_radius(item))
            for item in items
            if isinstance(item, Via)
        ]
        existing_tracks = [item for item in items if isinstance(item, (Track, ArcTrack))]
        board_x = [point[0] for loop in loops for point in loop]
        board_y = [point[1] for loop in loops for point in loop]
        max_search = math.hypot(max(board_x) - min(board_x), max(board_y) - min(board_y))
        created = []
        fanout_count = 0
        matching_pad_count = 0
        already_fanned_count = 0

        for footprint in footprints:
            layer = (
                BoardLayer.BL_B_Cu
                if footprint.layer == BoardLayer.BL_B_Cu
                else BoardLayer.BL_F_Cu
            )
            for pad in footprint.definition.pads:
                if pad.pad_type != PadType.PT_SMD or pad.net.name.casefold() != net.name.casefold():
                    continue
                matching_pad_count += 1
                if self._pad_is_fanned_out(pad, existing_tracks, items):
                    already_fanned_count += 1
                    continue

                track_width = self._fanout_track_width(
                    pad,
                    existing_tracks,
                    default_width_nm,
                )
                via_position = self._find_fanout_position(
                    pad,
                    footprint,
                    loops,
                    pad_obstacles,
                    via_obstacles,
                    max_search,
                    track_width,
                )
                if via_position is None:
                    continue

                track = Track()
                track.net = net
                track.layer = layer
                track.start = pad.position
                track.end = via_position
                track.width = track_width

                via = Via()
                via.net = net
                via.position = via_position
                via.diameter = self.FANOUT_VIA_DIAMETER_NM
                via.drill_diameter = self.FANOUT_VIA_DRILL_NM

                created.extend((track, via))
                via_obstacles.append(
                    (via_position.x, via_position.y, self.FANOUT_VIA_DIAMETER_NM / 2)
                )
                fanout_count += 1

        if not created and matching_pad_count and already_fanned_count == matching_pad_count:
            return 0
        if not created:
            raise RecorderError(f"No unfanned SMD pads found on network {net.name!r}.")

        commit = self.board.begin_commit()
        try:
            self.board.create_items(created)
            self.board.push_commit(commit, f"KiLog: fanout {net.name}")
        except Exception:
            self.board.drop_commit(commit)
            raise
        return fanout_count

    @staticmethod
    def _pad_radius(pad) -> float:
        """Return a conservative circular bound for every copper shape in a pad."""
        radius = 0.0
        for copper_layer in pad.padstack.copper_layers:
            shape_radius = math.hypot(copper_layer.size.x, copper_layer.size.y) / 2
            offset = math.hypot(copper_layer.offset.x, copper_layer.offset.y)
            radius = max(radius, shape_radius + offset)
        drill = pad.padstack.drill.diameter
        return max(radius, math.hypot(drill.x, drill.y) / 2)

    @staticmethod
    def _pad_extent_in_direction(pad, direction_x: int, direction_y: int) -> float:
        """Return pad copper extent from its anchor along one cardinal direction."""
        angle = math.radians(pad.padstack.angle.degrees)
        cosine = math.cos(angle)
        sine = math.sin(angle)
        extent = 0.0
        for copper_layer in pad.padstack.copper_layers:
            half_width = copper_layer.size.x / 2
            half_height = copper_layer.size.y / 2
            rotated_half_x = abs(cosine) * half_width + abs(sine) * half_height
            rotated_half_y = abs(sine) * half_width + abs(cosine) * half_height
            rotated_offset_x = (
                cosine * copper_layer.offset.x - sine * copper_layer.offset.y
            )
            rotated_offset_y = (
                sine * copper_layer.offset.x + cosine * copper_layer.offset.y
            )
            directional_offset = (
                direction_x * rotated_offset_x + direction_y * rotated_offset_y
            )
            directional_half_size = (
                abs(direction_x) * rotated_half_x + abs(direction_y) * rotated_half_y
            )
            extent = max(extent, directional_offset + directional_half_size)
        drill = pad.padstack.drill.diameter
        drill_extent = (
            abs(direction_x) * drill.x / 2 + abs(direction_y) * drill.y / 2
        )
        return max(extent, drill_extent)

    @classmethod
    def _via_radius(cls, via: Via) -> float:
        try:
            return via.diameter / 2
        except ValueError:
            return cls.FANOUT_VIA_DIAMETER_NM / 2

    @classmethod
    def _fanout_track_width(cls, pad, tracks, default_width_nm: int) -> int:
        """Use the width of the closest same-net trace endpoint connected to a pad."""
        pad_radius = cls._pad_radius(pad)
        connected = []
        for track in tracks:
            if track.net.name.casefold() != pad.net.name.casefold():
                continue
            endpoint_distance = min(
                math.hypot(track.start.x - pad.position.x, track.start.y - pad.position.y),
                math.hypot(track.end.x - pad.position.x, track.end.y - pad.position.y),
            )
            if endpoint_distance <= max(1.0, pad_radius):
                connected.append((endpoint_distance, -track.width, track.width))
        return min(connected)[2] if connected else default_width_nm

    @classmethod
    def _pad_is_fanned_out(cls, pad, tracks, items) -> bool:
        """Return whether a pad already reaches a same-net via directly or by trace."""
        pad_net = pad.net.name.casefold()
        pad_radius = cls._pad_radius(pad)
        vias = [
            item
            for item in items
            if isinstance(item, Via) and item.net.name.casefold() == pad_net
        ]
        if any(
            math.hypot(via.position.x - pad.position.x, via.position.y - pad.position.y)
            <= pad_radius + cls._via_radius(via)
            for via in vias
        ):
            return True

        for track in tracks:
            if track.net.name.casefold() != pad_net:
                continue
            endpoints = (track.start, track.end)
            for pad_endpoint, via_endpoint in (endpoints, endpoints[::-1]):
                if math.hypot(
                    pad_endpoint.x - pad.position.x,
                    pad_endpoint.y - pad.position.y,
                ) > max(1.0, pad_radius):
                    continue
                if any(
                    math.hypot(
                        via.position.x - via_endpoint.x,
                        via.position.y - via_endpoint.y,
                    )
                    <= cls._via_radius(via) + track.width / 2
                    for via in vias
                ):
                    return True
        return False

    def _find_fanout_position(
        self,
        pad,
        footprint: FootprintInstance,
        loops: list[list[tuple[float, float]]],
        pad_obstacles: list[tuple[float, float, float, object]],
        via_obstacles: list[tuple[float, float, float]],
        max_search: float,
        track_width_nm: int,
    ) -> Vector2 | None:
        via_radius = self.FANOUT_VIA_DIAMETER_NM / 2
        radial_x = pad.position.x - footprint.position.x
        radial_y = pad.position.y - footprint.position.y
        cardinal_directions = ((1, 0), (0, 1), (-1, 0), (0, -1))
        directions = sorted(
            cardinal_directions,
            key=lambda direction: radial_x * direction[0] + radial_y * direction[1],
            reverse=True,
        )

        candidates = []
        for preference, (direction_x, direction_y) in enumerate(directions):
            distance = max(
                self.FANOUT_LENGTH_NM,
                self._pad_extent_in_direction(pad, direction_x, direction_y)
                + via_radius
                + self.FANOUT_PAD_CLEARANCE_NM,
            )
            while distance <= max_search:
                candidates.append((distance, preference, direction_x, direction_y))
                distance += self.FANOUT_SEARCH_STEP_NM

        for distance, _preference, direction_x, direction_y in sorted(candidates):
            candidate = (
                round(pad.position.x + distance * direction_x),
                round(pad.position.y + distance * direction_y),
            )
            if not circle_inside_board(
                candidate,
                via_radius + self.FANOUT_EDGE_CLEARANCE_NM,
                loops,
            ):
                continue
            if any(
                obstacle_pad is not pad
                and math.hypot(candidate[0] - x, candidate[1] - y)
                < via_radius + radius + self.FANOUT_PAD_CLEARANCE_NM
                for x, y, radius, obstacle_pad in pad_obstacles
            ):
                continue
            if any(
                math.hypot(candidate[0] - x, candidate[1] - y)
                < via_radius + radius + self.FANOUT_PAD_CLEARANCE_NM
                for x, y, radius in via_obstacles
            ):
                continue
            start = (pad.position.x, pad.position.y)
            if any(
                obstacle[3] is not pad
                and point_segment_distance((obstacle[0], obstacle[1]), start, candidate)
                < obstacle[2]
                + track_width_nm / 2
                + self.FANOUT_PAD_CLEARANCE_NM
                for obstacle in pad_obstacles
            ):
                continue
            return Vector2.from_xy(*candidate)
        return None

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

    def _state_for_replay_item(self, item) -> ItemState:
        proto = item.proto
        data = MessageToDict(
            proto,
            preserving_proto_field_name=True,
            use_integers_for_enums=False,
            always_print_fields_with_no_presence=True,
        )
        return ItemState(
            item_uuid=proto.id.value,
            kind=self._kind(item),
            type_name=proto.DESCRIPTOR.full_name,
            data=data,
            raw_item=item,
        )

    def _apply_change_to_states(
        self,
        states: dict[str, ItemState],
        change: dict,
    ) -> None:
        """Apply a change to an in-memory step state without touching KiCad."""
        item_uuid = change["item_uuid"]
        operation = change["operation"]
        state = states.get(item_uuid)

        if operation.endswith(".remove"):
            if state is None:
                raise ReplayError(f"PCB item {item_uuid} does not exist.")
            del states[item_uuid]
            return

        if state is None and operation.endswith(".add"):
            item = change.get("item")
            type_name = item.get("type") if isinstance(item, dict) else None
            data = item.get("data") if isinstance(item, dict) else None
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
            if new_item.proto.id.value != item_uuid:
                raise ReplayError(f"{operation} item UUID does not match {item_uuid}.")
            states[item_uuid] = self._state_for_replay_item(new_item)
            return

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
            item = change.get("item")
            if item is not None:
                type_name = item.get("type") if isinstance(item, dict) else None
                item_data = item.get("data") if isinstance(item, dict) else None
                if type_name != state.type_name or not isinstance(item_data, dict):
                    raise ReplayError(f"{operation} does not contain a compatible PCB item.")
                ParseDict(item_data, updated.proto)
                if updated.proto.id.value != item_uuid:
                    raise ReplayError(f"{operation} item UUID does not match {item_uuid}.")
                states[item_uuid] = self._state_for_replay_item(updated)
                return

            path = change.get("path")
            if not isinstance(path, str):
                raise ReplayError(f"{operation} has no replayable JSON path.")
            parts = self._pointer_parts(path)
            if len(parts) < 2 or parts[0] != "items" or parts[1] != item_uuid:
                raise ReplayError(f"Invalid change path: {path}")
            data = copy.deepcopy(state.log_value())
            relative = parts[2:]
            if not relative:
                raise ReplayError(f"{operation} has no item target for {item_uuid}.")
            if "value" in change:
                self._set_pointer_value(data, relative, change["value"])
            elif change.get("delete") is True:
                self._set_pointer_value(data, relative, None, remove=True)
            else:
                raise ReplayError(f"{operation} has neither a value nor a delete marker.")
            ParseDict(data["data"], updated.proto)

        states[item_uuid] = self._state_for_replay_item(updated)

    def apply_step(
        self,
        changes: tuple[dict, ...] | list[dict],
        description: str = "KiLog replay step",
    ) -> BoardSnapshot:
        """Apply all changes in one replay step as one undoable KiCad commit."""
        current = self._snapshot_with_retry()
        states = dict(current.items)
        for change in changes:
            self._apply_change_to_states(states, change)

        current_ids = set(current.items)
        target_ids = set(states)
        remove_ids = [current.items[item_id].raw_item.id for item_id in current_ids - target_ids]
        create_items = [states[item_id].raw_item for item_id in target_ids - current_ids]
        update_items = [
            states[item_id].raw_item
            for item_id in current_ids & target_ids
            if current.items[item_id].log_value() != states[item_id].log_value()
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
        return self._snapshot_with_retry()
