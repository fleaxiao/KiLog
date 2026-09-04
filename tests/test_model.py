from kilog.model import BoardSnapshot, ItemState, snapshots_match_restored_state


def state(item_uuid: str, kind: str, **data) -> ItemState:
    return ItemState(item_uuid, kind, f"test.{kind}", data)


def snapshot(*items: ItemState) -> BoardSnapshot:
    return BoardSnapshot.create("demo.kicad_pcb", {item.item_uuid: item for item in items})


def test_restored_footprint_ignores_repacked_library_definition():
    target = snapshot(
        state(
            "fp-1",
            "footprint",
            id={"value": "fp-1"},
            position={"x_nm": "10", "y_nm": "20"},
            orientation={"value_degrees": 0.0},
            layer="BL_F_Cu",
            locked="LS_UNLOCKED",
            reference_field={"text": "U1"},
            value_field={"text": "LM2735"},
            definition={"items": [{"id": "pad-1"}, {"id": "shape-1"}]},
        )
    )
    restored = snapshot(
        state(
            "fp-1",
            "footprint",
            id={"value": "fp-1"},
            position={"x_nm": "10", "y_nm": "20"},
            orientation={"value_degrees": 0.0},
            layer="BL_F_Cu",
            locked="LS_UNLOCKED",
            reference_field={"text": "U1"},
            value_field={"text": "LM2735"},
            definition={"items": [{"id": "shape-1"}, {"id": "pad-1"}]},
        )
    )

    assert restored.fingerprint != target.fingerprint
    assert snapshots_match_restored_state(restored, target)


def test_restored_footprint_still_requires_replayable_fields_to_match():
    target = snapshot(
        state(
            "fp-1",
            "footprint",
            id={"value": "fp-1"},
            position={"x_nm": "10", "y_nm": "20"},
            orientation={"value_degrees": 0.0},
            reference_field={"text": "U1"},
            value_field={"text": "LM2735"},
        )
    )
    wrong_position = snapshot(
        state(
            "fp-1",
            "footprint",
            id={"value": "fp-1"},
            position={"x_nm": "11", "y_nm": "20"},
            orientation={"value_degrees": 0.0},
            reference_field={"text": "U1"},
            value_field={"text": "LM2735"},
        )
    )

    assert not snapshots_match_restored_state(wrong_position, target)


def test_restored_non_footprint_items_remain_exact():
    target = snapshot(state("track-1", "track", width={"value_nm": "400000"}))
    restored = snapshot(state("track-1", "track", width={"value_nm": "500000"}))

    assert not snapshots_match_restored_state(restored, target)
