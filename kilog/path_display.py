from __future__ import annotations

from pathlib import Path


def default_log_name(board_path: Path | None, fallback: str = "ref") -> str:
    """Return the current PCB filename without its extension."""
    if board_path is None:
        return fallback
    stem = Path(board_path).stem.strip()
    return stem or fallback


def trailing_directories(path: Path, count: int = 2) -> str:
    """Return the last ``count`` directory names for a compact path label."""
    if count < 1:
        raise ValueError("count must be at least 1")

    parts = path.parts
    anchor_parts = Path(path.anchor).parts if path.anchor else ()
    named_parts = parts[len(anchor_parts) :]
    if not named_parts:
        return str(path)
    return str(Path(*named_parts[-count:]))
