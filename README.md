<p align="center">
  <img src="assets/icon-256.png" alt="KiLog icon" width="128" />
</p>

# KiLog

KiLog is a KiCad 9/10 PCB Editor plugin for recording unsaved board changes,
replaying them from JSON, and applying a small set of board-editing helpers. It
communicates with the active PCB Editor through KiCad's IPC API.

## Features

- Record live PCB edits as UUID-addressed JSON operations.
- Preview, undo, truncate, and mark positions in a recording.
- Replay a log step by step or at `0.25×`–`4×` speed.
- Create full-board copper zones on `F.Cu`, `B.Cu`, or both.
- Fan out matching SMD pads with orthogonal traces and through vias.

## Installation

Copy this repository to the KiCad plugin directory:

- Windows: `%USERPROFILE%\Documents\KiCad\<version>\plugins\KiLog`
- macOS: `~/Documents/KiCad/<version>/plugins/KiLog`
- Linux: `~/.local/share/KiCad/<version>/plugins/KiLog`

In PCB Editor, enable the IPC API under **Preferences > Plugins**, then reload
the plugins or restart PCB Editor. Open a board before launching KiLog.

## Record

The **Log** field controls the output prefix. If it contains `ref`, files are
written beside the open PCB as:

- `ref.json` — the operation log.
- `ref_000.kicad_pcb`, `ref_001.kicad_pcb`, … — marked board states, numbered by
  their actual position in the operation sequence.

The centre button first restores the open PCB to its last saved on-disk state,
then starts recording. It changes to **Stop** while recording. If the JSON file
already exists, KiLog asks before replacing it.

Recording changes stay in memory until **Stop** writes the JSON log. Starting,
continuing, undoing, and resetting a recording do not create or modify the log
on disk. Existing logs remain unchanged until Stop succeeds. Closing KiLog does
not save automatically; use Stop first. **Mark** still explicitly saves a PCB copy.

While recording:

- **Back / Next** previews recorded positions.
- Dragging the progress bar seeks to a recorded position.
- **Reset** accepts the preview, removes all later operations, and continues
  recording from that state.
- **Stop** also accepts the currently previewed position before ending.
- **Mark** saves the current position as a `.kicad_pcb` copy.
- **Ctrl+Z** in PCB Editor is handled by KiCad itself. KiLog does not intercept it.

Recording positions are observed board states, not native undo transaction IDs.
The current IPC API does not expose the editor's undo stack or commit notifications.
During recording KiLog keeps each observed change intact and in order. Edits between polls
can still share one observation, so exact one-to-one native undo recording requires
additional support inside KiCad. Explicit preview/reset restores a recorded state
as a new edit; it is not the same operation as native Ctrl+Z. The native undo path
never overwrites KiCad's result with a snapshot if the boundaries differ.

When the editor returns to an earlier recorded board state (for example after
Ctrl+Z), KiLog removes the later in-memory steps and updates the step counter.
The inverse edit is not appended as another step. This uses board-state matching:
a manual edit returning to exactly the same state is also treated as a rewind.

On **Stop**, the saved JSON combines consecutive position/angle adjustments of
the same footprint into one step, keeping the last transform. Intervening edits
prevent merging. All footprint silk-field edits and standalone silkscreen text
and graphics are collected in one final step. Deferred footprint field values
are taken from the final board so later footprint moves do not leave stale silk
positions. Saved steps are renumbered; the in-memory recording history is unchanged.

## Replay

Choose **Load** on the Replay tab and select a KiLog JSON file. KiLog restores
the PCB named by `initial_pcb_path` to its saved state before applying the log.

> **Warning:** loading a replay discards unsaved edits in the open PCB. Save
> unrelated work first, and open the PCB referenced by the log.

Replay controls provide continue-recording, previous, play/pause, next, and mark.
The progress bar seeks to any step. Seeking backward restores a cached earlier
state. **Continue recording** switches to Record at the current step and discards
all later steps from the loaded log; new edits are then appended from that PCB
state. Playback changes are placed on KiCad's undo stack but are not saved to
disk automatically.

## Board helpers

### Copper zones

On the **Skill** tab, enter the base net, select `F.Cu`, `B.Cu`, or both, and
choose **GNDfill**. KiLog creates one full-board zone per selected layer in a single
undoable commit. It also creates a higher-priority local zone when two or more
pads in one non-magnetic footprint share another net on the same selected layer.
The central body of power inductors and transformers remains copper-free while
copper around their pads is preserved. KiLog prefers a closed `F.SilkS` outline,
then a closed `F.Fab` outline. Separate lines and arcs are joined into closed
contours; concave polygons and curved outlines retain their actual shape. Inner
markings do not replace the enclosing body outline. Only when no closed body
outline exists does KiLog fall back to the central rectangle derived from pad
placement. The zones are intentionally left unfilled; refill
them in KiCad when ready. Active recording captures them as `zone.add` operations.

