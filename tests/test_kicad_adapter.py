from __future__ import annotations

import math

import pytest
from kipy.board_types import (
    ArcTrack,
    BoardLayer,
    BoardRectangle,
    BoardSegment,
    Footprint3DModel,
    FootprintInstance,
    Net,
    Pad,
    PadType,
    Track,
    Via,
    Zone,
    ZoneConnectionStyle,
)
from kipy.geometry import Vector2

from kilog.kicad_adapter import KiCadBoardAdapter
from kilog.recorder import RecorderError


class FakeBoard:
    name = "C:/project/demo.kicad_pcb"

    def __init__(self, items):
        self.items = items
        self.calls = 0
        self.reverted = False

    def get_items(self, types):
        self.calls += 1
        return self.items

    def revert(self):
        self.reverted = True


class FillBoard(FakeBoard):
    def __init__(self, items):
        super().__init__(items)
        self.created = []
        self.commit_message = ""
        self.commit_count = 0
        self.create_calls = 0
        self.refilled = False
        self.removed = []

    def get_nets(self):
        return [Net(name="GND"), Net(name="VCC")]

    def begin_commit(self):
        return object()

    def create_items(self, items):
        self.create_calls += 1
        self.created.extend(items)
        self.items.extend(items)
        return items

    def remove_items_by_id(self, item_ids):
        removed_ids = {item_id.value for item_id in item_ids}
        self.removed.extend(removed_ids)
        self.items[:] = [item for item in self.items if item.proto.id.value not in removed_ids]

    def update_items(self, items):
        updates = {item.proto.id.value: item for item in items}
        self.items[:] = [updates.get(item.proto.id.value, item) for item in self.items]
        return items

    def push_commit(self, _commit, message):
        self.commit_count += 1
        self.commit_message = message

    def drop_commit(self, _commit):
        raise AssertionError("valid copper fill should not drop its commit")

    def refill_zones(self):
        self.refilled = True


class ProjectSpecifier:
    def __init__(self, path, name=""):
        self.path = path
        self.name = name


class BoardDocument:
    def __init__(self, project_path, project_name=""):
        self.project = ProjectSpecifier(project_path, project_name)


def with_id(value, item_uuid):
    value.proto.id.value = item_uuid
    return value


def edge_segment(item_uuid, start, end):
    segment = with_id(BoardSegment(), item_uuid)
    segment.start = Vector2.from_xy(*start)
    segment.end = Vector2.from_xy(*end)
    segment.layer = BoardLayer.BL_Edge_Cuts
    return segment


def test_snapshot_uses_one_api_request_and_classifies_common_items():
    board = FakeBoard(
        [
            with_id(FootprintInstance(), "fp-1"),
            with_id(Track(), "track-1"),
            with_id(Via(), "via-1"),
            with_id(Zone(), "zone-1"),
        ]
    )
    adapter = KiCadBoardAdapter(object(), board)

    state = adapter.snapshot()

    assert board.calls == 1
    assert {key: value.kind for key, value in state.items.items()} == {
        "fp-1": "footprint",
        "track-1": "track",
        "via-1": "via",
        "zone-1": "zone",
    }
    assert adapter.output_directory.as_posix().lower().endswith("/project")


def test_snapshot_records_zone_definition_without_derived_filled_polygons():
    zone = with_id(Zone(), "zone-1")
    zone.proto.filled_polygons.add()
    board = FakeBoard([zone])

    state = KiCadBoardAdapter(object(), board).snapshot().items["zone-1"]

    assert "filled_polygons" not in state.data
    assert len(state.raw_item.proto.filled_polygons) == 0
    # Snapshot normalization must not alter the live KiCad object.
    assert len(zone.proto.filled_polygons) == 1


def test_relative_board_filename_uses_current_kicad_project_directory(tmp_path):
    board = FakeBoard([])
    board.name = "demo.kicad_pcb"
    board.document = BoardDocument(str(tmp_path))
    adapter = KiCadBoardAdapter(object(), board)

    assert adapter.output_directory == tmp_path.resolve()


def test_empty_board_filename_uses_project_path_returned_by_kicad(tmp_path):
    board = FakeBoard([])
    board.name = ""
    board.document = BoardDocument(str(tmp_path))
    adapter = KiCadBoardAdapter(object(), board)

    assert adapter.output_directory == tmp_path.resolve()


def test_board_path_is_reconstructed_from_kicad_project_data(tmp_path):
    board = FakeBoard([])
    board.name = ""
    board.document = BoardDocument(str(tmp_path), "controller")
    adapter = KiCadBoardAdapter(object(), board)

    assert adapter.board_path == (tmp_path / "controller.kicad_pcb").resolve()


def test_replay_footprint_transform_moves_anchor_and_child_fields():
    footprint = FootprintInstance()
    footprint.position = Vector2.from_xy(1_000_000, 2_000_000)
    # Exercise the same clone path used by live snapshots and apply_change.
    footprint = KiCadBoardAdapter._clone_item(footprint)
    old_reference = footprint.reference_field.text.position

    KiCadBoardAdapter._apply_footprint_transform(
        footprint,
        {
            "position": {"x_nm": "6000000", "y_nm": "9000000"},
            "orientation": {"value_degrees": 90},
        },
    )

    assert (footprint.position.x, footprint.position.y) == (6_000_000, 9_000_000)
    assert footprint.orientation.degrees == 90
    # This is the behavior direct ParseDict misses: kipy moves footprint children too.
    assert footprint.reference_field.text.position != old_reference


