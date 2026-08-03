from __future__ import annotations

import pytest

from kilog.storage import next_counter, normalize_stem, numbered_path, write_json_atomic


def test_filename_normalization_and_counter(tmp_path):
    assert normalize_stem(" ref.kicad_pcb ", ".kicad_pcb") == "ref"
    assert normalize_stem("记录.json", ".json") == "记录"
    (tmp_path / "log_000003.json").write_text("{}", encoding="utf-8")
    assert next_counter(tmp_path, "log", ".json") == 4
    assert numbered_path(tmp_path, "log", 4, ".json").name == "log_000004.json"


@pytest.mark.parametrize("name", ["", "../log", "a/b", "CON", "bad:name"])
def test_unsafe_filename_is_rejected(name):
    with pytest.raises(ValueError):
        normalize_stem(name, ".json")


def test_atomic_json_leaves_no_temporary_file(tmp_path):
    path = tmp_path / "log_000001.json"
    write_json_atomic(path, {"ok": True})
    assert path.read_text(encoding="utf-8").strip() == '{\n  "ok": true\n}'
    assert list(tmp_path.glob("*.tmp")) == []
