from pathlib import Path

import pytest

from kilog import trace_output
from kilog.selection import export_selection
from types import SimpleNamespace


def test_numbering_uses_numeric_max_and_ignores_other_boards(tmp_path):
    for name in ("board_002.kicad_pcb", "board_010.kicad_pcb", "BOARD_009.KICAD_PCB",
                 "other_100.kicad_pcb", "board_R200.kicad_pcb", "board_900.json"):
        (tmp_path / name).touch()
    assert trace_output.next_trace_path(tmp_path, "board").name == "board_011.kicad_pcb"
    (tmp_path / "board_999.kicad_pcb").touch()
    assert trace_output.next_trace_path(tmp_path, "board").name == "board_1000.kicad_pcb"


@pytest.mark.parametrize("stem", ["../board", "board/name", "CON", "NUL", "a:b", "a\x00b", "", "a" * 256])
def test_invalid_filename_does_not_write(tmp_path, stem):
    with pytest.raises(ValueError):
        trace_output.write_trace(tmp_path, stem, "copy")
    assert list(tmp_path.iterdir()) == []


def test_board_stem_with_suffix_is_preserved(tmp_path):
    assert trace_output.next_trace_path(tmp_path, "board.kicad_pcb").name == "board.kicad_pcb_001.kicad_pcb"


def test_invalid_directory_fails_before_ipc_export(tmp_path):
    calls = []
    adapter = SimpleNamespace(output_directory=tmp_path, save_copy=calls.append)
    file = tmp_path / "file"
    file.touch()
    for invalid in ("", tmp_path / "missing", file):
        with pytest.raises(ValueError):
            export_selection(adapter, {"a"}, invalid)
    assert calls == []


def test_destination_taken_during_write_uses_next_number(tmp_path, monkeypatch):
    publish = trace_output._publish_new
    calls = []
    def compete(pending, output):
        if not calls:
            output.write_text("another exporter", encoding="utf-8")
        calls.append(output)
        publish(pending, output)
    monkeypatch.setattr(trace_output, "_publish_new", compete)
    output = trace_output.write_trace(tmp_path, "board", "complete copy")
    assert output.name == "board_002.kicad_pcb"
    assert output.read_text() == "complete copy"
    assert (tmp_path / "board_001.kicad_pcb").read_text() == "another exporter"
    assert len(list(tmp_path.iterdir())) == 2


def test_failed_flush_cleans_temporary_file_and_does_not_consume_number(tmp_path, monkeypatch):
    previous = tmp_path / "board_001.kicad_pcb"
    previous.write_text("previous", encoding="utf-8")
    def fail(_fd):
        raise OSError("disk full")
    with monkeypatch.context() as patch:
        patch.setattr(trace_output.os, "fsync", fail)
        with pytest.raises(OSError, match="disk full"):
            trace_output.write_trace(tmp_path, "board", "new")
    assert list(tmp_path.iterdir()) == [previous]
    assert previous.read_text() == "previous"
    assert trace_output.write_trace(tmp_path, "board", "new").name == "board_002.kicad_pcb"