def test_replay_independent_footprint_reference_move_preserves_anchor():
    footprint = with_id(FootprintInstance(), "fp-1")
    footprint.position = Vector2.from_xy(1_000_000, 2_000_000)
    footprint.reference_field.text.position = Vector2.from_xy(1_100_000, 2_100_000)
    adapter = KiCadBoardAdapter(object(), FakeBoard([footprint]))
    states = {"fp-1": adapter._state_for_replay_item(adapter._clone_item(footprint))}

    adapter._apply_change_to_states(
        states,
        {
            "item_uuid": "fp-1",
            "operation": "footprint.field.modify",
            "path": "/items/fp-1/data/reference_field/text/text/position/x_nm",
            "value": "1600000",
        },
    )

    replayed = states["fp-1"].raw_item
    assert (replayed.position.x, replayed.position.y) == (1_000_000, 2_000_000)
    assert (replayed.reference_field.text.position.x, replayed.reference_field.text.position.y) == (
        1_600_000,
        2_100_000,
    )


def test_replay_footprint_rotation_preserves_3d_models():
    footprint = FootprintInstance()
    model = Footprint3DModel()
    model.filename = "${KICAD10_3DMODEL_DIR}/Package.step"
    footprint.definition.add_item(model)

    KiCadBoardAdapter._apply_footprint_transform(
        footprint,
        {
            "position": {"x_nm": "6000000", "y_nm": "9000000"},
            "orientation": {"value_degrees": 90},
        },
    )

    assert [item.filename for item in footprint.definition.models] == [
        "${KICAD10_3DMODEL_DIR}/Package.step"
    ]


def test_prepare_replay_reverts_matching_board_to_saved_state(tmp_path, monkeypatch):
    board_path = tmp_path / "demo.kicad_pcb"
    board = FakeBoard([])
    board.name = str(board_path)
    adapter = KiCadBoardAdapter(object(), board)
    monkeypatch.setattr(adapter, "REVERT_SETTLE_SECONDS", 0)

    baseline = adapter.prepare_replay(str(board_path))

    assert board.reverted
    assert baseline.board_name == str(board_path)


def test_prepare_recording_reverts_board_to_saved_initial_state(tmp_path, monkeypatch):
    board_path = tmp_path / "demo.kicad_pcb"
    board = FakeBoard([])
    board.name = str(board_path)
    adapter = KiCadBoardAdapter(object(), board)
    monkeypatch.setattr(adapter, "REVERT_SETTLE_SECONDS", 0)

    baseline = adapter.prepare_recording()

    assert board.reverted
    assert baseline.board_name == str(board_path)


def test_fill_board_creates_recordable_zone_per_selected_layer():
    board = FillBoard(
        [
            edge_segment("edge-1", (0, 0), (20_000_000, 0)),
            edge_segment("edge-2", (20_000_000, 0), (20_000_000, 10_000_000)),
            edge_segment("edge-3", (20_000_000, 10_000_000), (0, 10_000_000)),
            edge_segment("edge-4", (0, 10_000_000), (0, 0)),
        ]
    )
    adapter = KiCadBoardAdapter(object(), board)

    count = adapter.fill_board_copper("gnd", ("F.Cu", "B.Cu"))

    assert count == 2
    assert [list(zone.layers) for zone in board.created] == [
        [BoardLayer.BL_F_Cu],
        [BoardLayer.BL_B_Cu],
    ]
    assert all(zone.net.name == "GND" for zone in board.created)
    assert all(len(zone.outline.outline.nodes) == 4 for zone in board.created)
    assert board.commit_message == "KiLog: create board zones for GND"
    assert not board.refilled
    for zone in board.created:
        restored = Zone(zone.proto)
        connection = restored.proto.copper_settings.connection
        assert connection.zone_connection == ZoneConnectionStyle.ZCS_THERMAL
        assert connection.thermal_spokes.gap.value_nm == 500_000
        assert connection.thermal_spokes.width.value_nm == 500_000
        assert connection.thermal_spokes.width.value_nm >= restored.min_thickness


def test_magnetic_void_prefers_f_silkscreen_body_over_f_fab():
    inductor = with_id(FootprintInstance(), "inductor-outline")
    inductor.position = Vector2.from_xy(10_000_000, 5_000_000)
    inductor.reference_field.text.value = "L1"
    fab = BoardRectangle()
    fab.layer = BoardLayer.BL_F_Fab
    fab.top_left = Vector2.from_xy(6_000_000, 3_000_000)
    fab.bottom_right = Vector2.from_xy(14_000_000, 7_000_000)
    inductor.definition.add_item(fab)
    silk = BoardRectangle()
    silk.layer = BoardLayer.BL_F_SilkS
    silk.top_left = Vector2.from_xy(5_000_000, 2_000_000)
    silk.bottom_right = Vector2.from_xy(15_000_000, 8_000_000)
    inductor.definition.add_item(silk)

    loops = KiCadBoardAdapter._magnetic_keepout_loops(
        KiCadBoardAdapter(object(), FakeBoard([inductor])).snapshot()
    )

    assert loops == [[
        (5_000_000, 2_000_000),
        (15_000_000, 2_000_000),
        (15_000_000, 8_000_000),
        (5_000_000, 8_000_000),
    ]]


