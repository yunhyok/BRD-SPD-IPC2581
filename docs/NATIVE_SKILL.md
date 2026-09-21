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

The SKILL program opens `base.brd`, validates every selected layer and net, starts a database transaction, replaces the selected plane shapes, and saves a separate `result.brd`. Coordinates stay in millimetres in the generated source and are converted to the active board units with `axlMKSConvert`; the board unit setting is not changed.

Treat execution as successful only when all three checks pass: Allegro exits with code 0, `result.json` contains `"status":"success"`, and `result.brd` exists and is nonempty. `execution.log` records native progress and the first explicit failing operation. A failed run rolls back an active transaction, removes a partial `result.brd`, writes a failed `result.json`, and exits with code 2.

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
