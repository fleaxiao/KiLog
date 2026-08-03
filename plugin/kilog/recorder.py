from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Protocol
from uuid import uuid4

from .diffing import build_event
from .model import BoardSnapshot
from .storage import normalize_stem, next_counter, numbered_path, write_json_atomic


class RecorderError(RuntimeError):
    pass


class BoardAdapter(Protocol):
    @property
    def output_directory(self) -> Path: ...

    def snapshot(self) -> BoardSnapshot: ...

    def save_copy(self, path: Path) -> None: ...

    def undo_to(self, target: BoardSnapshot) -> tuple[BoardSnapshot, str]: ...


@dataclass(frozen=True)
class RecorderConfig:
    pcb_stem: str = "ref"
    log_stem: str = "log"
    settle_seconds: float = 0.45


@dataclass(frozen=True)
class HistoryEntry:
    before: BoardSnapshot
    path: Path


class Recorder:
    def __init__(self, adapter: BoardAdapter):
        self.adapter = adapter
        self.recording = False
        self.session_uuid = ""
        self.config = RecorderConfig()
        self.baseline: BoardSnapshot | None = None
        self.pending: BoardSnapshot | None = None
        self.pending_since = 0.0
        self.history: list[HistoryEntry] = []
        self.log_counter = 1
        self.note_counter = 1

    @property
    def event_count(self) -> int:
        return len(self.history)

    def start(self, config: RecorderConfig) -> BoardSnapshot:
        if self.recording:
            raise RecorderError("记录已经在运行")
        pcb_stem = normalize_stem(config.pcb_stem, ".kicad_pcb")
        log_stem = normalize_stem(config.log_stem, ".json")
        self.config = RecorderConfig(pcb_stem, log_stem, config.settle_seconds)
        output = self.adapter.output_directory
        output.mkdir(parents=True, exist_ok=True)
        self.log_counter = next_counter(output, log_stem, ".json")
        self.note_counter = next_counter(output, pcb_stem, ".kicad_pcb")
        self.baseline = self.adapter.snapshot()
        self.pending = None
        self.history.clear()
        self.session_uuid = str(uuid4())
        self.recording = True
        return self.baseline

    def poll(self, now: float | None = None) -> dict | None:
        if not self.recording or self.baseline is None:
            return None
        current = self.adapter.snapshot()
        clock = time.monotonic() if now is None else now
        if current.fingerprint == self.baseline.fingerprint:
            self.pending = None
            return None
        if self.pending is None or current.fingerprint != self.pending.fingerprint:
            self.pending = current
            self.pending_since = clock
            return None
        if clock - self.pending_since < self.config.settle_seconds:
            return None
        return self._commit(current)

    def flush(self) -> dict | None:
        if not self.recording or self.baseline is None:
            return None
        current = self.adapter.snapshot()
        if current.fingerprint == self.baseline.fingerprint:
            self.pending = None
            return None
        return self._commit(current)

    def _commit(self, current: BoardSnapshot) -> dict | None:
        assert self.baseline is not None
        event = build_event(
            self.baseline,
            current,
            sequence=self.log_counter,
            session_uuid=self.session_uuid,
        )
        self.pending = None
        if event is None:
            self.baseline = current
            return None
        path = numbered_path(
            self.adapter.output_directory, self.config.log_stem, self.log_counter, ".json"
        )
        write_json_atomic(path, event)
        self.history.append(HistoryEntry(self.baseline, path))
        self.baseline = current
        self.log_counter += 1
        return event

    def note(self) -> Path:
        if not self.recording:
            raise RecorderError("请先点击 Start")
        self.flush()
        path = numbered_path(
            self.adapter.output_directory,
            self.config.pcb_stem,
            self.note_counter,
            ".kicad_pcb",
        )
        self.adapter.save_copy(path)
        self.note_counter += 1
        return path

    def undo(self) -> tuple[Path, str]:
        if not self.recording:
            raise RecorderError("请先点击 Start")
        # A user can click undo before the debounce window writes the newest operation.  Flush it
        # first so the PCB action and the JSON file always refer to the same history entry.
        self.flush()
        self.pending = None
        if not self.history:
            raise RecorderError("没有可撤销的已记录操作")
        entry = self.history[-1]
        restored, strategy = self.adapter.undo_to(entry.before)
        if restored.fingerprint != entry.before.fingerprint:
            raise RecorderError("KiCad 状态未能恢复，日志文件保持不变")
        try:
            entry.path.unlink()
        except OSError as exc:
            self.history.pop()
            self.baseline = restored
            self.log_counter -= 1
            raise RecorderError(f"PCB 已撤销，但日志删除失败：{exc}") from exc
        self.history.pop()
        self.baseline = restored
        self.log_counter -= 1
        return entry.path, strategy

    def end(self) -> dict | None:
        if not self.recording:
            return None
        event = self.flush()
        self.recording = False
        self.pending = None
        return event
