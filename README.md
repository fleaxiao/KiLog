<p align="center">
  <img src="assets/icon-256.png" alt="KiLog icon" width="128" />
</p>

# KiLog

KiLog is a KiCad 9/10 PCB Editor plugin that records live, unsaved board changes through the IPC API.

## Usage

The UI has one file-prefix field. With the default prefix `ref`, KiLog writes files next to the open PCB:

- `ref_log.json` — accumulated changes
- `ref_01.kicad_pcb`, `ref_02.kicad_pcb`, ... — reference snapshots

If the log already exists, KiLog warns and does not overwrite it. Choose another prefix to start a new recording.

- **Start** begins recording.
- **Note** saves a numbered PCB snapshot.
- **Undo** reverts the latest recorded change and updates the log.
- **End** flushes pending changes and stops recording.

## Replay

Click **Load JSON** and choose a KiLog log file. KiLog immediately restores the PCB
referenced by `initial_pcb_path` to its last saved on-disk state and applies each
UUID-addressed operation directly in PCB Editor. Unsaved PCB edits are discarded by
this reset, so save any work that is unrelated to the replay first.

- **Play / Pause** controls automatic fixed-step playback.
- **Back / Next** moves one operation; **-10 / +10** jumps ten operations.
- Drag the progress bar to seek to any operation.
- Choose `0.25×` through `4×` playback speed.

Seeking backward resets the PCB to the saved initial state captured when the log was loaded, then
replays up to the requested operation. For reliable results, open the PCB named by
`initial_pcb_path` before loading its log. Playback changes are committed to KiCad's
undo stack and are not saved to disk automatically.

## Log format

The log contains the initial PCB path and recorded changes. Footprint placement, movement, and rotation are all stored as `footprint.move`. Only the latest position and final angle are retained.

Footprints outside the live `Edge.Cuts` boundary are ignored. Moving a footprint into the board creates a `footprint.move` entry; moving it outside does not store its outside position.

```json
{
  "initial_pcb_path": "C:/project/board.kicad_pcb",
  "changes": [
    {
      "change_uuid": "...",
      "item_uuid": "...",
      "operation": "footprint.move",
      "position": {"x_nm": "10000000", "y_nm": "20000000"},
      "orientation": {"value_degrees": 270}
    }
  ]
}
```

## Installation

Place this repository in the KiCad plugin directory:

- Windows: `%USERPROFILE%\Documents\KiCad\<version>\plugins\KiLog`
- macOS: `~/Documents/KiCad/<version>/plugins/KiLog`
- Linux: `~/.local/share/KiCad/<version>/plugins/KiLog`

Enable the IPC API under **PCB Editor > Preferences > Plugins**, then reload or restart PCB Editor.

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```