The **Net** input selects the full-board base plane (GND by default).
GNDfill automatically detects other nets named `GND`, `GND1`, `GND2`, etc.
(case-insensitive) and cuts rectangular holes in the base plane for them. Each secondary rectangle
encloses every component connected to that net, including all of each component's
pads and courtyard/fabrication/silkscreen graphics, plus all tracks (including arc
tracks and their widths) and vias (including copper diameters) on that net across
all layers, with a 0.5 mm margin and square
corners, and receives its own copper zone. Overlapping ground rectangles or a
rectangle containing another ground's pad are reported without modifying the board.
Rectangles are clipped to Edge.Cuts, cutouts and magnetic keepouts; actual curved
or diagonal board boundaries are preserved. Both selected layers use the same
regions. Each secondary ground net must have a component, track or via to locate
its region; otherwise the command reports the missing net
without modifying the board. Ground nets do not receive additional local pad
zones that could override the partition. Ground partitioning and closed magnetic
body recognition use Shapely
from `requirements.txt`. Review the generated boundaries before refilling copper.

Transformers (`T` references) and coupled inductors are exceptions to full-component coverage: only
pads on the secondary ground itself contribute to its region, including their
copper extents on every layer. Other transformer pads and other nets' routing do
not expand the rectangle. All tracks and vias on the secondary ground still
contribute, and other connected components retain full-component coverage.
Magnetic body keepouts still apply.
Coupled inductors are recognized from transformer/coupled metadata, or from an
`L` reference with at least four distinct numbered pads when metadata is absent.

### Fanout

Enter an existing net and a fallback track width in millimetres (0.4 mm by
default), then choose **Fanout**. For each matching on-board SMD pad, KiLog creates an orthogonal trace
and a configurable-diameter through via in one undoable commit. The UI defaults
to a 0.5 mm via with a configurable 0.3 mm drill.

Fanout behavior:

- Candidate vias start at least 0.5 mm from the pad center and are tried at
  0.1 mm increments along the four board axes, increasing the initial distance
  when pad geometry requires it. Straight horizontal or vertical routes are
  preferred. If none fits, fanout tries an orthogonal escape followed by one
  45-degree turn, checking both segments and the end via. Pads with no valid
  route are reported as incomplete.
- Front-side footprints use `F.Cu`; back-side footprints use `B.Cu`.
- Each component uses the widest trace directly connected to any of its pads
  (matching that pad's net), even if the pad belongs to another net. Via and drill
  diameters scale by the resulting width divided by the entered width, allowing
  smaller or larger sizes. After scaling, minimums are 0.2 mm trace width,
  0.2 mm via diameter, and 0.1 mm drill diameter. Without connected traces,
  the entered values use the same minimums.
- Via copper stays at least 0.5 mm from the closed `Edge.Cuts` outline and
  internal cut-outs.
- Placement avoids other on-board pads, existing vias, and traces on other nets.
- Each fanout uses one clearance: the smaller of 0.2 mm and the minimum
  starting trace-copper gap to nearby pads, vias, and other-net tracks.
  This gap applies to the whole trace and its end via, including obstacles
  farther along the route. Copper contact is rejected; board-edge spacing
  remains unchanged.
- Pads already connected to a same-net via are skipped, so repeated runs do not
  duplicate completed fanouts.
- KiLog exits automatically when its associated PCB Editor window closes.

## Log format

Changes captured together are stored under one step and replayed as one atomic
KiCad commit, without exposing intermediate object states. Footprint placement, movement, and
rotation are stored as `footprint.move`; consecutive transforms keep only the
latest position and angle.
Independent moves and edits of footprint Reference and Value fields are stored
as `footprint.field.modify` changes and replayed without moving the footprint anchor.

Persisted changes describe only their replay target: complete additions use
`item` with the exact KiCad `type` and `data` (including `data.id.value`), field
updates use `value`, and field removals use `delete: true`. The recording JSON
does not store the redundant item `kind` or `item_uuid`, or any `before` and
`after` values. Changes without a complete `item` use the shorter `id` field.
Each numbered `step` has one `step_uuid`; individual changes do not carry UUIDs.

Footprints outside the live `Edge.Cuts` boundary are ignored. Moving a footprint
into the board is recorded; moving it out does not preserve the outside position.

```json
{
  "initial_pcb_path": "C:/project/board.kicad_pcb",
  "steps": [
    {
      "step": 1,
      "step_uuid": "...",
      "changes": [
        {
          "id": "...",
          "operation": "footprint.move",
          "position": {"x_nm": "10000000", "y_nm": "20000000"},
          "orientation": {"value_degrees": 270}
        }
      ]
    }
  ]
}
```

The schema is available at
[`kilog/schemas/operation-log-v1.schema.json`](kilog/schemas/operation-log-v1.schema.json).

## Project layout

- `kilog_action.py` — KiCad entry point and DPI bootstrap.
- `kilog/recorder.py` — recording state and log persistence.
- `kilog/replay.py` — log validation and playback controller.
- `kilog/kicad_adapter.py` — KiCad IPC reads, writes, and board helpers.
- `kilog/ui.py` — wxPython control panel.
- `tests/` — unit tests using fake board adapters.

## Development

Requires Python 3.10 or newer.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

See [CHANGELOG.md](CHANGELOG.md) for release history.
