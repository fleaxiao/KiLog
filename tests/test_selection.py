from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kilog.selection import export_selection, filter_board, parse_board, selection_output_stem


BOARD = '''(kicad_pcb (version 20241229)
 (net 0 "") (net 1 "GND") (net 2 "signal") (net 3 "unused")
 (footprint "selected (part)" (uuid "a") (at 10 20 90)
  (property "Reference" "U1") (property "Value" "quoted \\"value\\"")
  (pad "1" smd rect (uuid "pad-a") (net 1 "GND"))
  (pad "2" smd rect (net 2 "signal")) (pad "3" smd rect (net 0 "")))
 (footprint "other" (uuid "b") (pad "1" smd rect (net 3 "unused")))
 (segment (uuid "shared") (net 1)) (arc (uuid "arc") (net 2))
 (via (uuid "via") (net 2)) (segment (uuid "other") (net 3))
 (segment (uuid "unassigned") (net 0))
 (zone (net 1) (filled_polygon (pts (xy 0 0))))
 (zone (net 2)) (zone (net 0) (keepout (tracks not_allowed)))
 (gr_line (start 0 0) (end 1 1) (layer "Edge.Cuts"))
 (group "removed" (uuid "g1") (members "b"))
 (group "nested" (uuid "g2") (members "g1"))
 (group "kept" (uuid "g3") (members "a" "via")))'''


class SelectionTests(unittest.TestCase):
    def test_filter_preserves_layout_and_associated_routing(self):
        result = filter_board(BOARD, {"a"})
        board = parse_board(result)
        self.assertEqual([f.field("uuid") for f in board.children("footprint")], ["a"])
        for uuid in ("shared", "arc", "via", "g3", "other", "unassigned", "b", "g1", "g2"):
            self.assertNotIn(f'(uuid "{uuid}")', result)
        self.assertEqual(len(board.children("zone")), 1)
        self.assertTrue(board.children("zone")[0].children("keepout"))
        self.assertIn('(at 10 20 90)', result)
        self.assertIn('(layer "Edge.Cuts")', result)
        original = parse_board(BOARD).children("footprint")[0]
        self.assertIn(BOARD[original.start:original.end], result)

    def test_all_selected_still_removes_copper_and_net_zero(self):
        result = filter_board(BOARD, {"a", "b"})
        self.assertNotIn('(uuid "other")', result)
        self.assertNotIn('filled_polygon', result)
        self.assertNotIn('(uuid "unassigned")', result)

    def test_empty_stale_and_invalid_selections_fail(self):
        for selected in (set(), {"missing"}, {"a", "missing"}):
            with self.assertRaises(ValueError):
                filter_board(BOARD, selected)
        with self.assertRaises(ValueError):
            filter_board(BOARD[:-1], {"a"})

    def test_footprint_copper_zones_are_removed_too(self):
        source = '(kicad_pcb (footprint "part" (uuid "a") (zone (net 1))))'
        self.assertNotIn('(zone', filter_board(source, {"a"}))

    def test_named_nets_and_unicode_are_preserved(self):
        source = '''(kicad_pcb
          (footprint "电阻" (uuid "a") (pad "1" smd rect (net "电源")))
          (segment (net "电源")) (via (net "other")))'''
        result = filter_board(source, {"a"})
        self.assertIn('"电阻"', result)
        self.assertNotIn('(segment', result)
        self.assertNotIn('(via', result)

    def test_export_overwrites_same_name_but_preserves_source(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            original = directory / "board.kicad_pcb"
            original.write_text("original on disk", encoding="utf-8")
            existing = directory / "board_U1.kicad_pcb"
            existing.write_text("previous mark", encoding="utf-8")
            adapter = SimpleNamespace(
                board_path=original, output_directory=directory,
                save_copy=lambda path: path.write_text(BOARD, encoding="utf-8"),
            )
            output = export_selection(adapter, {"a"})
            self.assertEqual(output.name, "board_U1.kicad_pcb")
            self.assertEqual(original.read_text(), "original on disk")
            self.assertEqual(existing.read_text(encoding="utf-8"), filter_board(BOARD, {"a"}))
            self.assertEqual(output.read_text(encoding="utf-8"), filter_board(BOARD, {"a"}))
            with self.assertRaises(ValueError):
                export_selection(adapter, {"missing"})
            self.assertEqual(len(list(directory.glob("*.kicad_pcb"))), 2)

    def test_failed_replace_preserves_existing_export_and_cleans_temp(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            existing = directory / "board_U1.kicad_pcb"
            existing.write_text("previous mark", encoding="utf-8")
            adapter = SimpleNamespace(
                board_path=directory / "board.kicad_pcb", output_directory=directory,
                save_copy=lambda path: path.write_text(BOARD, encoding="utf-8"),
            )
            with patch.object(Path, "replace", side_effect=PermissionError("file busy")):
                with self.assertRaises(PermissionError):
                    export_selection(adapter, {"a"})
            self.assertEqual(existing.read_text(), "previous mark")
            self.assertEqual(list(directory.iterdir()), [existing])

    def test_filename_uses_exported_references_in_natural_order(self):
        source = '(kicad_pcb ' + ' '.join(
            f'(footprint "part" (property "Reference" "{ref}"))'
            for ref in ["U1", "R10", "C2", "R2"]
        ) + ')'
        self.assertEqual(selection_output_stem("original", source), "original_C2_R2_R10_U1")

    def test_first_export_has_no_counter_suffix(self):
        with TemporaryDirectory() as tmp:
            adapter = SimpleNamespace(
                board_path=Path(tmp) / "original.kicad_pcb", output_directory=Path(tmp),
                save_copy=lambda path: path.write_text(BOARD, encoding="utf-8"),
            )
            self.assertEqual(export_selection(adapter, {"a"}).name, "original_U1.kicad_pcb")

    def test_invalid_reference_cannot_escape_output_directory(self):
        for reference in ("../R1", "R:1", "", "R1."):
            with self.assertRaises(ValueError):
                selection_output_stem("board", f'(kicad_pcb (footprint "part" (property "Reference" "{reference}")))')

    def test_locked_components_are_retained_by_default(self):
        for flag in ('locked', '(locked yes)'):
            source = f'(kicad_pcb (footprint "part" (uuid "a") {flag}) (footprint "other" (uuid "b")))'
            result = filter_board(source, set())
            self.assertIn('(uuid "a")', result)
            self.assertNotIn('(uuid "b")', result)

    def test_locked_only_export_omits_reference_from_filename(self):
        with TemporaryDirectory() as tmp:
            adapter = SimpleNamespace(
                board_path=Path(tmp) / "board.kicad_pcb", output_directory=Path(tmp),
                save_copy=lambda path: path.write_text(BOARD.replace('(uuid "a")', '(uuid "a") (locked yes)'), encoding="utf-8"),
            )
            output = export_selection(adapter, set())
            self.assertEqual(output.name, "board_select.kicad_pcb")
            self.assertIn('(uuid "a")', output.read_text(encoding="utf-8"))

    def test_filename_omits_locked_references_in_mixed_selection(self):
        for flag in ('locked', '(locked yes)'):
            source = f'''(kicad_pcb
              (footprint "fixed" {flag} (property "Reference" "U1"))
              (footprint "part" (property "Reference" "R2"))
              (footprint "part" (property "Reference" "C1")))'''
            self.assertEqual(selection_output_stem("board", source), "board_C1_R2")


if __name__ == "__main__":
    unittest.main()