def test_magnetic_void_uses_f_fab_when_silkscreen_has_no_closed_body():
    transformer = with_id(FootprintInstance(), "transformer-fab")
    transformer.position = Vector2.from_xy(10_000_000, 5_000_000)
    transformer.reference_field.text.value = "T1"
    fab = BoardRectangle()
    fab.layer = BoardLayer.BL_F_Fab
    fab.top_left = Vector2.from_xy(6_000_000, 3_000_000)
    fab.bottom_right = Vector2.from_xy(14_000_000, 7_000_000)
    transformer.definition.add_item(fab)

    loops = KiCadBoardAdapter._magnetic_keepout_loops(
        KiCadBoardAdapter(object(), FakeBoard([transformer])).snapshot()
    )

    assert loops == [[
        (6_000_000, 3_000_000),
        (14_000_000, 3_000_000),
        (14_000_000, 7_000_000),
        (6_000_000, 7_000_000),
    ]]


def test_fill_board_falls_back_to_center_between_magnetic_pad_rows():
    transformer = with_id(FootprintInstance(), "transformer-1")
    transformer.position = Vector2.from_xy(10_000_000, 5_000_000)
    transformer.reference_field.text.value = "T1"
    for y in (1_000_000, 9_000_000):
        for x in (6_000_000, 8_500_000, 11_500_000, 14_000_000):
            pad = Pad()
            pad.position = Vector2.from_xy(x, y)
            pad.padstack.copper_layers[0].size = Vector2.from_xy(1_000_000, 2_000_000)
            transformer.definition.add_item(pad)
    board = FillBoard(
        [
            transformer,
            edge_segment("edge-1", (0, 0), (20_000_000, 0)),
            edge_segment("edge-2", (20_000_000, 0), (20_000_000, 10_000_000)),
            edge_segment("edge-3", (20_000_000, 10_000_000), (0, 10_000_000)),
            edge_segment("edge-4", (0, 10_000_000), (0, 0)),
        ]
    )

    KiCadBoardAdapter(object(), board).fill_board_copper("GND", ("F.Cu", "B.Cu"))

    assert len(board.created) == 2
    for zone in board.created:
        assert len(zone.outline.holes) == 1
        assert [(node.point.x, node.point.y) for node in zone.outline.holes[0].nodes] == [
            (5_500_000, 2_000_000),
            (14_500_000, 2_000_000),
            (14_500_000, 8_000_000),
            (5_500_000, 8_000_000),
        ]


def test_magnetic_center_void_uses_gap_between_two_inductor_pads():
    inductor = with_id(FootprintInstance(), "inductor-1")
    inductor.position = Vector2.from_xy(10_000_000, 5_000_000)
    inductor.reference_field.text.value = "L1"
    for x in (5_000_000, 15_000_000):
        pad = Pad()
        pad.position = Vector2.from_xy(x, 5_000_000)
        pad.padstack.copper_layers[0].size = Vector2.from_xy(2_000_000, 4_000_000)
        inductor.definition.add_item(pad)

    loops = KiCadBoardAdapter._magnetic_keepout_loops(
        KiCadBoardAdapter(object(), FakeBoard([inductor])).snapshot()
    )

    assert loops == [[
        (6_000_000, 3_000_000),
        (14_000_000, 3_000_000),
        (14_000_000, 7_000_000),
        (6_000_000, 7_000_000),
    ]]


def test_fill_board_adds_one_local_zone_for_same_net_pads_in_a_footprint():
    component = with_id(FootprintInstance(), "component-1")
    component.position = Vector2.from_xy(10_000_000, 5_000_000)
    component.reference_field.text.value = "U1"
    for x in (7_000_000, 13_000_000):
        pad = Pad()
        pad.position = Vector2.from_xy(x, 5_000_000)
        pad.net = Net(name="VCC")
        pad.pad_type = PadType.PT_SMD
        pad.padstack.copper_layers[0].size = Vector2.from_xy(2_000_000, 2_000_000)
        component.definition.add_item(pad)
    board = FillBoard(
        [
            component,
            edge_segment("edge-1", (0, 0), (20_000_000, 0)),
            edge_segment("edge-2", (20_000_000, 0), (20_000_000, 10_000_000)),
            edge_segment("edge-3", (20_000_000, 10_000_000), (0, 10_000_000)),
            edge_segment("edge-4", (0, 10_000_000), (0, 0)),
        ]
    )

    count = KiCadBoardAdapter(object(), board).fill_board_copper(
        "GND", ("F.Cu", "B.Cu")
    )

    assert count == 3
    local_zone = board.created[2]
    assert local_zone.net.name == "VCC"
    assert list(local_zone.layers) == [BoardLayer.BL_F_Cu]
    assert local_zone.priority == 1
    connection = Zone(local_zone.proto).proto.copper_settings.connection
    assert connection.zone_connection == ZoneConnectionStyle.ZCS_THERMAL
    assert connection.thermal_spokes.gap.value_nm == 500_000
    assert connection.thermal_spokes.width.value_nm == 500_000
    assert [(node.point.x, node.point.y) for node in local_zone.outline.outline.nodes] == [
        (5_750_000, 3_750_000),
        (14_250_000, 3_750_000),
        (14_250_000, 6_250_000),
        (5_750_000, 6_250_000),
    ]


