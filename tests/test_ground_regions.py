import pytest
from shapely.geometry import Polygon
from shapely.ops import unary_union

from kilog.ground_regions import ground_regions
from kilog.kicad_adapter import KiCadBoardAdapter
from kilog.recorder import RecorderError
from kipy.board_types import FootprintInstance, Pad, Net, BoardRectangle, BoardSegment, BoardLayer, BoardArc, Track, ArcTrack, Via
from kipy.geometry import Vector2
from tests.test_kicad_adapter import FillBoard, edge_segment, with_id


def test_partition_preserves_concave_board_holes_and_disconnected_regions():
    outer = [(0, 0), (20, 0), (20, 20), (12, 20), (12, 8), (8, 8), (8, 20), (0, 20)]
    hole = [(2, 2), (4, 2), (4, 4), (2, 4)]
    regions = ground_regions([outer, hole], {(3, 10): "gnd", (17, 10): "gnd2", (10, 3): "gnd3"}, margin=1)
    shapes = [unary_union([Polygon(r[0], r[1:]) for r in parts]) for parts in regions.values()]
    assert unary_union(shapes).symmetric_difference(Polygon(outer, [hole])).area < 1e-8
    for i, shape in enumerate(shapes):
        assert shape.is_valid
        for other in shapes[i + 1:]:
            assert shape.intersection(other).area < 1e-8


def make_board(include_second=True):
    components = []
    for name, x in [("GND", 3_000_000), ("GND2", 17_000_000)]:
        if name == "GND2" and not include_second:
            continue
        component = with_id(FootprintInstance(), f"ground-pads-{name}")
        component.position = Vector2.from_xy(x, 5_000_000)
        pad = Pad()
        pad.position = Vector2.from_xy(x, 5_000_000)
        pad.net = Net(name=name)
        component.definition.add_item(pad)
        components.append(component)
    points = [(0, 0), (20_000_000, 0), (20_000_000, 10_000_000), (0, 10_000_000)]
    board = FillBoard([*components, *[
        edge_segment(f"edge-{i}", a, b)
        for i, (a, b) in enumerate(zip(points, points[1:] + points[:1]))
    ]])
    board.get_nets = lambda: [Net(name="GND"), Net(name="GND2"), Net(name="VCC")]
    return board


def test_multiple_ground_fill_creates_separate_regions_on_both_layers():
    board = make_board()
    adapter = KiCadBoardAdapter(object(), board)
    assert adapter.fill_board_copper(None, ("F.Cu", "B.Cu")) == 4
    assert board.commit_count == 1
    for zone in board.created:
        xs = [node.point.x for node in zone.outline.outline.nodes]
        if zone.net.name == "GND":
            assert min(xs) == 0 and max(xs) == 20_000_000
            assert len(zone.outline.holes) == 1
        else:
            assert min(xs) == 16_500_000 and max(xs) == 17_500_000
            assert len(xs) == 4
    assert not board.refilled


def test_missing_ground_location_does_not_modify_board():
    board = make_board(include_second=False)
    with pytest.raises(RecorderError, match="gnd2"):
        KiCadBoardAdapter(object(), board).fill_board_copper("GND", ("F.Cu",))
    assert board.commit_count == 0
    assert not board.created and not board.removed


def test_same_net_cells_merge_and_keep_each_ground_pad_in_its_region():
    from shapely.geometry import Point

    sites = {(2, 2): "gnd", (2, 8): "gnd", (8, 5): "gnd2"}
    result = ground_regions([[(0, 0), (10, 0), (10, 10), (0, 10)]], sites, margin=1)
    assert len(result["gnd"]) == 1
    for point, name in sites.items():
        assert any(Polygon(r[0], r[1:]).covers(Point(point)) for r in result[name])


def test_coincident_different_ground_pads_do_not_modify_board():
    board = make_board()
    footprint = board.items[0]
    pad = Pad()
    pad.position = Vector2.from_xy(3_000_000, 5_000_000)
    pad.net = Net(name="GND2")
    footprint.definition.add_item(pad)
    with pytest.raises(RecorderError, match="coincident"):
        KiCadBoardAdapter(object(), board).fill_board_copper("GND", ("F.Cu",))
    assert board.commit_count == 0
    assert not board.created and not board.removed


def test_secondary_rectangle_uses_pad_extents_and_square_corners():
    result = ground_regions(
        [[(0, 0), (20, 0), (20, 20), (0, 20)]],
        {(2, 2): "gnd", (12, 10): "gnd2", (14, 14): "gnd2"},
        {"gnd2": (11, 9, 15, 15)}, margin=1,
    )
    assert Polygon(result["gnd2"][0][0]).bounds == (10, 8, 16, 16)
    assert len(result["gnd2"][0][0]) == 4
    assert len(result["gnd"][0]) == 2
    for ring in result["gnd2"][0]:
        for a, b in zip(ring, ring[1:] + ring[:1]):
            assert a[0] == b[0] or a[1] == b[1]


