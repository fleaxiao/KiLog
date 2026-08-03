from __future__ import annotations

import json
import os
from pathlib import Path
import re
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
        raise ValueError("文件名不能为空")
    if any(char in stem for char in '<>:"/\\|?*'):
        raise ValueError("文件名不能包含路径或 Windows 保留字符")
    if stem.upper() in _WINDOWS_RESERVED:
        raise ValueError(f"{stem} 是系统保留文件名")
    if len(stem) > 120:
        raise ValueError("文件名不能超过 120 个字符")
    return stem


def next_counter(directory: Path, stem: str, suffix: str) -> int:
    pattern = re.compile(rf"^{re.escape(stem)}_(\d+){re.escape(suffix)}$", re.IGNORECASE)
    highest = 0
    if directory.exists():
        for child in directory.iterdir():
            match = pattern.match(child.name)
            if match:
                highest = max(highest, int(match.group(1)))
    return highest + 1


def numbered_path(directory: Path, stem: str, counter: int, suffix: str) -> Path:
    return directory / f"{stem}_{counter:06d}{suffix}"


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
