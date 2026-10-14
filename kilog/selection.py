"""Lossless filtering of an IPC-saved board copy; never edit the live board."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tempfile

from .selection_routing import routing_replacements
from .trace_output import trace_directory, write_trace


@dataclass
class Node:
    start: int
    end: int
    values: list

    @property
    def kind(self):
        return self.values[0] if self.values else ""

    def children(self, kind):
        return [v for v in self.values if isinstance(v, Node) and v.kind == kind]

    def field(self, kind, default=""):
        nodes = self.children(kind)
        return nodes[0].values[1] if nodes and len(nodes[0].values) > 1 else default


def parse_board(text: str) -> Node:
    stack = []
    roots = []
    for match in re.finditer(r'"(?:\\.|[^"\\])*"|[()]|[^\s()"]+', text):
        token = match.group()
        if token == "(":
            node = Node(match.start(), 0, [])
            (stack[-1].values if stack else roots).append(node)
            stack.append(node)
        elif token == ")":
            if not stack:
                raise ValueError("Invalid PCB parentheses.")
            stack.pop().end = match.end()
        else:
            if not stack:
                raise ValueError("Invalid PCB content.")
            if token.startswith('"'):
                token = re.sub(r'\\(["\\])', r'\1', token[1:-1])
            stack[-1].values.append(token)
    if stack or len(roots) != 1 or roots[0].kind != "kicad_pcb":
        raise ValueError("Invalid KiCad PCB file.")
    return roots[0]


def filter_board(text: str, selected: set[str]) -> str:
    """Keep selected UUIDs and direct physical routes between selected footprints.

    Copper zones (including cached fills) are removed. Rule-area keepouts,
    board settings, outlines and board-level annotations remain intact. A path
    between two footprints cannot pass through a third footprint's copper pad.
    """
    board = parse_board(text)
    footprints = board.children("footprint")
    # Read locks from the saved live snapshot, not a potentially stale UI list.
    selected = selected | {
        fp.field("uuid") for fp in footprints
        if "locked" in fp.values or fp.field("locked") in {"yes", "true"}
    }
    if not selected:
        raise ValueError("Select at least one component first.")
    found = {fp.field("uuid") for fp in footprints}
    if not selected <= found:
        raise ValueError("Selected components changed. Refresh the component list.")
    routing = routing_replacements(board, text, selected)
    removed = []
    removed_ids = set()
    candidates = [node for node in board.values if isinstance(node, Node)]
    for footprint in footprints:
        if footprint.field("uuid") in selected:
            candidates.extend(footprint.children("zone"))
    for node in candidates:
        remove = (
            (node.kind == "footprint" and node.field("uuid") not in selected)
            or (node.kind in {"segment", "arc", "via"} and not routing[node.start])
            or (node.kind == "zone" and not node.children("keepout"))
        )
        if remove:
            removed.append(node)
            # Groups must not retain references to deleted items or pads.
            def collect_ids(item):
                if item.kind == "uuid":
                    removed_ids.add(item.values[1])
                for child in item.values:
                    if isinstance(child, Node):
                        collect_ids(child)
            collect_ids(node)
    # Strip affected group definitions rather than leaving dangling members.
    pending = board.children("group")
    while pending:
        affected = [g for g in pending if any(
            member in removed_ids for members in g.children("members")
            for member in members.values[1:] if isinstance(member, str)
        )]
        if not affected:
            break
        for group in affected:
            removed.append(group)
            removed_ids.add(group.field("uuid", group.field("id")))
            pending.remove(group)
    edits = [(node.start, node.end, "") for node in removed]
    for node in candidates:
        if node.start in routing and routing[node.start]:
            edits.append((node.start, node.end, routing[node.start]))
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def export_selection(adapter, selected: set[str], output_directory: Path | None = None) -> Path:
    directory = trace_directory(adapter.output_directory if output_directory is None else output_directory)
    stem = adapter.board_path.stem if adapter.board_path else "board"
    # Filter only the IPC-saved live snapshot, then publish a numbered copy.
    with tempfile.TemporaryDirectory(prefix="kilog-trace-") as temporary:
        source = Path(temporary) / "source.kicad_pcb"
        adapter.save_copy(source)
        result = filter_board(source.read_text(encoding="utf-8"), selected)
    return write_trace(directory, stem, result)
