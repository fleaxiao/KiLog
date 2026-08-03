# Changelog

## Unreleased

- Make the repository root the live KiCad plugin directory for restart-to-update development.
- Remove PCM metadata, ZIP artifacts, and package build tooling.

## 1.0.1 — 2026-08-03

- Replace Tkinter with the wxPython runtime bundled with KiCad 10.
- Fix startup on KiCad distributions that do not ship the `_tkinter` extension module.
- Validate the real KiCad 10 plugin virtual environment and hidden wx frame initialization.

## 1.0.0 — 2026-08-03

- Initial KiLog recorder implementation.