def test_fill_board_replaces_legacy_full_board_zone_with_stale_keepout():
    old_zone = with_id(Zone(), "old-full-board-zone")
    old_zone.net = Net(name="GND")
    old_zone.layers = [BoardLayer.BL_F_Cu]
    old_zone.outline = KiCadBoardAdapter._zone_outline(
        [
            [(0, 0), (20_000_000, 0), (20_000_000, 10_000_000), (0, 10_000_000)],
            [(1_000_000, 1_000_000), (2_000_000, 1_000_000), (2_000_000, 2_000_000), (1_000_000, 2_000_000)],
        ]
    )
    board = FillBoard(
        [
            old_zone,
            edge_segment("edge-1", (0, 0), (20_000_000, 0)),
            edge_segment("edge-2", (20_000_000, 0), (20_000_000, 10_000_000)),
            edge_segment("edge-3", (20_000_000, 10_000_000), (0, 10_000_000)),
            edge_segment("edge-4", (0, 10_000_000), (0, 0)),
        ]
    )

    KiCadBoardAdapter(object(), board).fill_board_copper("GND", ("F.Cu",))

    assert board.removed == ["old-full-board-zone"]
    assert len(board.created) == 1
    assert board.created[0].name == "KiLog full-board GND BL_F_Cu"


def test_fanout_creates_trace_and_via_for_matching_smd_pads_only():
    footprint = with_id(FootprintInstance(), "fp-fanout")
    footprint.position = Vector2.from_xy(10_000_000, 10_000_000)
    footprint.layer = BoardLayer.BL_F_Cu
    matching_pad = Pad()
    matching_pad.position = Vector2.from_xy(12_000_000, 10_000_000)
    matching_pad.net = Net(name="GND")
    matching_pad.pad_type = PadType.PT_SMD
    other_pad = Pad()
    other_pad.position = Vector2.from_xy(10_000_000, 12_000_000)
    other_pad.net = Net(name="VCC")
    other_pad.pad_type = PadType.PT_SMD
    footprint.definition.add_item(matching_pad)
    footprint.definition.add_item(other_pad)
    board = FillBoard(
        [
            footprint,
            edge_segment("edge-1", (0, 0), (30_000_000, 0)),
            edge_segment("edge-2", (30_000_000, 0), (30_000_000, 30_000_000)),
            edge_segment("edge-3", (30_000_000, 30_000_000), (0, 30_000_000)),
            edge_segment("edge-4", (0, 30_000_000), (0, 0)),
        ]
    )
    adapter = KiCadBoardAdapter(object(), board)

    count = adapter.fanout_net("gnd", "0.40")

    assert count == 1
    track, via = board.created
    assert isinstance(track, Track)
    assert isinstance(via, Via)
    assert track.net.name == via.net.name == "GND"
    assert track.layer == BoardLayer.BL_F_Cu
    assert (track.start.x, track.start.y) == (12_000_000, 10_000_000)
    assert (track.end.x, track.end.y) == (12_500_000, 10_000_000)
    assert (via.position.x, via.position.y) == (12_500_000, 10_000_000)
    assert track.width == 400_000
    assert via.diameter == 400_000
    assert via.drill_diameter == 200_000
    assert board.commit_message == "KiLog: fanout GND"


def test_fanout_uses_pad_size_and_board_bounds_to_place_via_safely():
    footprint = with_id(FootprintInstance(), "fp-large-pad")
    footprint.position = Vector2.from_xy(18_000_000, 10_000_000)
    footprint.layer = BoardLayer.BL_F_Cu
    pad = Pad()
    pad.position = Vector2.from_xy(18_500_000, 10_000_000)
    pad.net = Net(name="GND")
    pad.pad_type = PadType.PT_SMD
    pad.padstack.copper_layers[0].size = Vector2.from_xy(4_000_000, 4_000_000)
    footprint.definition.add_item(pad)
    board = FillBoard(
        [
            footprint,
            edge_segment("edge-1", (0, 0), (20_000_000, 0)),
            edge_segment("edge-2", (20_000_000, 0), (20_000_000, 20_000_000)),
            edge_segment("edge-3", (20_000_000, 20_000_000), (0, 20_000_000)),
            edge_segment("edge-4", (0, 20_000_000), (0, 0)),
        ]
    )

    count = KiCadBoardAdapter(object(), board).fanout_net("GND")

    assert count == 1
    via = board.created[1]
    assert max(
        abs(via.position.x - pad.position.x),
        abs(via.position.y - pad.position.y),
    ) >= 2_000_000 + 200_000 + 200_000
    assert via.position.x == pad.position.x or via.position.y == pad.position.y
    assert 500_000 <= via.position.x <= 19_500_000
    assert 500_000 <= via.position.y <= 19_500_000


def test_fanout_via_clears_board_edge_by_at_least_point_five_mm():
    footprint = with_id(FootprintInstance(), "fp-near-edge")
    footprint.position = Vector2.from_xy(2_400_000, 10_000_000)
    footprint.layer = BoardLayer.BL_F_Cu
    pad = Pad()
    pad.position = Vector2.from_xy(1_400_000, 10_000_000)
    pad.net = Net(name="GND")
    pad.pad_type = PadType.PT_SMD
    footprint.definition.add_item(pad)
    board = FillBoard(
        [
            footprint,
            edge_segment("edge-1", (0, 0), (20_000_000, 0)),
            edge_segment("edge-2", (20_000_000, 0), (20_000_000, 20_000_000)),
            edge_segment("edge-3", (20_000_000, 20_000_000), (0, 20_000_000)),
            edge_segment("edge-4", (0, 20_000_000), (0, 0)),
        ]
    )

    count = KiCadBoardAdapter(object(), board).fanout_net("GND")

    assert count == 1
    via = board.created[1]
    # The preferred outward candidate has its center inside the board at x=0.4 mm,
    # but its 0.4 mm via would leave only 0.2 mm to Edge.Cuts and must be rejected.
    assert (via.position.x, via.position.y) != (400_000, 10_000_000)
    assert min(
        via.position.x,
        via.position.y,
        20_000_000 - via.position.x,
        20_000_000 - via.position.y,
    ) >= 800_000


