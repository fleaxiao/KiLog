from __future__ import annotations

import math

import pytest
from kipy.board_types import (
    BoardLayer,
    BoardSegment,
    FootprintInstance,
    Net,
    Pad,
    PadType,
    Track,
    Via,
    Zone,
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
        self.refilled = False

    def get_nets(self):
        return [Net(name="GND"), Net(name="VCC")]

    def begin_commit(self):
        return object()

    def create_items(self, items):
        self.created.extend(items)
        self.items.extend(items)
        return items

    def push_commit(self, _commit, message):
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
    assert (track.end.x, track.end.y) == (13_000_000, 10_000_000)
    assert (via.position.x, via.position.y) == (13_000_000, 10_000_000)
    assert track.width == 400_000
    assert via.diameter == 600_000
    assert via.drill_diameter == 300_000
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
    ) >= 2_000_000 + 300_000 + 200_000
    assert via.position.x == pad.position.x or via.position.y == pad.position.y
    assert 500_000 <= via.position.x <= 19_500_000
    assert 500_000 <= via.position.y <= 19_500_000


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
    assert abs(track.end.y - track.start.y) == 1_000_000


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

    result = adapter.apply_change(
        {
            "item_uuid": "zone-front",
            "operation": "zone.add",
            "after": source_state.items["zone-front"].log_value(),
        }
    )

    assert result.items["zone-front"].kind == "zone"
    assert board.created[0].net.name == "GND"
    assert list(board.created[0].layers) == [BoardLayer.BL_F_Cu]
