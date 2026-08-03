from __future__ import annotations

from kipy.board_types import FootprintInstance, Track, Via, Zone

from kilog.kicad_adapter import KiCadBoardAdapter


class FakeBoard:
    name = "C:/project/demo.kicad_pcb"

    def __init__(self, items):
        self.items = items
        self.calls = 0

    def get_items(self, types):
        self.calls += 1
        assert len(types) == 9
        return self.items


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
