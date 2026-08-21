from __future__ import annotations

import json

import pytest

from kilog.storage import (
    normalize_stem,
    snapshot_path,
    write_json_atomic,
    write_json_new,
)


def test_filename_normalization_and_snapshot_path(tmp_path):
    assert normalize_stem(" ref.kicad_pcb ", ".kicad_pcb") == "ref"
    assert normalize_stem("记录.json", ".json") == "记录"
    assert snapshot_path(tmp_path, "ref", 12).name == "ref_12.kicad_pcb"


@pytest.mark.parametrize("name", ["", "../log", "a/b", "CON", "bad:name"])
def test_unsafe_filename_is_rejected(name):
    with pytest.raises(ValueError):
        normalize_stem(name, ".json")


def test_atomic_json_leaves_no_temporary_file(tmp_path):
    path = tmp_path / "log_000001.json"
    write_json_atomic(path, {"ok": True})
    assert path.read_text(encoding="utf-8").strip() == '{\n  "ok": true\n}'
    assert list(tmp_path.glob("*.tmp")) == []


def test_new_json_refuses_to_overwrite(tmp_path):
    path = tmp_path / "log.json"
    write_json_new(path, {"events": []})
    with pytest.raises(FileExistsError):
        write_json_new(path, {"events": [1]})
    assert json.loads(path.read_text(encoding="utf-8"))["events"] == []