def test_fanout_ignores_components_outside_board():
    inside = with_id(FootprintInstance(), "fp-inside")
    inside.position = Vector2.from_xy(5_000_000, 5_000_000)
    inside.layer = BoardLayer.BL_F_Cu
    inside_pad = Pad()
    inside_pad.position = Vector2.from_xy(6_000_000, 5_000_000)
    inside_pad.net = Net(name="GND")
    inside_pad.pad_type = PadType.PT_SMD
    inside.definition.add_item(inside_pad)

    outside = with_id(FootprintInstance(), "fp-outside")
    outside.position = Vector2.from_xy(25_000_000, 5_000_000)
    outside.layer = BoardLayer.BL_F_Cu
    outside_pad = Pad()
    outside_pad.position = Vector2.from_xy(25_000_000, 5_000_000)
    outside_pad.net = Net(name="GND")
    outside_pad.pad_type = PadType.PT_SMD
    outside.definition.add_item(outside_pad)
    board = FillBoard(
        [
            inside,
            outside,
            edge_segment("edge-1", (0, 0), (20_000_000, 0)),
            edge_segment("edge-2", (20_000_000, 0), (20_000_000, 20_000_000)),
            edge_segment("edge-3", (20_000_000, 20_000_000), (0, 20_000_000)),
            edge_segment("edge-4", (0, 20_000_000), (0, 0)),
        ]
    )

    count = KiCadBoardAdapter(object(), board).fanout_net("GND")

    assert count == 1
    assert len(board.created) == 2


def test_fanout_via_avoids_other_on_board_pads():
    footprint = with_id(FootprintInstance(), "fp-pad-obstacle")
    footprint.position = Vector2.from_xy(9_000_000, 10_000_000)
    footprint.layer = BoardLayer.BL_F_Cu
    source = Pad()
    source.position = Vector2.from_xy(10_000_000, 10_000_000)
    source.net = Net(name="GND")
    source.pad_type = PadType.PT_SMD
    blocker = Pad()
    blocker.position = Vector2.from_xy(11_500_000, 10_000_000)
    blocker.net = Net(name="VCC")
    blocker.pad_type = PadType.PT_SMD
    blocker.padstack.copper_layers[0].size = Vector2.from_xy(1_000_000, 1_000_000)
    footprint.definition.add_item(source)
    footprint.definition.add_item(blocker)
    board = FillBoard(
        [
            footprint,
            edge_segment("edge-1", (0, 0), (20_000_000, 0)),
            edge_segment("edge-2", (20_000_000, 0), (20_000_000, 20_000_000)),
            edge_segment("edge-3", (20_000_000, 20_000_000), (0, 20_000_000)),
            edge_segment("edge-4", (0, 20_000_000), (0, 0)),
        ]
    )

    KiCadBoardAdapter(object(), board).fanout_net("GND")

    via = board.created[1]
    blocker_radius = math.hypot(1_000_000, 1_000_000) / 2
    assert math.hypot(
        via.position.x - blocker.position.x,
        via.position.y - blocker.position.y,
    ) >= blocker_radius + 300_000 + 200_000
    assert via.position.x == source.position.x or via.position.y == source.position.y


def test_fanout_trace_uses_rectangular_pad_clearance_instead_of_diagonal_radius():
    footprint = with_id(FootprintInstance(), "fp-rectangular-clearance")
    footprint.position = Vector2.from_xy(11_500_000, 10_000_000)
    footprint.layer = BoardLayer.BL_F_Cu

    source = Pad()
    source.position = Vector2.from_xy(10_000_000, 10_000_000)
    source.net = Net(name="GND")
    source.pad_type = PadType.PT_SMD
    source.padstack.copper_layers[0].size = Vector2.from_xy(1_100_000, 3_700_000)

    left_blocker = Pad()
    left_blocker.position = Vector2.from_xy(7_750_000, 10_000_000)
    left_blocker.net = Net(name="VCC")
    left_blocker.pad_type = PadType.PT_SMD
    left_blocker.padstack.copper_layers[0].size = Vector2.from_xy(
        1_100_000, 3_700_000
    )

    right_blocker = Pad()
    right_blocker.position = Vector2.from_xy(13_000_000, 10_000_000)
    right_blocker.net = Net(name="VCC")
    right_blocker.pad_type = PadType.PT_SMD
    right_blocker.padstack.copper_layers[0].size = Vector2.from_xy(
        1_100_000, 3_700_000
    )

    footprint.definition.add_item(source)
    footprint.definition.add_item(left_blocker)
    footprint.definition.add_item(right_blocker)
    board = FillBoard(
        [
            footprint,
            edge_segment("edge-1", (0, 0), (20_000_000, 0)),
            edge_segment("edge-2", (20_000_000, 0), (20_000_000, 20_000_000)),
            edge_segment("edge-3", (20_000_000, 20_000_000), (0, 20_000_000)),
            edge_segment("edge-4", (0, 20_000_000), (0, 0)),
        ]
    )

    KiCadBoardAdapter(object(), board).fanout_net("GND", 0.5)

    track = board.created[0]
    assert track.start.x == track.end.x == source.position.x
    assert abs(track.end.y - track.start.y) == 2_250_000


