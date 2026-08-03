from __future__ import annotations

from pathlib import Path

from kilog.model import BoardSnapshot, ItemState


def item(item_uuid: str, kind: str, **data) -> ItemState:
    return ItemState(item_uuid, kind, f"test.{kind}", data, raw_item=None)


def snapshot(*items: ItemState) -> BoardSnapshot:
    return BoardSnapshot.create("demo.kicad_pcb", {value.item_uuid: value for value in items})


class FakeAdapter:
    def __init__(self, directory: Path, snapshots: list[BoardSnapshot]):
        self.output_directory = directory
        self.snapshots = list(snapshots)
        self.current = self.snapshots[0]
        self.saved: list[Path] = []
        self.undo_strategy = "native"

    def snapshot(self) -> BoardSnapshot:
        if self.snapshots:
            self.current = self.snapshots.pop(0)
        return self.current

    def save_copy(self, path: Path) -> None:
        self.saved.append(path)
        path.write_text("(kicad_pcb)", encoding="utf-8")

    def undo_to(self, target: BoardSnapshot):
        self.current = target
        return target, self.undo_strategy
