import pytest

from kilog.selection import filter_board, parse_board
from kilog.selection_routing import connecting_edges


def footprint(uuid, x, y, net="1", layer="F.Cu", angle=0, local=(0, 0), shape="rect"):
    return f'''(footprint "part" (uuid "{uuid}") (at {x} {y} {angle})
      (pad "1" smd {shape} (at {local[0]} {local[1]} {angle}) (size 0.6 0.6)
        (layers "{layer}") (net {net})))'''


def track(uuid, start, end, layer="F.Cu", net="1"):
    return f'''(segment (uuid "{uuid}") (start {start[0]} {start[1]})
      (end {end[0]} {end[1]}) (width 0.1) (layer "{layer}") (net {net}))'''


def board(*items):
    return '(kicad_pcb (layers (0 "F.Cu" signal) (2 "In1.Cu" signal) (31 "B.Cu" signal))' + '\n'.join(items) + ')'


def routing(text):
    root = parse_board(text)
    return {node.field("uuid"): node for kind in ("segment", "arc", "via") for node in root.children(kind)}


def test_removes_same_net_branch_and_island():
    source = board(footprint("a", 0, 0), footprint("b", 10, 0), footprint("c", 5, 5),
                   track("left", (0, 0), (5, 0)), track("right", (5, 0), (10, 0)),
                   track("branch", (5, 0), (5, 5)), track("island", (30, 30), (40, 30)))
    assert set(routing(filter_board(source, {"a", "b"}))) == {"left", "right"}


def test_trims_existing_segment_at_interior_branch_point():
    source = board(footprint("a", 0, 0), footprint("b", 5, 5), footprint("c", 10, 0),
                   track("trunk", (0, 0), (10, 0)), track("branch", (5, 0), (5, 5)))
    kept = routing(filter_board(source, {"a", "b"}))
    assert set(kept) == {"trunk", "branch"}
    assert kept["trunk"].children("end")[0].values[1:] == ["5.000000", "0.000000"]


def test_unrouted_and_other_layer_crossings_do_not_connect():
    source = board(footprint("a", 0, 0), footprint("b", 10, 0, layer="B.Cu"),
                   track("front", (0, 0), (5, 0)), track("back", (5, 0), (10, 0), layer="B.Cu"))
    assert not routing(filter_board(source, {"a", "b"}))
    source = board(footprint("a", 0, 0), footprint("b", 10, 0),
                   track("left", (0, 0), (4, 0)), track("right", (6, 0), (10, 0)))
    assert not routing(filter_board(source, {"a", "b"}))


def test_via_connects_layers_and_unused_via_is_removed():
    via = '(via (uuid "via") (at 5 0) (size 0.5) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))'
    source = board(footprint("a", 0, 0), footprint("b", 10, 0, layer="B.Cu"),
                   track("front", (0, 0), (5, 0)), track("back", (5, 0), (10, 0), layer="B.Cu"), via,
                   via.replace('"via"', '"unused"').replace('(at 5 0)', '(at 30 30)'))
    assert set(routing(filter_board(source, {"a", "b"}))) == {"front", "back", "via"}
    # A blind via that ends on In1.Cu must not join the back layer.
    source = source.replace('(layers "F.Cu" "B.Cu") (net 1)', '(layers "F.Cu" "In1.Cu") (net 1)')
    assert not routing(filter_board(source, {"a", "b"}))


def test_rotated_pad_coordinates_and_pad_edge_contact():
    source = board(footprint("a", 10, 10, angle=90, local=(2, 0)), footprint("b", 20, 8),
                   track("connection", (10.25, 8), (19.75, 8)))
    assert set(routing(filter_board(source, {"a", "b"}))) == {"connection"}


