"""Keep a full-board ground plane with rectangular secondary-ground insets."""
from __future__ import annotations


def ground_regions(loops, sites, bounds_by_net=None, margin=500_000, primary=None):
    from shapely.geometry import Point, Polygon, box
    from shapely.ops import unary_union

    board = Polygon(loops[0])
    for hole in loops[1:]:
        board = board.difference(Polygon(hole))
    names = sorted(set(sites.values()) | set(bounds_by_net or {}))
    if not names:
        return {}
    primary = primary or ("gnd" if "gnd" in names else names[0])
    insets = {}
    for name in names:
        if name == primary:
            continue
        bounds = (bounds_by_net or {}).get(name)
        if bounds is None:
            points = [point for point, net in sites.items() if net == name]
            bounds = (min(p[0] for p in points), min(p[1] for p in points),
                      max(p[0] for p in points), max(p[1] for p in points))
        left, top, right, bottom = bounds
        inset = box(left - margin, top - margin, right + margin, bottom + margin).intersection(board)
        for other_name, other in insets.items():
            if inset.intersection(other).area > 0:
                raise ValueError(f"Ground rectangles overlap: {other_name}, {name}.")
        for point, net in sites.items():
            if net != name and inset.covers(Point(point)):
                raise ValueError(f"Ground rectangle {name} contains a {net} pad.")
        insets[name] = inset

    def polygons(geometry):
        if geometry.geom_type == "Polygon":
            if geometry.area > 0:
                yield geometry
        elif hasattr(geometry, "geoms"):
            for part in geometry.geoms:
                yield from polygons(part)

    regions = {primary: board.difference(unary_union(list(insets.values()))), **insets}
    return {
        name: [[list(part.exterior.coords)[:-1],
                *[list(ring.coords)[:-1] for ring in part.interiors]]
               for part in polygons(geometry.simplify(0))]
        for name, geometry in regions.items()
    }