def test_fanout_trace_avoids_crossing_other_net_track_on_same_layer():
    footprint = with_id(FootprintInstance(), "fp-track-obstacle")
    footprint.position = Vector2.from_xy(9_000_000, 10_000_000)
    footprint.layer = BoardLayer.BL_F_Cu
    source = Pad()
    source.position = Vector2.from_xy(10_000_000, 10_000_000)
    source.net = Net(name="GND")
    source.pad_type = PadType.PT_SMD
    footprint.definition.add_item(source)
    blocker = with_id(Track(), "vcc-track")
    blocker.net = Net(name="VCC")
    blocker.layer = BoardLayer.BL_F_Cu
    blocker.start = Vector2.from_xy(10_400_000, 9_900_000)
    blocker.end = Vector2.from_xy(10_400_000, 10_100_000)
    blocker.width = 20_000
    board = FillBoard(
        [
            footprint,
            blocker,
            edge_segment("edge-1", (0, 0), (20_000_000, 0)),
            edge_segment("edge-2", (20_000_000, 0), (20_000_000, 20_000_000)),
            edge_segment("edge-3", (20_000_000, 20_000_000), (0, 20_000_000)),
            edge_segment("edge-4", (0, 20_000_000), (0, 0)),
        ]
    )

    KiCadBoardAdapter(object(), board).fanout_net("GND", 0.1)

    fanout = board.created[0]
    assert (fanout.end.x, fanout.end.y) != (11_000_000, 10_000_000)


def test_fanout_via_avoids_other_net_track_on_opposite_layer():
    footprint = with_id(FootprintInstance(), "fp-via-track-obstacle")
    footprint.position = Vector2.from_xy(9_000_000, 10_000_000)
    footprint.layer = BoardLayer.BL_F_Cu
    source = Pad()
    source.position = Vector2.from_xy(10_000_000, 10_000_000)
    source.net = Net(name="GND")
    source.pad_type = PadType.PT_SMD
    footprint.definition.add_item(source)
    blocker = with_id(Track(), "vcc-back-track")
    blocker.net = Net(name="VCC")
    blocker.layer = BoardLayer.BL_B_Cu
    blocker.start = Vector2.from_xy(11_000_000, 9_500_000)
    blocker.end = Vector2.from_xy(11_000_000, 10_500_000)
    blocker.width = 200_000
    board = FillBoard(
        [
            footprint,
            blocker,
            edge_segment("edge-1", (0, 0), (20_000_000, 0)),
            edge_segment("edge-2", (20_000_000, 0), (20_000_000, 20_000_000)),
            edge_segment("edge-3", (20_000_000, 20_000_000), (0, 20_000_000)),
            edge_segment("edge-4", (0, 20_000_000), (0, 0)),
        ]
    )

    KiCadBoardAdapter(object(), board).fanout_net("GND")

    via = board.created[1]
    assert (via.position.x, via.position.y) != (11_000_000, 10_000_000)


def test_fanout_trace_avoids_other_net_arc_on_same_layer():
    footprint = with_id(FootprintInstance(), "fp-arc-obstacle")
    footprint.position = Vector2.from_xy(9_000_000, 10_000_000)
    footprint.layer = BoardLayer.BL_F_Cu
    source = Pad()
    source.position = Vector2.from_xy(10_000_000, 10_000_000)
    source.net = Net(name="GND")
    source.pad_type = PadType.PT_SMD
    footprint.definition.add_item(source)
    blocker = with_id(ArcTrack(), "vcc-arc")
    blocker.net = Net(name="VCC")
    blocker.layer = BoardLayer.BL_F_Cu
    blocker.start = Vector2.from_xy(10_400_000, 9_900_000)
    blocker.mid = Vector2.from_xy(10_490_000, 10_000_000)
    blocker.end = Vector2.from_xy(10_400_000, 10_100_000)
    blocker.width = 1
    board = FillBoard(
        [
            footprint,
            blocker,
            edge_segment("edge-1", (0, 0), (20_000_000, 0)),
            edge_segment("edge-2", (20_000_000, 0), (20_000_000, 20_000_000)),
            edge_segment("edge-3", (20_000_000, 20_000_000), (0, 20_000_000)),
            edge_segment("edge-4", (0, 20_000_000), (0, 0)),
        ]
    )

    KiCadBoardAdapter(object(), board).fanout_net("GND", 0.1)

    fanout = board.created[0]
    assert (fanout.end.x, fanout.end.y) != (11_000_000, 10_000_000)


def test_fanout_chooses_short_axis_of_rectangular_pad():
    footprint = with_id(FootprintInstance(), "fp-short-fanout")
    footprint.position = Vector2.from_xy(8_000_000, 10_000_000)
    footprint.layer = BoardLayer.BL_F_Cu
    pad = Pad()
    pad.position = Vector2.from_xy(10_000_000, 10_000_000)
    pad.net = Net(name="GND")
    pad.pad_type = PadType.PT_SMD
    pad.padstack.copper_layers[0].size = Vector2.from_xy(6_000_000, 1_000_000)
    footprint.definition.add_item(pad)
    board = FillBoard(
        [
            footprint,
            edge_segment("edge-1", (0, 0), (20_000_000, 0)),
            edge_segment("edge-2", (20_000_000, 0), (20_000_000, 20_000_000)),
            edge_segment("edge-3", (20_000_000, 20_000_000), (0, 20_000_000)),
            edge_segment("edge-4", (0, 20_000_000), (0, 0)),
        ]
    )

    KiCadBoardAdapter(object(), board).fanout_net("GND")

    track = board.created[0]
    assert track.end.x == track.start.x
    assert abs(track.end.y - track.start.y) == 900_000


