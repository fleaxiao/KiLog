from kilog.board_outline import circle_inside_board, closed_outline_loops, ordered_board_loops


def test_unordered_segments_are_joined_into_closed_loop():
    segments = [
        ((10, 0), (10, 10)),
        ((0, 10), (0, 0)),
        ((10, 10), (0, 10)),
        ((0, 0), (10, 0)),
    ]

    loops = closed_outline_loops(segments)

    assert len(loops) == 1
    assert len(loops[0]) == 4


def test_largest_outline_is_returned_before_cutout():
    outer = [
        ((0, 0), (20, 0)),
        ((20, 0), (20, 20)),
        ((20, 20), (0, 20)),
        ((0, 20), (0, 0)),
    ]
    hole = [
        ((5, 5), (10, 5)),
        ((10, 5), (10, 10)),
        ((10, 10), (5, 10)),
        ((5, 10), (5, 5)),
    ]

    loops = ordered_board_loops([*hole, *outer])

    assert loops[0][0] in {(0, 0), (20, 0), (20, 20), (0, 20)}
    assert len(loops) == 2


def test_open_outline_is_rejected():
    assert closed_outline_loops([((0, 0), (10, 0)), ((10, 0), (10, 10))]) == []


def test_circle_must_clear_outer_edge_and_internal_cutout():
    loops = [
        [(0, 0), (20, 0), (20, 20), (0, 20)],
        [(8, 8), (12, 8), (12, 12), (8, 12)],
    ]

    assert circle_inside_board((4, 4), 2, loops)
    assert not circle_inside_board((1, 4), 2, loops)
    assert not circle_inside_board((7, 10), 2, loops)
