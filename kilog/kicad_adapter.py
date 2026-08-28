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
    BoardCircle,
    BoardPolygon,
    BoardRectangle,
    BoardShape,
    BoardText,
    BoardTextBox,
    BoardLayer,
    Dimension,
    Footprint3DModel,
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
    FANOUT_VIA_EDGE_CLEARANCE_NM = 500_000
    FANOUT_SEARCH_STEP_NM = 500_000
    LOCAL_PAD_ZONE_MARGIN_NM = 250_000

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
            # Filled zone polygons are derived render data, not part of the zone's
            # editable definition.  A board refill can create thousands of polygon
            # nodes; serializing and diffing them made the final recording flush look
            # hung and produced enormous ``zone.refill`` steps.  Keep an unfilled
            # clone so the log records the zone outline/settings and KiCad can refill
            # it after replay.
            raw_item = self._clone_item(item)
            if isinstance(raw_item, Zone):
                raw_item.proto.ClearField("filled_polygons")

            proto = raw_item.proto
            item_uuid = proto.id.value
            if not item_uuid:
                continue
            data = MessageToDict(
                proto,
                preserving_proto_field_name=True,
                use_integers_for_enums=False,
                always_print_fields_with_no_presence=True,
            )
            if isinstance(raw_item, Zone):
                data.pop("filled_polygons", None)
            states[item_uuid] = ItemState(
                item_uuid=item_uuid,
                kind=self._kind(item),
                type_name=proto.DESCRIPTOR.full_name,
                data=data,
                raw_item=raw_item,
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

    @classmethod
    def _magnetic_keepout_loops(
        cls, snapshot: BoardSnapshot
    ) -> list[list[tuple[float, float]]]:
        """Build a body-shaped copper void for each L/T part, with a pad fallback."""
        keepouts: list[list[tuple[float, float]]] = []
        for state in snapshot.items.values():
            footprint = state.raw_item
            if not isinstance(footprint, FootprintInstance):
                continue
            reference = footprint.reference_field.text.value.strip().upper()
            if not reference.startswith(("L", "T")):
                continue

            body_bounds = cls._magnetic_body_bounds(footprint)
            if body_bounds is not None:
                left, top, right, bottom = body_bounds
                keepouts.append(
                    [(left, top), (right, top), (right, bottom), (left, bottom)]
                )
                continue

            # Footprints without a usable body outline fall back to the largest
            # pad-free rectangle crossing the footprint anchor.
            pad_bounds = [
                bounds
                for pad in footprint.definition.pads
                if (bounds := cls._pad_bounds_on_all_copper_layers(pad)) is not None
            ]
            if len(pad_bounds) < 2:
                continue

            center_x, center_y = footprint.position.x, footprint.position.y
            outer_left = min(bounds[0] for bounds in pad_bounds)
            outer_top = min(bounds[1] for bounds in pad_bounds)
            outer_right = max(bounds[2] for bounds in pad_bounds)
            outer_bottom = max(bounds[3] for bounds in pad_bounds)
            candidates: list[tuple[float, tuple[float, float, float, float]]] = []

            left_edges = [bounds[2] for bounds in pad_bounds if bounds[2] <= center_x]
            right_edges = [bounds[0] for bounds in pad_bounds if bounds[0] >= center_x]
            if left_edges and right_edges:
                left = max(left_edges)
                right = min(right_edges)
                if left < right:
                    candidates.append(
                        (
                            (right - left) * (outer_bottom - outer_top),
                            (left, outer_top, right, outer_bottom),
                        )
                    )

            top_edges = [bounds[3] for bounds in pad_bounds if bounds[3] <= center_y]
            bottom_edges = [bounds[1] for bounds in pad_bounds if bounds[1] >= center_y]
            if top_edges and bottom_edges:
                top = max(top_edges)
                bottom = min(bottom_edges)
                if top < bottom:
                    candidates.append(
                        (
                            (outer_right - outer_left) * (bottom - top),
                            (outer_left, top, outer_right, bottom),
                        )
                    )

            if not candidates:
                continue
            # Corner pad arrays can leave a gap on both axes.  The larger central
            # rectangle represents the magnetic body rather than a narrow channel
            # between pads in the same row or column.
            _, (left, top, right, bottom) = max(
                candidates, key=lambda value: value[0]
            )
            keepouts.append(
                [(left, top), (right, top), (right, bottom), (left, bottom)]
            )
        return keepouts

    @staticmethod
    def _closed_shape_bounds(
        shape: BoardShape,
    ) -> tuple[float, float, float, float] | None:
        """Return the axis-aligned bounds of a closed footprint graphic."""
        points: list[tuple[float, float]] = []
        if isinstance(shape, BoardCircle):
            radius = math.dist(
                (shape.center.x, shape.center.y),
                (shape.radius_point.x, shape.radius_point.y),
            )
            points = [
                (shape.center.x - radius, shape.center.y - radius),
                (shape.center.x + radius, shape.center.y + radius),
            ]
        elif isinstance(shape, BoardRectangle):
            points = [
                (shape.top_left.x, shape.top_left.y),
                (shape.bottom_right.x, shape.bottom_right.y),
            ]
        elif isinstance(shape, BoardPolygon):
            for polygon in shape.polygons:
                box = polygon.bounding_box()
                points.extend(
                    [
                        (box.pos.x, box.pos.y),
                        (box.pos.x + box.size.x, box.pos.y + box.size.y),
                    ]
                )
        if not points:
            return None

        half_stroke = max(0, shape.attributes.stroke.width) / 2
        xs, ys = zip(*points)
        return (
            min(xs) - half_stroke,
            min(ys) - half_stroke,
            max(xs) + half_stroke,
            max(ys) + half_stroke,
        )

    @classmethod
    def _magnetic_body_bounds(
        cls, footprint: FootprintInstance
    ) -> tuple[float, float, float, float] | None:
        """Prefer a closed F.SilkS body, then fall back to a closed F.Fab body."""
        center_x, center_y = footprint.position.x, footprint.position.y
        for layer in (BoardLayer.BL_F_SilkS, BoardLayer.BL_F_Fab):
            candidates = [
                bounds
                for shape in footprint.definition.shapes
                if shape.layer == layer
                if (bounds := cls._closed_shape_bounds(shape)) is not None
                if bounds[0] <= center_x <= bounds[2]
                and bounds[1] <= center_y <= bounds[3]
            ]
            if candidates:
                return max(
                    candidates,
                    key=lambda bounds: (bounds[2] - bounds[0])
                    * (bounds[3] - bounds[1]),
                )
        return None

    @staticmethod
    def _pad_bounds_on_all_copper_layers(
        pad,
    ) -> tuple[float, float, float, float] | None:
        """Return the union of a pad's copper shapes on every board layer."""
        angle = math.radians(pad.padstack.angle.degrees)
        cosine = math.cos(angle)
        sine = math.sin(angle)
        bounds = []
        for copper in pad.padstack.copper_layers:
            if copper.size.x <= 0 or copper.size.y <= 0:
                continue
            center_x = pad.position.x + cosine * copper.offset.x - sine * copper.offset.y
            center_y = pad.position.y + sine * copper.offset.x + cosine * copper.offset.y
            half_x = abs(cosine) * copper.size.x / 2 + abs(sine) * copper.size.y / 2
            half_y = abs(sine) * copper.size.x / 2 + abs(cosine) * copper.size.y / 2
            bounds.append(
                (
                    center_x - half_x,
                    center_y - half_y,
                    center_x + half_x,
                    center_y + half_y,
                )
            )
        if not bounds:
            return None
        return (
            min(value[0] for value in bounds),
            min(value[1] for value in bounds),
            max(value[2] for value in bounds),
            max(value[3] for value in bounds),
        )

    @staticmethod
    def _pad_bounds_on_layer(pad, layer) -> tuple[float, float, float, float] | None:
        """Return the axis-aligned copper bounds of a pad on one board layer."""
        copper = pad.padstack.copper_layer(layer)
        if copper is None or copper.size.x <= 0 or copper.size.y <= 0:
            return None
        angle = math.radians(pad.padstack.angle.degrees)
        cosine = math.cos(angle)
        sine = math.sin(angle)
        center_x = pad.position.x + cosine * copper.offset.x - sine * copper.offset.y
        center_y = pad.position.y + sine * copper.offset.x + cosine * copper.offset.y
        half_x = (
            abs(cosine) * copper.size.x / 2 + abs(sine) * copper.size.y / 2
        )
        half_y = (
            abs(sine) * copper.size.x / 2 + abs(cosine) * copper.size.y / 2
        )
        return (
            center_x - half_x,
            center_y - half_y,
            center_x + half_x,
            center_y + half_y,
        )

    @classmethod
    def _shared_pad_zones(
        cls,
        snapshot: BoardSnapshot,
        layers,
        fill_net,
        nets_by_name,
        board_loops,
    ) -> list[Zone]:
        """Create one local zone for each same-net multi-pad group in a footprint."""
        zones: list[Zone] = []
        fill_name = fill_net.name.casefold()
        for state in snapshot.items.values():
            footprint = state.raw_item
            if not isinstance(footprint, FootprintInstance):
                continue
            if not point_inside_board(
                (footprint.position.x, footprint.position.y), board_loops
            ):
                continue

            reference = footprint.reference_field.text.value.strip().upper()
            # Preserve the central copper void requested for magnetic components.
            if reference.startswith(("L", "T")):
                continue

            for layer in layers:
                grouped: dict[str, list[tuple[float, float, float, float]]] = {}
                for pad in footprint.definition.pads:
                    net_name = pad.net.name.strip()
                    if not net_name or net_name.casefold() == fill_name:
                        continue
                    bounds = cls._pad_bounds_on_layer(pad, layer)
                    if bounds is not None:
                        grouped.setdefault(net_name.casefold(), []).append(bounds)

                for net_key, pad_bounds in grouped.items():
                    if len(pad_bounds) < 2:
                        continue
                    net = nets_by_name.get(net_key)
                    if net is None:
                        continue
                    margin = cls.LOCAL_PAD_ZONE_MARGIN_NM
                    left = min(value[0] for value in pad_bounds) - margin
                    top = min(value[1] for value in pad_bounds) - margin
                    right = max(value[2] for value in pad_bounds) + margin
                    bottom = max(value[3] for value in pad_bounds) + margin
                    zone = Zone()
                    zone.net = net
                    zone.layers = [layer]
                    zone.priority = 1
                    zone.name = f"KiLog {reference or 'footprint'} {net.name} pad group"
                    zone.outline = cls._zone_outline(
                        [[(left, top), (right, top), (right, bottom), (left, bottom)]]
                    )
                    zones.append(zone)
        return zones

    @staticmethod
    def _same_closed_loop(
        left: list[tuple[float, float]],
        right: list[tuple[float, float]],
        tolerance: float = 1.0,
    ) -> bool:
        """Compare closed loops independent of their start node and direction."""
        if len(left) != len(right) or not left:
            return False

        def close(a, b) -> bool:
            return abs(a[0] - b[0]) <= tolerance and abs(a[1] - b[1]) <= tolerance

        for start in range(len(right)):
            if not close(left[0], right[start]):
                continue
            if all(close(left[index], right[(start + index) % len(right)]) for index in range(len(left))):
                return True
            if all(close(left[index], right[(start - index) % len(right)]) for index in range(len(left))):
                return True
        return False

    @classmethod
    def _zones_replaced_by_fill(
        cls,
        snapshot: BoardSnapshot,
        layers,
        fill_net,
        board_outer_loop,
    ) -> list:
        """Find current and legacy KiLog zones that a new Fill supersedes."""
        selected_layers = set(layers)
        fill_name = fill_net.name.casefold()
        remove_ids = []
        for state in snapshot.items.values():
            zone = state.raw_item
            if not isinstance(zone, Zone) or not selected_layers.intersection(zone.layers):
                continue
            if zone.name.startswith("KiLog "):
                remove_ids.append(zone.id)
                continue
            if zone.net is None or zone.net.name.casefold() != fill_name:
                continue
            try:
                outline = [
                    (node.point.x, node.point.y)
                    for node in zone.outline.outline.nodes
                    if node.has_point
                ]
            except (IndexError, ValueError):
                continue
            # Older KiLog full-board zones did not have a name.  Match their
            # defining outer loop so user-created partial zones remain untouched.
            if cls._same_closed_loop(outline, board_outer_loop):
                remove_ids.append(zone.id)
        return remove_ids

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

        snapshot = self.snapshot()
        loops = ordered_board_loops(edge_segments(snapshot))
        if not loops:
            raise RecorderError("Edge.Cuts does not contain a closed board outline.")
        zone_loops = [*loops, *self._magnetic_keepout_loops(snapshot)]

        zones = []
        for layer in layers:
            zone = Zone()
            zone.net = net
            zone.layers = [layer]
            zone.name = f"KiLog full-board {net.name} {BoardLayer.Name(layer)}"
            zone.outline = self._zone_outline(zone_loops)
            zones.append(zone)
        zones.extend(
            self._shared_pad_zones(
                snapshot,
                layers,
                net,
                {value.name.casefold(): value for value in nets},
                loops,
            )
        )
        remove_ids = self._zones_replaced_by_fill(
            snapshot, layers, net, loops[0]
        )

        commit = self.board.begin_commit()
        try:
            if remove_ids:
                self.board.remove_items_by_id(remove_ids)
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
                    layer,
                    loops,
                    pad_obstacles,
                    via_obstacles,
                    existing_tracks,
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
        layer,
        loops: list[list[tuple[float, float]]],
        pad_obstacles: list[tuple[float, float, float, object]],
        via_obstacles: list[tuple[float, float, float]],
        existing_tracks: list[Track | ArcTrack],
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
                # Keep the entire via copper, not merely its center, 0.5 mm
                # away from both the outer board edge and internal cut-outs.
                via_radius + self.FANOUT_VIA_EDGE_CLEARANCE_NM,
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
            if self._fanout_hits_other_net_track(
                start,
                candidate,
                layer,
                pad.net.name,
                track_width_nm,
                via_radius,
                existing_tracks,
            ):
                continue
            return Vector2.from_xy(*candidate)
        return None

    @staticmethod
    def _segment_distance(start, end, obstacle_start, obstacle_end) -> float:
        """Return the shortest distance between two closed line segments."""
        def orientation(left, middle, right):
            return (middle[0] - left[0]) * (right[1] - left[1]) - (
                middle[1] - left[1]
            ) * (right[0] - left[0])

        def on_segment(point, left, right):
            return (
                min(left[0], right[0]) <= point[0] <= max(left[0], right[0])
                and min(left[1], right[1]) <= point[1] <= max(left[1], right[1])
            )

        first = orientation(start, end, obstacle_start)
        second = orientation(start, end, obstacle_end)
        third = orientation(obstacle_start, obstacle_end, start)
        fourth = orientation(obstacle_start, obstacle_end, end)
        def opposite_signs(left, right):
            return (left > 0 and right < 0) or (left < 0 and right > 0)

        intersects = (
            (first == 0 and on_segment(obstacle_start, start, end))
            or (second == 0 and on_segment(obstacle_end, start, end))
            or (third == 0 and on_segment(start, obstacle_start, obstacle_end))
            or (fourth == 0 and on_segment(end, obstacle_start, obstacle_end))
            or (opposite_signs(first, second) and opposite_signs(third, fourth))
        )
        if intersects:
            return 0.0
        return min(
            point_segment_distance(start, obstacle_start, obstacle_end),
            point_segment_distance(end, obstacle_start, obstacle_end),
            point_segment_distance(obstacle_start, start, end),
            point_segment_distance(obstacle_end, start, end),
        )

    @staticmethod
    def _track_segments(track: Track | ArcTrack):
        """Yield straight segments approximating a track, plus arc sagitta."""
        if isinstance(track, Track):
            yield (
                (track.start.x, track.start.y),
                (track.end.x, track.end.y),
                0.0,
            )
            return

        center = track.center()
        arc_angle = track.angle()
        if center is None or arc_angle is None or track.radius() == 0:
            yield (
                (track.start.x, track.start.y),
                (track.end.x, track.end.y),
                0.0,
            )
            return

        start_angle = math.atan2(track.start.y - center.y, track.start.x - center.x)
        mid_angle = math.atan2(track.mid.y - center.y, track.mid.x - center.x)
        ccw_to_mid = (mid_angle - start_angle) % (2 * math.pi)
        ccw = ccw_to_mid <= arc_angle + 1e-12
        signed_angle = arc_angle if ccw else -arc_angle
        segment_count = max(1, math.ceil(arc_angle / math.radians(5)))
        step = signed_angle / segment_count
        radius = track.radius()
        sagitta = radius * (1 - math.cos(abs(step) / 2))
        points = [
            (
                center.x + radius * math.cos(start_angle + step * index),
                center.y + radius * math.sin(start_angle + step * index),
            )
            for index in range(segment_count + 1)
        ]
        for segment_start, segment_end in zip(points, points[1:]):
            yield segment_start, segment_end, sagitta

    @classmethod
    def _fanout_hits_other_net_track(
        cls,
        start,
        candidate,
        layer,
        net_name: str,
        track_width_nm: int,
        via_radius: float,
        existing_tracks: list[Track | ArcTrack],
    ) -> bool:
        """Reject fanout copper that would touch a trace belonging to another net."""
        requested_net = net_name.casefold()
        for obstacle in existing_tracks:
            if obstacle.net.name.casefold() == requested_net:
                continue
            for obstacle_start, obstacle_end, approximation_margin in cls._track_segments(
                obstacle
            ):
                clearance = cls.FANOUT_PAD_CLEARANCE_NM + approximation_margin
                obstacle_radius = obstacle.width / 2
                # The through via intersects every copper layer.
                if point_segment_distance(candidate, obstacle_start, obstacle_end) < (
                    via_radius + obstacle_radius + clearance
                ):
                    return True
                # The fanout trace only conflicts with copper on its own layer.
                if obstacle.layer == layer and cls._segment_distance(
                    start, candidate, obstacle_start, obstacle_end
                ) < (track_width_nm / 2 + obstacle_radius + clearance):
                    return True
        return False

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
            # kicad-python 0.7.1 rebuilds ``definition.items`` in the
            # FootprintInstance orientation setter but omits 3D models from
            # the rebuilt list.  Preserve them across rotation so replaying a
            # footprint transform does not sever its 3D model associations.
            models = [
                item
                for item in footprint.definition.items
                if isinstance(item, Footprint3DModel)
            ]
            footprint.orientation = cls._replay_orientation(orientation)
            for model in models:
                footprint.definition.add_item(model)

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
        refill_zones = any(
            change.get("operation") == "zone.refill"
            and not (
                str(change.get("path", "")).endswith("/filled")
                and change.get("value") is False
            )
            for change in changes
        )
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
            if refill_zones:
                # ``filled`` records the user's intent, while filled_polygons is
                # deliberately omitted from logs because it is large derived data.
                # Rebuild that data in KiCad even when the flag already matched.
                self.board.refill_zones()
                return self._snapshot_with_retry()
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
        if refill_zones:
            # Updating Zone.filled alone does not calculate any copper polygons.
            # The PCB Editor's refill command must run after the zone definitions
            # have been committed to its live board model.
            self.board.refill_zones()
        return self._snapshot_with_retry()
