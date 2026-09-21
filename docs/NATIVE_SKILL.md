# Allegro 24.1 native update bundle

`brd_spd.skill.generate_bundle()` creates a batch SKILL bundle that applies selected SPD plane geometry to a caller-supplied Allegro board. Bundle generation parses the SPD but does not launch Allegro, consume a license, or prove that the native update succeeded.

```python
from pathlib import Path
from brd_spd.skill import generate_bundle

result = generate_bundle(
    Path("design.spd"),
    Path("native-job"),
    layers=["TOP"],       # original Signal$ name is also accepted
    nets=["PWR"],
    update_components=False,
)
```

The destination must not already exist. The generated directory contains `design.il`, `run.scr`, `manifest.json`, `generation.log`, and `generation.report.json`. It intentionally does not contain `base.brd`.

Copy the original Allegro board to `native-job/base.brd`, then run Allegro 24.1 with the bundle directory as the working directory:

```powershell
& 'C:\Cadence\SPB_24.1\tools\bin\allegro.exe' -nograph -s run.scr base.brd
```

The SKILL program opens `base.brd`, validates every selected layer and net, starts a database transaction, replaces the selected plane shapes, updates dynamic shapes globally, and saves a separate `result.brd`. Coordinates stay in millimetres in the generated source and are converted to the active board units with `axlMKSConvert`; the board unit setting is not changed.

Treat execution as successful only when all three checks pass: Allegro exits with code 0, `result.json` contains `"status":"success"`, and `result.brd` exists and is nonempty. `execution.log` records native progress and the first explicit failing operation. A failed run rolls back an active transaction, removes a partial `result.brd`, writes a failed `result.json`, and exits with code 2.

## Existing Allegro session selected by PID

Set `session_mode=True` to generate a bundle for the drawing already open in a user-selected Allegro process:

```python
result = generate_bundle(
    Path("design.spd"),
    Path("native-session-job"),
    layers=["L08(DGND)"],
    nets=["DGND"],
    session_mode=True,
)
```

This mode does not open `base.brd`, close Allegro, or call `axlOSExit`. `run.scr` contains one absolute `skill load(".../design.il")` command without `exit`; the Windows agent sends that line directly to the specific validated Allegro PID.

Before changing the current drawing, SKILL checks `axlOKToProceed(t)`, validates every selected layer and net, honors a queued `cancel.flag`, and saves the complete current state to the absolute `session-before.brd` path with `?writeModel t`. This captures unsaved edits without renaming or overwriting the user's source board. The successful final save uses the absolute `result.brd` path and leaves that result open in the selected Allegro session.

All runtime artifacts use absolute paths inside the unique job directory:

- `execution.started` records entry into the selected session.
- `session-before.brd` is the recovery snapshot and remains available after success, failure, or cancellation.
- `cancel.flag` requests cooperative cancellation. SKILL checks it before backup, before every selected-pair deletion, between plane shapes, before component moves, and before the commit/save sequence.
- `result.json` has `success`, `failed`, or `cancelled` status. Its `design_modified` boolean says whether the transaction committed, and `recovery_brd` gives the absolute recovery snapshot path.
- `finished.flag` is written only after `result.json` is closed and is the terminal polling marker.

The script refuses to overwrite any pre-existing runtime output or snapshot. A process-wide `bsSessionBusy` guard is checked before the job resets any of its globals; a second job receives its own failed result and finished marker without modifying the drawing. Cancellation or failure rolls back an active transaction when possible and leaves Allegro running. If saving fails after commit, `design_modified` is true: the current session remains modified, and `session-before.brd` is the explicit recovery copy. The script does not force-open that backup or discard the current design.

Session mode does not call the global `axlDBDynamicShapes(t)` update because that could repour stale dynamic shapes outside the selected layer/net scope in the user's live drawing. The generated report records `NATIVE_SESSION_DYNAMIC_REPOUR_DEFERRED`; review or update unrelated dynamic shapes interactively after the scoped result. Batch mode retains the global update in its isolated board process.

The installed 24.1 ISR008 `allegro_sendcmd.exe` was inspected to establish the transport used by Cadence: it selects the first top-level window whose title contains `Allegro`, reads command lines from standard input into a 1024-byte buffer, and sends `WM_COPYDATA` with `dwData=0x26297811`. Because it has no PID filter, it is unsafe when several Allegro sessions are open. The agent instead enumerates top-level windows, matches the requested PID and executable identity, and sends the same payload only to that window. The Windows ANSI encoded dispatch line including its newline must fit within 1023 bytes; unrepresentable paths are rejected. Generated SKILL remains runtime-unverified until exercised against the licensed target installation. The Windows transport itself is tested with two synthetic receiver processes to verify PID isolation and exact payload bytes.

## Applied changes

- Positive polygon and circle records become solid static `ETCH/<layer>` shapes.
- Negative records become permanent polygon or circular voids in a uniquely containing positive shape on the same net. Ambiguous, unmatched, or invalid void geometry stops bundle generation before an executable bundle is published.
- For each selected layer/net pair, existing dynamic `BOUNDARY/<layer>` shapes and existing static `ETCH/<layer>` shapes are deleted before replacement. Auto-generated ETCH children of dynamic shapes are not deleted directly.
- With `update_components=True`, an existing placed refdes is moved and rotated with `axlTransformObject`. The SPD placement layer must be the first or last conductor layer, and it must match the base symbol side. A missing, unplaced, or side-mismatched component fails the native transaction.

The default selection includes every mapped conductor plane section that has a net and positive geometry. Supplying `layers` or `nets` narrows that scope. A requested selection with no positive geometry fails before any native deletion.

## Preserved and unsupported changes

The base board remains the source for constraints, stackup, padstacks, routing, vias, component definitions, artwork, and all unselected objects. SPD trace and via differences are not applied. Component creation, deletion, package changes, and side changes are not applied. These limits and their SPD record counts are written to the generation report; the generator does not claim a full SPD-to-BRD reconstruction.

Replacing a selected pair removes every dynamic plane boundary and every independent static plane shape for that net on that layer. Review `manifest.json` and the generated report before running, and compare `result.brd` with the original in Allegro after execution.

The emitted APIs and behavior were checked against the installed Allegro 24.1 SKILL reference under `share\pcb\examples\skill\DOC\FUNCS`, including `axlOpenDesignForBatch`, `axlDBGetShapes`, `axlDeleteObject`, `axlDBCreateOpenShape`, `axlDBCreateVoid`, `axlDBCreateVoidCircle`, `axlDBCreateCloseShape`, `axlDBTransactionStart`, `axlTransformObject`, `axlMKSConvert`, and `axlSaveDesign`. Native execution still requires a licensed Allegro 24.1 installation and a real board review.
