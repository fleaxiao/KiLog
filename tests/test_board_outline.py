from kilog.board_outline import closed_outline_loops, ordered_board_loops


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