def test_conflicting_rectangle_is_rejected():
    with pytest.raises(ValueError, match="contains"):
        ground_regions([[(0, 0), (20, 0), (20, 20), (0, 20)]],
                       {(10, 10): "gnd", (8, 8): "gnd2", (12, 12): "gnd2"}, margin=1)


def test_automatic_ground_selection_without_plain_gnd():
    board = make_board()
    board.get_nets = lambda: [Net(name="GND2"), Net(name="VCC")]
    assert KiCadBoardAdapter(object(), board).fill_board_copper(None, ("F.Cu",)) == 1
    assert board.created[0].net.name == "GND2"


def test_no_ground_net_does_not_modify_board():
    board = make_board()
    board.get_nets = lambda: [Net(name="VCC")]
    with pytest.raises(RecorderError, match="requires a ground net"):
        KiCadBoardAdapter(object(), board).fill_board_copper(None, ("F.Cu",))
    assert not board.created and not board.removed


def test_selected_net_is_the_full_board_base():
    board = make_board()
    assert KiCadBoardAdapter(object(), board).fill_board_copper("GND2", ("F.Cu",)) == 2
    base = next(zone for zone in board.created if zone.net.name == "GND2")
    inset = next(zone for zone in board.created if zone.net.name == "GND")
    assert {node.point.x for node in base.outline.outline.nodes} == {0, 20_000_000}
    assert len(base.outline.holes) == 1
    assert {node.point.x for node in inset.outline.outline.nodes} == {2_500_000, 3_500_000}


def test_base_plane_does_not_require_a_pad():
    result = ground_regions(
        [[(0, 0), (20, 0), (20, 20), (0, 20)]],
        {(12, 10): "gnd2"}, margin=1, primary="gnd",
    )
    assert set(result) == {"gnd", "gnd2"}
    assert len(result["gnd"][0]) == 2


def test_secondary_region_covers_entire_connected_components():
    board = make_board()
    component = board.items[1]
    signal_pad = Pad()
    signal_pad.net = Net(name="VCC")
    signal_pad.position = Vector2.from_xy(13_000_000, 5_000_000)
    signal_pad.padstack.copper_layers[0].size = Vector2.from_xy(2_000_000, 2_000_000)
    component.definition.add_item(signal_pad)
    courtyard = BoardRectangle()
    courtyard.layer = BoardLayer.BL_F_CrtYd
    courtyard.top_left = Vector2.from_xy(11_000_000, 2_000_000)
    courtyard.bottom_right = Vector2.from_xy(18_000_000, 8_000_000)
    component.definition.add_item(courtyard)
    KiCadBoardAdapter(object(), board).fill_board_copper("GND", ("F.Cu",))
    zone = next(zone for zone in board.created if zone.net.name == "GND2")
    points = [(node.point.x, node.point.y) for node in zone.outline.outline.nodes]
    assert Polygon(points).bounds == (10_500_000, 1_500_000, 18_500_000, 8_500_000)


def test_magnetic_void_preserves_concave_segment_outline_and_ignores_inner_marking():
    component = FootprintInstance()
    component.reference_field.text.value = "L1"
    component.position = Vector2.from_xy(3_000_000, 3_000_000)
    points = [(0, 0), (8_000_000, 0), (8_000_000, 4_000_000),
              (5_000_000, 4_000_000), (5_000_000, 8_000_000), (0, 8_000_000)]
    for a, b in zip(points, points[1:] + points[:1]):
        line = BoardSegment()
        line.layer = BoardLayer.BL_F_SilkS
        line.start, line.end = Vector2.from_xy(*a), Vector2.from_xy(*b)
        component.definition.add_item(line)
    marking = BoardRectangle()
    marking.layer = BoardLayer.BL_F_SilkS
    marking.top_left = Vector2.from_xy(2_000_000, 2_000_000)
    marking.bottom_right = Vector2.from_xy(4_000_000, 4_000_000)
    component.definition.add_item(marking)
    # An unrelated open silk line must not invalidate the closed contour.
    line = BoardSegment()
    line.layer = BoardLayer.BL_F_SilkS
    line.start, line.end = Vector2.from_xy(9_000_000, 0), Vector2.from_xy(10_000_000, 0)
    component.definition.add_item(line)
    loop = KiCadBoardAdapter._magnetic_body_loop(component)
    assert Polygon(loop).equals(Polygon(points))


def test_magnetic_void_accepts_closed_arc_and_line_outline():
    component = FootprintInstance()
    component.position = Vector2.from_xy(0, 1_000_000)
    arc = BoardArc()
    arc.layer = BoardLayer.BL_F_Fab
    arc.start = Vector2.from_xy(-2_000_000, 0)
    arc.mid = Vector2.from_xy(0, 2_000_000)
    arc.end = Vector2.from_xy(2_000_000, 0)
    component.definition.add_item(arc)
    line = BoardSegment()
    line.layer = BoardLayer.BL_F_Fab
    line.start, line.end = arc.end, arc.start
    component.definition.add_item(line)
    loop = KiCadBoardAdapter._magnetic_body_loop(component)
    assert len(loop) > 4
    assert Polygon(loop).area == pytest.approx(2 * 3.141592653589793 * 1e12, rel=0.01)