def test_fanout_inherits_width_from_trace_connected_to_pad():
    footprint = with_id(FootprintInstance(), "fp-width")
    footprint.position = Vector2.from_xy(8_000_000, 10_000_000)
    footprint.layer = BoardLayer.BL_F_Cu
    pad = Pad()
    pad.position = Vector2.from_xy(10_000_000, 10_000_000)
    pad.net = Net(name="GND")
    pad.pad_type = PadType.PT_SMD
    pad.padstack.copper_layers[0].size = Vector2.from_xy(1_000_000, 1_000_000)
    footprint.definition.add_item(pad)
    connected_track = with_id(Track(), "existing-track")
    connected_track.net = Net(name="GND")
    connected_track.layer = BoardLayer.BL_F_Cu
    connected_track.start = pad.position
    connected_track.end = Vector2.from_xy(10_000_000, 8_000_000)
    connected_track.width = 650_000
    board = FillBoard(
        [
            footprint,
            connected_track,
            edge_segment("edge-1", (0, 0), (20_000_000, 0)),
            edge_segment("edge-2", (20_000_000, 0), (20_000_000, 20_000_000)),
            edge_segment("edge-3", (20_000_000, 20_000_000), (0, 20_000_000)),
            edge_segment("edge-4", (0, 20_000_000), (0, 0)),
        ]
    )

    KiCadBoardAdapter(object(), board).fanout_net("GND", 0.25)

    assert board.created[0].width == 650_000


def test_fanout_rejects_invalid_default_width():
    adapter = KiCadBoardAdapter(object(), FillBoard([]))

    with pytest.raises(RecorderError, match="Width must be greater than zero"):
        adapter.fanout_net("GND", 0)


@pytest.mark.parametrize(
    ("diameter", "message"),
    [
        ("wide", "Via diameter must be a number"),
        (0.2, "Via diameter must be greater than the Drill diameter"),
    ],
)
def test_fanout_rejects_invalid_via_diameter(diameter, message):
    adapter = KiCadBoardAdapter(object(), FillBoard([]))

    with pytest.raises(RecorderError, match=message):
        adapter.fanout_net("GND", 0.1, diameter)


@pytest.mark.parametrize(
    ("drill", "message"),
    [
        ("narrow", "Drill diameter must be a number"),
        (0, "Drill diameter must be greater than zero"),
        (0.4, "Via diameter must be greater than the Drill diameter"),
    ],
)
def test_fanout_rejects_invalid_via_drill(drill, message):
    adapter = KiCadBoardAdapter(object(), FillBoard([]))

    with pytest.raises(RecorderError, match=message):
        adapter.fanout_net("GND", 0.1, 0.4, drill)


def test_fanout_reports_pad_when_no_position_can_be_found():
    footprint = with_id(FootprintInstance(), "fp-failed-fanout")
    footprint.position = Vector2.from_xy(1_000_000, 1_000_000)
    footprint.layer = BoardLayer.BL_F_Cu
    footprint.reference_field.text.value = "L2"
    pad = Pad()
    pad.number = "1"
    pad.position = footprint.position
    pad.net = Net(name="GND")
    pad.pad_type = PadType.PT_SMD
    footprint.definition.add_item(pad)
    board = FillBoard(
        [
            footprint,
            edge_segment("edge-1", (0, 0), (2_000_000, 0)),
            edge_segment("edge-2", (2_000_000, 0), (2_000_000, 2_000_000)),
            edge_segment("edge-3", (2_000_000, 2_000_000), (0, 2_000_000)),
            edge_segment("edge-4", (0, 2_000_000), (0, 0)),
        ]
    )

    with pytest.raises(
        RecorderError,
        match=r"Fanout incomplete.*created 0.*1 pad\(s\): L2\.1",
    ):
        KiCadBoardAdapter(object(), board).fanout_net("GND")

    assert board.created == []


def test_fanout_commits_successes_before_reporting_incomplete_pads(monkeypatch):
    footprint = with_id(FootprintInstance(), "fp-partial-fanout")
    footprint.position = Vector2.from_xy(9_000_000, 10_000_000)
    footprint.layer = BoardLayer.BL_F_Cu
    footprint.reference_field.text.value = "U1"
    successful = Pad()
    successful.number = "1"
    successful.position = Vector2.from_xy(10_000_000, 10_000_000)
    successful.net = Net(name="GND")
    successful.pad_type = PadType.PT_SMD
    failed = Pad()
    failed.number = "2"
    failed.position = Vector2.from_xy(10_000_000, 15_000_000)
    failed.net = Net(name="GND")
    failed.pad_type = PadType.PT_SMD
    footprint.definition.add_item(successful)
    footprint.definition.add_item(failed)
    board = FillBoard(
        [
            footprint,
            edge_segment("edge-1", (0, 0), (20_000_000, 0)),
            edge_segment("edge-2", (20_000_000, 0), (20_000_000, 20_000_000)),
            edge_segment("edge-3", (20_000_000, 20_000_000), (0, 20_000_000)),
            edge_segment("edge-4", (0, 20_000_000), (0, 0)),
        ]
    )
    adapter = KiCadBoardAdapter(object(), board)
    find_position = adapter._find_fanout_position

    def fail_second_pad(pad, *args, **kwargs):
        if pad.number == "2":
            return None
        return find_position(pad, *args, **kwargs)

    monkeypatch.setattr(adapter, "_find_fanout_position", fail_second_pad)

    with pytest.raises(
        RecorderError,
        match=r"Fanout incomplete.*created 1.*1 pad\(s\): U1\.2",
    ):
        adapter.fanout_net("GND")

    assert len(board.created) == 2
    assert board.commit_count == 1
    assert board.commit_message == "KiLog: fanout GND"


