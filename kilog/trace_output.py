"""Numbered Trace exports, published only after a complete private write."""
from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile

from .storage import normalize_stem


def trace_directory(value: str | Path) -> Path:
    if not str(value).strip():
        raise ValueError("Choose a Trace output directory first.")
    directory = Path(value).expanduser().resolve()
    if not directory.is_dir():
        raise ValueError("The Trace output directory does not exist or is not a directory.")
    return directory


def next_trace_path(directory: Path, board_stem: str) -> Path:
    """Continue after the largest matching number, including gaps and >999."""
    stem = normalize_stem(board_stem + ".kicad_pcb", ".kicad_pcb")
    if stem != board_stem or any(ord(char) < 32 for char in stem):
        raise ValueError("The board filename contains invalid characters.")
    pattern = re.compile(re.escape(stem) + r"_([0-9]+)\.kicad_pcb", re.IGNORECASE)
    numbers = [int(match.group(1)) for entry in directory.iterdir()
               if (match := pattern.fullmatch(entry.name))]
    filename = f"{stem}_{max(numbers, default=0) + 1:03d}.kicad_pcb"
    if len(filename.encode("utf-16-le")) // 2 > 255:
        raise ValueError("The Trace output filename is too long.")
    return directory / filename


def _publish_new(pending: Path, output: Path) -> None:
    # Windows rename refuses an existing destination. POSIX rename replaces it,
    # so use an atomic hard link there; both publish an already complete file.
    if os.name == "nt":
        pending.rename(output)
    else:
        os.link(pending, output)


def write_trace(directory: Path, board_stem: str, content: str) -> Path:
    directory = trace_directory(directory)
    output = next_trace_path(directory, board_stem)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=directory, prefix=".kilog-trace-", suffix=".tmp",
                                         delete=False) as stream:
            pending = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        while True:
            try:
                _publish_new(pending, output)
                return output
            except FileExistsError:
                # Another exporter may have taken this number while we wrote.
                output = next_trace_path(directory, board_stem)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
