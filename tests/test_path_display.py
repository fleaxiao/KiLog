from pathlib import Path

import pytest

from kilog.path_display import default_log_name, trailing_directories


def test_uses_current_pcb_filename_as_default_log_name():
    assert default_log_name(Path("project/controller.kicad_pcb")) == "controller"


def test_uses_fallback_when_current_pcb_path_is_unavailable():
    assert default_log_name(None) == "ref"


def test_shows_parent_and_grandparent_directories():
    directory = Path("workspace") / "projects" / "controller"

    assert trailing_directories(directory) == str(Path("projects") / "controller")


def test_keeps_available_directory_when_path_has_only_one_named_part():
    directory = Path("controller")

    assert trailing_directories(directory) == "controller"


def test_rejects_non_positive_directory_count():
    with pytest.raises(ValueError, match="at least 1"):
        trailing_directories(Path("controller"), count=0)
