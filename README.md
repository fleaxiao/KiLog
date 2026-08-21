<p align="center">
  <img src="assets/icon-256.png" alt="KiLog icon" width="128" />
</p>

# KiLog

KiLog is a KiCad 9/10 PCB Editor plugin that records live, unsaved board changes through the IPC API.

## Usage

The UI has one file-prefix field. With the default prefix `ref`, KiLog writes files next to the open PCB:

- `ref.json` — accumulated changes; its basename exactly matches the Record
  tab's **Log** value
- `ref_00.kicad_pcb`, `ref_01.kicad_pcb`, ... — marked PCB states; the number is
  the state's actual position in the recorded operation sequence

If the log already exists, KiLog asks whether to overwrite it. Choosing **Yes**
replaces the JSON and starts a new recording; choosing **No** leaves the existing
file untouched and does not start recording.

- The orange centre control restores the PCB to its last saved on-disk state,
  starts recording from that initial state, and changes to **Stop** while active.
- **Back / Next** previews one recorded position. Stopping while previewing keeps
  the selected position and removes later operations from the log.
- While previewing, **Reset** discards all later operations, makes the displayed
  PCB state the new recording baseline, and continues the same recording session.
- The rightmost **Mark** control saves the current recorded position as a PCB snapshot.
- Drag the Record progress bar to preview any captured PCB state.
- **Ctrl+Z** confirms a preview, or reverts the latest change when no preview is active.

## Replay

Click **Load JSON** and choose a KiLog log file. KiLog immediately restores the PCB
referenced by `initial_pcb_path` to its last saved on-disk state and applies each
UUID-addressed operation directly in PCB Editor. Unsaved PCB edits are discarded by
this reset, so save any work that is unrelated to the replay first.

- **Play / Pause** controls automatic fixed-step playback.
- **Back / Next** moves one recorded step.
- Drag the progress bar to seek to any operation.
- Choose `0.25×` through `4×` playback speed.
- Use the rightmost **Mark** control to save the current replay position with the
  same position-based PCB filename used while recording.

Seeking backward resets the PCB to the saved initial state captured when the log was loaded, then
replays up to the requested operation. For reliable results, open the PCB named by
`initial_pcb_path` before loading its log. Playback changes are committed to KiCad's
undo stack and are not saved to disk automatically.

## Skills

The **Skill** tab can create copper zones covering the closed `Edge.Cuts` board
outline. Enter an existing network name (default `GND`) and select `F.Cu`, `B.Cu`,
or both, then click **Fill Board**. Each layer receives its own zone and KiCad
refills all zones after creating one undoable board commit. When Record is active,
the zones are stored as `zone.add` operations and can be recreated by Replay.

## Log format

The log contains the initial PCB path and recorded changes. Every change includes
`record_step`; changes captured in the same Record event share the same value and
Replay applies them together as one displayed step. Footprint placement, movement,
and rotation are all stored as `footprint.move`. Only the latest position and final
angle are retained.

Footprints outside the live `Edge.Cuts` boundary are ignored. Moving a footprint into the board creates a `footprint.move` entry; moving it outside does not store its outside position.

```json
{
  "initial_pcb_path": "C:/project/board.kicad_pcb",
  "changes": [
    {
      "change_uuid": "...",
      "record_step": 1,
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
