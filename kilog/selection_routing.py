"""Retain copper paths between selected footprints, not entire net membership.

Geometry is in PCB-file millimetres. Junctions split traces before a block-cut
forest removes branches that cannot lie between two selected footprints.
"""
from collections import defaultdict, deque
from dataclasses import dataclass, field
import math
from uuid import uuid4

from shapely import STRtree
from shapely.affinity import rotate, translate
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import nearest_points, unary_union


TOLERANCE = 0.000002  # 2 nm: rounding only, never bridge a clearance gap.


def values(node, name):
    children = node.children(name)
    return children[0].values[1:] if children else []


def xy(node, name):
    coordinates = values(node, name)
    if len(coordinates) < 2:
        raise ValueError(f"Missing {name} coordinates in PCB {node.kind}.")
    return tuple(float(v) for v in coordinates[:2])


@dataclass
class Route:
    node: object
    line: object
    cuts: set = field(default_factory=lambda: {0.0, 1.0})
    circle: tuple | None = None

    def fraction(self, point):
        return self.line.project(point, normalized=True)

    def point(self, fraction):
        if self.circle:
            x, y, radius, start, sweep = self.circle
            angle = start + sweep * fraction
            return x + radius * math.cos(angle), y + radius * math.sin(angle)
        return tuple(self.line.interpolate(fraction, normalized=True).coords[0])


def route_geometry(node):
    start, end = xy(node, "start"), xy(node, "end")
    if node.kind == "segment":
        return Route(node, LineString([start, end]))
    mid = xy(node, "mid")
    ax, ay = start
    bx, by = mid
    cx, cy = end
    determinant = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(determinant) < 1e-15:
        raise ValueError("Cannot determine connectivity of a degenerate arc.")
    aa, bb, cc = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    x = (aa * (by - cy) + bb * (cy - ay) + cc * (ay - by)) / determinant
    y = (aa * (cx - bx) + bb * (ax - cx) + cc * (bx - ax)) / determinant
    radius = math.hypot(ax - x, ay - y)
    angle = math.atan2(ay - y, ax - x)
    sweep = (math.atan2(cy - y, cx - x) - angle) % math.tau
    if (math.atan2(by - y, bx - x) - angle) % math.tau > sweep:
        sweep -= math.tau
    step = 2 * math.acos(max(-1, 1 - TOLERANCE / max(radius, TOLERANCE)))
    count = max(2, math.ceil(abs(sweep) / max(step, 1e-5)))
    route = Route(node, None, circle=(x, y, radius, angle, sweep))
    points = [route.point(i / count) for i in range(count + 1)]
    points[0], points[-1] = start, end
    route.line = LineString(points)
    return route


