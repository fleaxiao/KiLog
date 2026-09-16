# Changelog

## Unreleased

- Set the default fanout width, via diameter, and drill diameter to 0.4 mm,
  0.5 mm, and 0.3 mm respectively.
- Size fanout per component using its widest directly connected trace, scaling
  via and drill diameters in proportion to the entered width.
- Reduce the fanout candidate search step to 0.1 mm so narrow valid placement
  windows are not skipped.
- Allow a 1 nm numerical tolerance in fanout clearance comparisons so an exact
  design-rule clearance is not rejected by floating-point rounding.
- Restrict fanout to horizontal or vertical traces; remove lateral offsets that
  allowed diagonal escape traces.
- Ignore anonymous paste-aperture pads that KiCad IPC reports on top of numbered
  BGA copper pads when checking fanout collisions.
- Reduce the minimum fanout length from 1.0 mm to 0.5 mm.
- Avoid false restore failures when KiCad repacks an otherwise equivalent footprint definition.
- Record every track and via created by one fanout command as a single step.
- Continue a replay as a recording from its current step, truncating later steps.
- Prevent fanout traces and vias from touching existing traces on other nets.
- Prefer F.SilkS or F.Fab magnetic body outlines, with pad geometry as fallback.
- Close KiLog automatically when its associated PCB Editor window closes.
- Keep fanout via copper at least 0.5 mm from board edges and internal cut-outs.
- Record and replay independent footprint Reference and Value field moves.
- Store recordings as UUID-addressed grouped steps with target-only values and replay every step as one atomic KiCad commit.
- Simplify recorder history state and remove unused storage/replay APIs.
- Remove obsolete no-op status plumbing from the compact UI.
- Reorganize the README around installation, workflows, safety, and development.
- Make the repository root the live KiCad plugin directory for restart-to-update development.
- Remove PCM metadata, ZIP artifacts, and package build tooling.

## 1.0.1 — 2026-08-03

- Replace Tkinter with the wxPython runtime bundled with KiCad 10.
- Fix startup on KiCad distributions that do not ship the `_tkinter` extension module.
- Validate the real KiCad 10 plugin virtual environment and hidden wx frame initialization.

## 1.0.0 — 2026-08-03

- Initial KiLog recorder implementation.
