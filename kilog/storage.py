from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def normalize_stem(value: str, suffix: str) -> str:
    stem = value.strip()
    if stem.lower().endswith(suffix.lower()):
        stem = stem[: -len(suffix)]
    stem = stem.strip().rstrip(". ")
    if not stem or stem in {".", ".."}:
        raise ValueError("The filename cannot be empty.")
    if any(char in stem for char in '<>:"/\\|?*'):
        raise ValueError("The filename cannot contain a path or reserved Windows characters.")
    if stem.upper() in _WINDOWS_RESERVED:
        raise ValueError(f"{stem} is a reserved system filename.")
    if len(stem) > 120:
        raise ValueError("The filename cannot exceed 120 characters.")
    return stem


def snapshot_path(directory: Path, stem: str, position: int) -> Path:
    """Return the PCB snapshot name for an exact position in the recorded log."""
    if position < 0:
        raise ValueError("The recorded position cannot be negative.")
    return directory / f"{stem}_{position:02d}.kicad_pcb"


def write_json_new(path: Path, value: dict[str, Any]) -> None:
    """Create a JSON file while refusing to replace an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