def pad_shape(footprint, pad):
    fx, fy = xy(footprint, "at")
    px, py = xy(pad, "at")
    pose = values(footprint, "at")
    angle = math.radians(float(pose[2]) if len(pose) > 2 else 0)
    center = (fx + px * math.cos(angle) + py * math.sin(angle),
              fy - px * math.sin(angle) + py * math.cos(angle))
    width, height = xy(pad, "size")
    shape_name = pad.values[3]
    if shape_name == "circle":
        shape = Point(0, 0).buffer(width / 2, quad_segs=64)
    elif shape_name == "rect":
        shape = box(-width / 2, -height / 2, width / 2, height / 2)
    elif shape_name in {"oval", "roundrect"}:
        radius = (min(width, height) / 2 if shape_name == "oval" else
                  min(width, height) * float(pad.field("roundrect_rratio", "0.25")))
        dx, dy = max(0, width / 2 - radius), max(0, height / 2 - radius)
        if dx == dy == 0:
            base = Point(0, 0)
        elif dx == 0 or dy == 0:
            base = LineString([(-dx, -dy), (dx, dy)])
        else:
            base = box(-dx, -dy, dx, dy)
        shape = base.buffer(radius, quad_segs=64)
    elif shape_name == "trapezoid":
        dx, dy = (xy(pad, "rect_delta") if pad.children("rect_delta") else (0, 0))
        shape = Polygon([(-width / 2 - dy / 2, -height / 2 + dx / 2),
                         (width / 2 + dy / 2, -height / 2 - dx / 2),
                         (width / 2 - dy / 2, height / 2 + dx / 2),
                         (-width / 2 + dy / 2, height / 2 - dx / 2)])
    elif shape_name == "custom":
        options = pad.children("options")
        anchor = options[0].field("anchor", "rect") if options else "rect"
        parts = [Point(0, 0).buffer(width / 2, quad_segs=64) if anchor == "circle" else
                 box(-width / 2, -height / 2, width / 2, height / 2)]
        for primitives in pad.children("primitives"):
            for primitive in primitives.values[1:]:
                if not hasattr(primitive, "kind"):
                    continue
                thickness = float(primitive.field("width", "0"))
                if primitive.kind == "gr_poly":
                    points = primitive.children("pts")
                    if not points:
                        raise ValueError("Custom pad polygon has no points.")
                    part = Polygon([tuple(float(v) for v in p.values[1:3])
                                    for p in points[0].children("xy")])
                    if primitive.field("fill", "yes") == "no":
                        part = part.boundary
                elif primitive.kind in {"gr_line", "gr_arc"}:
                    if primitive.kind == "gr_line":
                        part = LineString([xy(primitive, "start"), xy(primitive, "end")])
                    else:
                        part = route_geometry(primitive).line
                elif primitive.kind == "gr_circle":
                    center_point, edge = xy(primitive, "center"), xy(primitive, "end")
                    part = Point(center_point).buffer(math.dist(center_point, edge), quad_segs=64)
                    if primitive.field("fill", "no") == "no":
                        part = part.boundary
                elif primitive.kind == "gr_rect":
                    a, b = xy(primitive, "start"), xy(primitive, "end")
                    part = box(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
                    if primitive.field("fill", "yes") == "no":
                        part = part.boundary
                else:
                    raise ValueError(f"Unsupported custom pad primitive: {primitive.kind}.")
                parts.append(part.buffer(thickness / 2) if thickness else part)
        shape = unary_union(parts)
    else:
        raise ValueError(f"Select routing does not yet support {shape_name} pads; no file was saved.")
    if pad.children("chamfer"):
        cut = min(width, height) * float(pad.field("chamfer_ratio", "0"))
        corners = {"top_left": (-1, -1), "top_right": (1, -1),
                   "bottom_left": (-1, 1), "bottom_right": (1, 1)}
        for corner in values(pad, "chamfer"):
            sx, sy = corners[corner]
            x, y = sx * width / 2, sy * height / 2
            shape = shape.difference(Polygon([(x, y), (x - sx * cut, y), (x, y - sy * cut)]))
    if pad.children("padstack"):
        raise ValueError("Select routing does not yet support per-layer custom padstacks; no file was saved.")
    pose = values(pad, "at")
    # Pad orientation in a PCB file is absolute; its position is footprint-local.
    rotation = float(pose[2]) if len(pose) > 2 else 0
    return translate(rotate(shape, -rotation, origin=(0, 0)), *center)


def connecting_edges(edges, terminals):
    """Union of simple paths between terminals on different footprints.

    Biconnected blocks preserve parallel routes, but not a dangling loop attached
    at a single junction. Iterative DFS also handles long tracks without recursion.
    """
    adjacency = defaultdict(list)
    for index, (a, b, _owner) in enumerate(edges):
        if a != b:
            adjacency[a].append((b, index))
            adjacency[b].append((a, index))
    discovered, low, parent = {}, {}, {}
    blocks, pending = [], []
    for root in list(adjacency):
        if root in discovered:
            continue
        discovered[root] = low[root] = len(discovered)
        stack = [(root, iter(adjacency[root]))]
        while stack:
            vertex, iterator = stack[-1]
            try:
                other, edge = next(iterator)
            except StopIteration:
                stack.pop()
                if vertex in parent:
                    ancestor, entering = parent[vertex]
                    low[ancestor] = min(low[ancestor], low[vertex])
                    if low[vertex] >= discovered[ancestor]:
                        block = []
                        while pending:
                            item = pending.pop()
                            block.append(item)
                            if item == entering:
                                break
                        blocks.append(block)
                continue
            if vertex in parent and edge == parent[vertex][1]:
                continue
            if other not in discovered:
                pending.append(edge)
                parent[other] = (vertex, edge)
                discovered[other] = low[other] = len(discovered)
                stack.append((other, iter(adjacency[other])))
            elif discovered[other] < discovered[vertex]:
                pending.append(edge)
                low[vertex] = min(low[vertex], discovered[other])

    tree = defaultdict(set)
    for index, block in enumerate(blocks):
        block_node = ("block", index)
        for edge in block:
            for vertex in edges[edge][:2]:
                tree[block_node].add(vertex)
                tree[vertex].add(block_node)
    visited, retained = set(), set()
    for root in list(tree):
        if root in visited:
            continue
        component, todo, owners = set(), [root], set()
        while todo:
            node = todo.pop()
            if node in component:
                continue
            component.add(node)
            owners.update(terminals.get(node, set()))
            todo.extend(tree[node] - component)
        visited.update(component)
        if len(owners) < 2:
            continue
        degree = {node: len(tree[node]) for node in component}
        leaves = deque(node for node in component if degree[node] <= 1 and not terminals.get(node))
        while leaves:
            node = leaves.popleft()
            if node not in component:
                continue
            component.remove(node)
            for neighbor in tree[node] & component:
                degree[neighbor] -= 1
                if degree[neighbor] <= 1 and not terminals.get(neighbor):
                    leaves.append(neighbor)
        for node in component:
            if isinstance(node, tuple) and node[0] == "block":
                retained.update(blocks[node[1]])
    return retained


def routing_replacements(board, text, selected):
    """Map each routing node's source offset to retained (possibly split) text."""
    routes = [n for n in board.values if hasattr(n, "kind") and n.kind in {"segment", "arc", "via"}]
    replacements = {n.start: "" for n in routes}
    pads_by_net = defaultdict(list)
    for fp in board.children("footprint"):
        if fp.field("uuid") in selected:
            for pad in fp.children("pad"):
                net = pad.field("net")
                if net not in {"", "0"}:
                    pads_by_net[net].append((fp, pad))
    copper = [n.values[1] for layer_list in board.children("layers")
              for n in layer_list.values[1:] if hasattr(n, "values")
              and len(n.values) > 1 and str(n.values[1]).endswith(".Cu")]
    if not copper:
        copper = ["F.Cu", "B.Cu"]

    def layers(node, via=False):
        names = values(node, "layers")
        if "*.Cu" in names:
            return set(copper)
        if via and len(names) == 2 and all(name in copper for name in names):
            a, b = sorted(copper.index(name) for name in names)
            return set(copper[a:b + 1])
        return set(names) & set(copper)

    for net, pads in pads_by_net.items():
        if len({fp.field("uuid") for fp, _pad in pads}) < 2:
            continue
        tracks = [route_geometry(n) for n in routes if n.field("net") == net and n.kind != "via"]
        if not tracks:
            continue
        anchors = []
        for fp, pad in pads:
            if pad.values[2] == "np_thru_hole":
                continue
            anchors.append((pad_shape(fp, pad), layers(pad), fp.field("uuid"), None))
        for via in routes:
            if via.kind == "via" and via.field("net") == net:
                anchors.append((Point(xy(via, "at")).buffer(float(via.field("size")) / 2, quad_segs=64),
                                layers(via, via=True), None, via))
        edges, terminals, links = [], {}, []
        identities = {}

        def vertex(key):
            return identities.setdefault(key, len(identities))

        def contact(index, point):
            fraction = round(tracks[index].fraction(point), 12)
            tracks[index].cuts.add(fraction)
            return vertex((index, fraction))

        # Spatial queries avoid comparing every track with every other track.
        tree = STRtree([track.line for track in tracks])
        widths = [float(track.node.field("width", "0")) / 2 for track in tracks]
        maximum_width = max(widths, default=0)
        for i, track in enumerate(tracks):
            for j in tree.query(track.line, predicate="dwithin", distance=widths[i] + maximum_width + TOLERANCE):
                j = int(j)
                if j <= i or track.node.field("layer") != tracks[j].node.field("layer"):
                    continue
                other = tracks[j].line
                if track.line.distance(other) > widths[i] + widths[j] + TOLERANCE:
                    continue
                intersection = track.line.intersection(other)
                if intersection.is_empty:
                    pairs = [nearest_points(track.line, other)]
                else:
                    geometries = list(intersection.geoms) if hasattr(intersection, "geoms") else [intersection]
                    points = [Point(coordinate) for geometry in geometries for coordinate in geometry.coords]
                    pairs = [(point, point) for point in points]
                for a, b in pairs:
                    links.append((contact(i, a), contact(j, b), None))
        for anchor_index, (shape, copper_layers, owner, via) in enumerate(anchors):
            anchor = vertex(("anchor", anchor_index))
            if owner:
                terminals[anchor] = {owner}
            for i in tree.query(shape, predicate="dwithin", distance=maximum_width + TOLERANCE):
                i = int(i)
                if tracks[i].node.field("layer") not in copper_layers:
                    continue
                if shape.distance(tracks[i].line) <= widths[i] + TOLERANCE:
                    touching = tracks[i].line.intersection(shape.buffer(widths[i] + TOLERANCE))
                    point = (nearest_points(shape.centroid, touching)[1] if not touching.is_empty
                             else nearest_points(shape, tracks[i].line)[1])
                    links.append((anchor, contact(i, point), via))
        # Copper touching another pad/via can carry a path even without an
        # explicit intervening segment (for example via-in-pad).
        anchor_tree = STRtree([anchor[0] for anchor in anchors])
        for i, (shape, copper_layers, _owner, via) in enumerate(anchors):
            for j in anchor_tree.query(shape, predicate="dwithin", distance=TOLERANCE):
                j = int(j)
                if j <= i or not copper_layers & anchors[j][1]:
                    continue
                junction = vertex(("anchor-contact", i, j))
                links.append((vertex(("anchor", i)), junction, via))
                links.append((vertex(("anchor", j)), junction, anchors[j][3]))
        for i, track in enumerate(tracks):
            cuts = sorted(track.cuts)
            for a, b in zip(cuts, cuts[1:]):
                edges.append((vertex((i, a)), vertex((i, b)), (i, a, b)))
        edges.extend(links)
        keep = connecting_edges(edges, terminals)
        intervals = defaultdict(list)
        for index in keep:
            owner = edges[index][2]
            if isinstance(owner, tuple):
                i, a, b = owner
                intervals[i].append((a, b))
            elif owner is not None:
                replacements[owner.start] = text[owner.start:owner.end]
        for i, ranges in intervals.items():
            merged = []
            for a, b in sorted(ranges):
                if merged and abs(merged[-1][1] - a) < 1e-10:
                    merged[-1] = (merged[-1][0], b)
                else:
                    merged.append((a, b))
            track = tracks[i]
            pieces = []
            for piece_index, (a, b) in enumerate(merged):
                if a == 0 and b == 1:
                    pieces.append(text[track.node.start:track.node.end])
                    continue
                edits = []
                for name, fraction in (("start", a), ("end", b), ("mid", (a + b) / 2)):
                    for node in track.node.children(name):
                        x, y = track.point(fraction)
                        edits.append((node.start, node.end, f"({name} {x:.6f} {y:.6f})"))
                if piece_index:
                    for node in track.node.children("uuid"):
                        edits.append((node.start, node.end, f'(uuid "{uuid4()}")'))
                piece = text[track.node.start:track.node.end]
                for start, end, replacement in sorted(edits, reverse=True):
                    piece = piece[:start - track.node.start] + replacement + piece[end - track.node.start:]
                pieces.append(piece)
            replacements[track.node.start] = "\n".join(pieces)
    return replacements
