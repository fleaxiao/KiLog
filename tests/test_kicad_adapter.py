from __future__ import annotations

from kipy.board_types import BoardLayer, BoardSegment, FootprintInstance, Net, Track, Via, Zone
from kipy.geometry import Vector2

from kilog.kicad_adapter import KiCadBoardAdapter


class FakeBoard:
    name = "C:/project/demo.kicad_pcb"

    def __init__(self, items):
        self.items = items
        self.calls = 0
        self.reverted = False

    def get_items(self, types):
        self.calls += 1
        assert len(types) == 9
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
    assert board.commit_message == "KiLog: fill board with GND"
    assert board.refilled


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