def test_fanout_skips_pad_already_connected_to_via():
    footprint = with_id(FootprintInstance(), "fp-already-fanned")
    footprint.position = Vector2.from_xy(8_000_000, 10_000_000)
    footprint.layer = BoardLayer.BL_F_Cu
    pad = Pad()
    pad.position = Vector2.from_xy(10_000_000, 10_000_000)
    pad.net = Net(name="GND")
    pad.pad_type = PadType.PT_SMD
    pad.padstack.copper_layers[0].size = Vector2.from_xy(1_000_000, 1_000_000)
    footprint.definition.add_item(pad)
    existing_track = with_id(Track(), "fanout-track")
    existing_track.net = Net(name="GND")
    existing_track.layer = BoardLayer.BL_F_Cu
    existing_track.start = pad.position
    existing_track.end = Vector2.from_xy(11_500_000, 10_000_000)
    existing_track.width = 500_000
    existing_via = with_id(Via(), "fanout-via")
    existing_via.net = Net(name="GND")
    existing_via.position = existing_track.end
    existing_via.diameter = 600_000
    existing_via.drill_diameter = 300_000
    board = FillBoard(
        [
            footprint,
            existing_track,
            existing_via,
            edge_segment("edge-1", (0, 0), (20_000_000, 0)),
            edge_segment("edge-2", (20_000_000, 0), (20_000_000, 20_000_000)),
            edge_segment("edge-3", (20_000_000, 20_000_000), (0, 20_000_000)),
            edge_segment("edge-4", (0, 20_000_000), (0, 0)),
        ]
    )

    count = KiCadBoardAdapter(object(), board).fanout_net("GND", 0.5)

    assert count == 0
    assert board.created == []


def test_replay_recreates_recorded_copper_zone():
    source_zone = with_id(Zone(), "zone-front")
    source_zone.net = Net(name="GND")
    source_zone.layers = [BoardLayer.BL_F_Cu]
    source_zone.outline = KiCadBoardAdapter._zone_outline(
        [[(0, 0), (20_000_000, 0), (20_000_000, 10_000_000), (0, 10_000_000)]]
    )
    source_state = KiCadBoardAdapter(object(), FakeBoard([source_zone])).snapshot()
    board = FillBoard([])
    adapter = KiCadBoardAdapter(object(), board)

    result = adapter.apply_step(
        ({
            "item_uuid": "zone-front",
            "operation": "zone.add",
            "item": source_state.items["zone-front"].log_value(),
        },)
    )

    assert result.items["zone-front"].kind == "zone"
    assert board.created[0].net.name == "GND"
    assert list(board.created[0].layers) == [BoardLayer.BL_F_Cu]


def test_replay_applies_multiple_changes_as_one_board_commit():
    zones = []
    for item_uuid, layer in (
        ("zone-front", BoardLayer.BL_F_Cu),
        ("zone-back", BoardLayer.BL_B_Cu),
    ):
        zone = with_id(Zone(), item_uuid)
        zone.net = Net(name="GND")
        zone.layers = [layer]
        zones.append(zone)
    source = KiCadBoardAdapter(object(), FakeBoard(zones)).snapshot()
    board = FillBoard([])
    adapter = KiCadBoardAdapter(object(), board)

    result = adapter.apply_step(
        tuple(
            {
                "item_uuid": item_uuid,
                "operation": "zone.add",
                "item": source.items[item_uuid].log_value(),
            }
            for item_uuid in ("zone-front", "zone-back")
        ),
        "KiLog replay: step 1",
    )

    assert set(result.items) == {"zone-front", "zone-back"}
    assert board.create_calls == 1
    assert board.commit_count == 1
    assert board.commit_message == "KiLog replay: step 1"


def test_replay_zone_refill_rebuilds_derived_copper_polygons():
    zone = with_id(Zone(), "zone-front")
    zone.net = Net(name="GND")
    zone.layers = [BoardLayer.BL_F_Cu]
    board = FillBoard([zone])
    adapter = KiCadBoardAdapter(object(), board)

    result = adapter.apply_step(
        ({
            "item_uuid": "zone-front",
            "operation": "zone.refill",
            "path": "/items/zone-front/data/filled",
            "value": True,
        },),
        "KiLog replay: final fill",
    )

    assert result.items["zone-front"].data["filled"] is True
    assert board.refilled
    assert board.commit_count == 1


def test_portable_skill_uses_same_serialized_copper_settings():
    import skill

    expected = KiCadBoardAdapter._new_copper_zone()
    actual = skill.KiCadBoardAdapter._new_copper_zone()
    assert actual.proto.copper_settings == expected.proto.copper_settings