def test_arc_retained_and_trimmed_without_changing_curve():
    arc = '(arc (uuid "curve") (start 0 0) (mid 5 5) (end 10 0) (width 0.1) (layer "F.Cu") (net 1))'
    source = board(footprint("a", 0, 0), footprint("b", 10, 0), arc)
    assert arc in filter_board(source, {"a", "b"})
    source = board(footprint("a", 0, 0), footprint("b", 5, 10), arc,
                   track("branch", (5, 5), (5, 10)))
    kept = routing(filter_board(source, {"a", "b"}))
    assert set(kept) == {"curve", "branch"}
    end = kept["curve"].children("end")[0].values[1:]
    assert [float(v) for v in end] == pytest.approx([5, 5], abs=1e-5)


def test_named_nets_supported_but_net_zero_is_not_connected():
    for net, expected in [('"电源"', {"connection"}), ('0', set())]:
        source = board(footprint("a", 0, 0, net=net), footprint("b", 10, 0, net=net),
                       track("connection", (0, 0), (10, 0), net=net))
        assert set(routing(filter_board(source, {"a", "b"}))) == expected


def test_unselected_pad_cannot_bridge_a_gap_in_retained_tracks():
    source = board(footprint("a", 0, 0), footprint("b", 10, 0), footprint("c", 5, 0),
                   track("left", (0, 0), (4.8, 0)), track("right", (5.2, 0), (10, 0)))
    assert not routing(filter_board(source, {"a", "b"}))


def test_only_same_component_terminals_do_not_preserve_routes():
    assert not connecting_edges([(0, 1, None)], {0: {"a"}, 1: {"a"}})


def test_parallel_routes_retained_but_attached_dead_loop_removed():
    edges = [(0, 1, None), (1, 2, None), (0, 3, None), (3, 2, None),
             (1, 4, None), (4, 5, None), (5, 1, None)]
    assert connecting_edges(edges, {0: {"a"}, 2: {"b"}}) == {0, 1, 2, 3}


def test_long_chains_do_not_exceed_recursion_limit():
    edges = [(i, i + 1, None) for i in range(3000)]
    assert len(connecting_edges(edges, {0: {"a"}, 3000: {"b"}})) == 3000


def test_via_in_pad_and_through_hole_pad_connect_layers():
    via = '(via (uuid "via") (at 0 0) (size 0.5) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))'
    source = board(footprint("a", 0, 0), footprint("b", 10, 0, layer="B.Cu"),
                   track("back", (0, 0), (10, 0), layer="B.Cu"), via)
    assert set(routing(filter_board(source, {"a", "b"}))) == {"back", "via"}
    source = board(footprint("a", 0, 0, layer="*.Cu").replace('smd rect', 'thru_hole circle'),
                   footprint("b", 10, 0, layer="B.Cu"), track("back", (0, 0), (10, 0), layer="B.Cu"))
    assert set(routing(filter_board(source, {"a", "b"}))) == {"back"}


def test_custom_pad_polygon_connects_track_outside_anchor_rectangle():
    custom = footprint("a", 0, 0, shape="custom")
    custom = custom.replace('(net 1)', '''(net 1) (options (anchor rect))
        (primitives (gr_poly (pts (xy -0.3 -0.3) (xy 2 -0.3) (xy 2 0.3) (xy -0.3 0.3))
          (width 0) (fill yes)))''')
    source = board(custom, footprint("b", 10, 0), track("connection", (1.8, 0), (10, 0)))
    assert set(routing(filter_board(source, {"a", "b"}))) == {"connection"}


def test_all_selected_preserves_all_real_connections_but_not_stubs():
    source = board(footprint("a", 0, 0), footprint("b", 10, 0), footprint("c", 5, 5),
                   track("left", (0, 0), (5, 0)), track("right", (5, 0), (10, 0)),
                   track("branch", (5, 0), (5, 5)), track("stub", (5, 0), (5, -5)))
    assert set(routing(filter_board(source, {"a", "b", "c"}))) == {"left", "right", "branch"}


def test_locked_default_connects_to_visible_selected_component():
    locked = footprint("a", 0, 0).replace('(uuid "a")', '(uuid "a") (locked yes)')
    source = board(locked, footprint("b", 10, 0), footprint("c", 5, 5),
                   track("connection", (0, 0), (10, 0)), track("branch", (5, 0), (5, 5)))
    assert set(routing(filter_board(source, {"b"}))) == {"connection"}