def test_secondary_region_includes_tracks_width_and_via_copper_on_other_layers():
    board = make_board()
    track = with_id(Track(), "ground-track")
    track.net = Net(name="GND2")
    track.layer = BoardLayer.BL_B_Cu
    track.start = Vector2.from_xy(17_000_000, 5_000_000)
    track.end = Vector2.from_xy(12_000_000, 2_000_000)
    track.width = 800_000
    via = with_id(Via(), "ground-via")
    via.net = Net(name="GND2")
    via.position = Vector2.from_xy(18_000_000, 7_000_000)
    via.diameter = 1_000_000
    board.items.extend([track, via])
    KiCadBoardAdapter(object(), board).fill_board_copper("GND", ("F.Cu",))
    zone = next(zone for zone in board.created if zone.net.name == "GND2")
    assert Polygon([(n.point.x, n.point.y) for n in zone.outline.outline.nodes]).bounds == (
        11_100_000, 1_100_000, 19_000_000, 8_000_000)


def test_secondary_region_includes_arc_extrema_without_ground_pads():
    board = make_board(include_second=False)
    arc = with_id(ArcTrack(), "ground-arc")
    arc.net = Net(name="GND2")
    arc.start = Vector2.from_xy(12_000_000, 4_000_000)
    arc.mid = Vector2.from_xy(15_000_000, 7_000_000)
    arc.end = Vector2.from_xy(18_000_000, 4_000_000)
    arc.width = 600_000
    board.items.append(arc)
    KiCadBoardAdapter(object(), board).fill_board_copper("GND", ("F.Cu",))
    zone = next(zone for zone in board.created if zone.net.name == "GND2")
    assert Polygon([(n.point.x, n.point.y) for n in zone.outline.outline.nodes]).bounds == (
        11_200_000, 3_200_000, 18_800_000, 7_800_000)


@pytest.mark.parametrize("reference", ["T1", "L1"])
def test_transformer_does_not_expand_ground_region_to_primary_side(reference):
    board = make_board()
    transformer = board.items[1]
    transformer.reference_field.text.value = reference
    transformer.proto.description_field.text.text.text = "Coupled inductor with ferrite core"
    primary = Pad()
    primary.net = Net(name="SW")
    primary.position = Vector2.from_xy(9_000_000, 5_000_000)
    primary.padstack.copper_layers[0].size = Vector2.from_xy(1_000_000, 1_000_000)
    transformer.definition.add_item(primary)
    courtyard = BoardRectangle()
    courtyard.layer = BoardLayer.BL_F_CrtYd
    courtyard.top_left = Vector2.from_xy(8_000_000, 3_000_000)
    courtyard.bottom_right = Vector2.from_xy(18_000_000, 7_000_000)
    transformer.definition.add_item(courtyard)
    KiCadBoardAdapter(object(), board).fill_board_copper("GND", ("F.Cu",))
    zone = next(zone for zone in board.created if zone.net.name == "GND2")
    assert Polygon([(n.point.x, n.point.y) for n in zone.outline.outline.nodes]).bounds == (
        16_500_000, 4_500_000, 17_500_000, 5_500_000)


def test_transformer_other_pads_and_partner_trace_do_not_expand_ground_region():
    board = make_board()
    transformer = board.items[1]
    transformer.reference_field.text.value = "T1"
    for name, x, y in [("RECT", 17_000_000, 7_000_000),
                       ("SW", 9_000_000, 5_000_000), ("VIN", 9_000_000, 7_000_000)]:
        pad = Pad()
        pad.net = Net(name=name)
        pad.position = Vector2.from_xy(x, y)
        transformer.definition.add_item(pad)
    track = with_id(Track(), "secondary-winding-track")
    track.net = Net(name="RECT")
    track.start = Vector2.from_xy(17_000_000, 7_000_000)
    track.end = Vector2.from_xy(14_000_000, 8_000_000)
    track.width = 400_000
    board.items.append(track)
    KiCadBoardAdapter(object(), board).fill_board_copper("GND", ("F.Cu",))
    zone = next(zone for zone in board.created if zone.net.name == "GND2")
    assert Polygon([(n.point.x, n.point.y) for n in zone.outline.outline.nodes]).bounds == (
        16_500_000, 4_500_000, 17_500_000, 5_500_000)
    assert track.net.name == "RECT"


def test_four_terminal_l1_is_recognized_without_library_description():
    component = FootprintInstance()
    component.reference_field.text.value = "L1"
    for number in ("1", "2", "3", "4"):
        pad = Pad()
        pad.number = number
        component.definition.add_item(pad)
    assert KiCadBoardAdapter._is_transformer_or_coupled_inductor(component)
    ordinary = FootprintInstance()
    ordinary.reference_field.text.value = "L2"
    for number in ("1", "1", "2", "2"):
        pad = Pad()
        pad.number = number
        ordinary.definition.add_item(pad)
    assert not KiCadBoardAdapter._is_transformer_or_coupled_inductor(ordinary)
