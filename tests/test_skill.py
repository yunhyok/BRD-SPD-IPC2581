from __future__ import annotations

import json

import pytest

from brd_spd.skill import generate_bundle


SPD = """Title native bundle test
.Shape top_plane
Polygon1::PWR+ 0mm 0mm 10mm 0mm 10mm 10mm 0mm 10mm
Circle2::PWR- 5mm 5mm 1mm
.EndShape
Signal$TOP Thickness = 35um Material = COPPER
PatchSignal$TOP Shape = top_plane Layer = Signal$TOP
Medium$CORE Thickness = 1mm Material = FR4
Signal$BOTTOM Thickness = 35um Material = COPPER
Node1::PWR X = 1mm Y = 1mm Layer = Signal$TOP
Node2::PWR X = 2mm Y = 1mm Layer = Signal$BOTTOM
Trace1::PWR StartingNode = Node1::PWR EndingNode = Node2::PWR Width = 0.2mm
Via1::PWR UpperNode = Node1::PWR LowerNode = Node2::PWR PadStack = VIA
.PadStackDef VIA 0.1mm 0.05mm
.PadDef Signal$TOP
Regular Circle 0.2mm
.EndPadDef
.PadDef Signal$BOTTOM
Regular Circle 0.2mm
.EndPadDef
.EndPadStackDef
.Component C1 3mm 4mm Rotation = 90 StartLayer = Signal$BOTTOM
"""


def test_generates_conservative_native_bundle(tmp_path):
    source = tmp_path / "board.spd"
    output = tmp_path / "native"
    source.write_text(SPD, encoding="utf-8")
    progress = []

    result = generate_bundle(source, output, update_components=True, progress=progress.append)

    assert result["status"] == "generated_unverified"
    assert set(path.name for path in output.iterdir()) == {
        "design.il",
        "run.scr",
        "manifest.json",
        "generation.log",
        "generation.report.json",
    }
    assert not (output / "base.brd").exists()
    assert not (output / "result.brd").exists()
    assert (output / "run.scr").read_text(encoding="ascii") == 'skill load("design.il")\nexit\n'

    skill = (output / "design.il").read_text(encoding="utf-8")
    assert 'bsValidate("TOP" "PWR")' in skill
    assert 'bsDeletePlane("TOP" "PWR")' in skill
    assert 'bsBeginPolygon("TOP" "PWR"' in skill
    assert "bsCircleVoid(5 5 1)" in skill
    assert 'bsUpdateComponent("C1" 3 4 90 t)' in skill
    assert 'axlMKSConvert(1.0 "mm" car(axlDBGetDesignUnits()))' in skill
    assert "axlMKS2UU" not in skill
    assert "axlDBTransactionStart()" in skill
    assert "axlDBTransactionRollback(bsTxn)" in skill
    assert "axlDBDynamicShapes(t)" in skill
    assert "axlDeleteObject(shape)" in skill
    assert 'axlSaveDesign(?design "result.brd" ?noConfirm t)' in skill
    assert "axlOSExit(0)" in skill and "axlOSExit(2)" in skill
    assert skill.index("bsValidateAll()") < skill.index("bsTxn = axlDBTransactionStart()")
    transaction = skill.index("bsTxn = axlDBTransactionStart()")
    assert transaction < skill.index("\n  bsApply()\n", transaction)

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "generated_unverified"
    assert manifest["selected_pairs"] == [{"layer": "TOP", "net": "PWR"}]
    report = json.loads((output / "generation.report.json").read_text(encoding="utf-8"))
    assert report["execution_verified"] is False
    assert report["counts"]["native_plane_shapes"] == 1
    assert report["counts"]["native_voids"] == 1
    assert report["counts"]["unsupported_traces"] == 1
    assert report["counts"]["unsupported_vias"] == 1
    assert progress[0] == "Native bundle: parsing SPD"


def test_filters_accept_native_layer_name_and_preserve_utf8_strings(tmp_path):
    source = tmp_path / "unicode.spd"
    source.write_text(SPD.replace("PWR", "전원"), encoding="utf-8")

    result = generate_bundle(source, tmp_path / "native", layers=["TOP"], nets=["전원"])

    assert result["status"] == "generated_unverified"
    skill = (tmp_path / "native" / "design.il").read_text(encoding="utf-8")
    assert 'bsValidate("TOP" "전원")' in skill
    assert "\\u" not in skill


def test_unsafe_selection_or_void_fails_without_executable_bundle(tmp_path):
    source = tmp_path / "board.spd"
    source.write_text(SPD, encoding="utf-8")
    output = tmp_path / "missing"

    with pytest.raises(ValueError, match="did not resolve safely"):
        generate_bundle(source, output, layers=["NO_SUCH_LAYER"])

    assert not (output / "design.il").exists()
    assert not (output / "run.scr").exists()
    assert json.loads((output / "generation.report.json").read_text())["status"] == "failed"

    unmatched = tmp_path / "unmatched.spd"
    unmatched.write_text(SPD.replace("5mm 5mm 1mm", "20mm 20mm 1mm"), encoding="utf-8")
    unmatched_output = tmp_path / "unmatched"
    with pytest.raises(ValueError, match="no unique positive parent"):
        generate_bundle(unmatched, unmatched_output)
    assert not (unmatched_output / "design.il").exists()
    assert not (unmatched_output / "run.scr").exists()


def test_component_updates_require_an_outer_layer(tmp_path):
    source = tmp_path / "component.spd"
    source.write_text(SPD.replace("StartLayer = Signal$BOTTOM", "StartLayer = Medium$CORE"), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported placement layer"):
        generate_bundle(source, tmp_path / "native", update_components=True)


def test_refuses_existing_destination(tmp_path):
    source = tmp_path / "board.spd"
    source.write_text(SPD, encoding="utf-8")
    output = tmp_path / "native"
    output.mkdir()

    with pytest.raises(FileExistsError):
        generate_bundle(source, output)


def test_existing_session_bundle_preserves_open_design_and_has_recovery_protocol(tmp_path):
    source = tmp_path / "board.spd"
    output = tmp_path / "native-session"
    source.write_text(SPD, encoding="utf-8")

    result = generate_bundle(
        source,
        output,
        update_components=True,
        session_mode=True,
    )

    assert result["status"] == "generated_unverified"
    skill = (output / "design.il").read_text(encoding="utf-8")
    script = (output / "run.scr").read_text(encoding="utf-8")
    root = output.resolve().as_posix()

    assert script == f'skill load("{root}/design.il")\n'
    assert "exit" not in script.casefold()
    assert "axlOpenDesign" not in skill
    assert "axlOSExit" not in skill
    assert "deleteFile" not in skill
    assert "axlGetDrawingName()" in skill
    assert "axlOKToProceed(t)" in skill
    assert "?writeModel t" in skill
    assert f'bsBackupPath = "{root}/session-before.brd"' in skill
    assert f'bsCancelPath = "{root}/cancel.flag"' in skill
    assert f'bsFinishedPath = "{root}/finished.flag"' in skill
    assert f'bsStartedPath = "{root}/execution.started"' in skill
    assert f'bsResultBrdPath = "{root}/result.brd"' in skill
    assert f'bsLogPath = "{root}/execution.log"' in skill
    assert f'bsResultJsonPath = "{root}/result.json"' in skill
    assert 'outfile(bsLogPath "a")' in skill
    assert 'outfile(bsResultJsonPath "w")' in skill
    assert "\\u" not in skill
    assert "axlDBDynamicShapes(t)" not in skill
    assert "bsCommitted = nil" in skill
    assert "rollback = errset(axlDBTransactionRollback(bsTxn) t)" in skill
    assert 'if(bsRollbackFailed then "null"' in skill
    assert "if(bsCancelled && !bsRollbackFailed then" in skill
    assert '\\"design_modified\\":%s' in skill
    assert '\\"recovery_brd\\":\\"%s\\"' in skill

    units = skill.index('bsMM = axlMKSConvert(1.0 "mm"', skill.index("procedure(bsMain()"))
    validate = skill.index("bsValidateAll()", skill.index("procedure(bsMain()"))
    backup = skill.index("axlSaveDesign(?design bsBackupPath")
    transaction = skill.index("bsTxn = axlDBTransactionStart()")
    apply = skill.index("\n    bsApply()\n", transaction)
    commit = skill.index("axlDBTransactionCommit(bsTxn)", apply)
    committed = skill.index("bsCommitted = t", commit)
    save_result = skill.index("axlSaveDesign(?design bsResultBrdPath")
    assert units < validate < backup < transaction < apply < commit < committed < save_result
    assert skill.index("bsCheckCancel()", validate) < backup
    assert skill.rfind("bsCheckCancel()", transaction, save_result) != -1
    assert "bsCheckCancel()\n    bsDeletePlane" in skill
    assert "bsCheckCancel()\n    bsBeginPolygon" in skill
    assert "bsCheckCancel()\n    bsUpdateComponent" in skill
    assert "FAILED after commit/save; current design remains modified" in skill

    result_write = skill.rfind('bsResult("failed"')
    finished_write = skill.rfind('bsTouch(bsFinishedPath "finished")')
    assert result_write < finished_write
    assert finished_write < skill.rfind("bsSessionBusy = nil")

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["session_mode"] is True
    assert manifest["base_brd"] == "current_open_design"
    assert manifest["recovery_brd"] == f"{root}/session-before.brd"
    report = json.loads((output / "generation.report.json").read_text(encoding="utf-8"))
    assert "NATIVE_EXISTING_SESSION_MODE" in report["warnings"]
    assert "NATIVE_SESSION_DYNAMIC_REPOUR_DEFERRED" in report["warnings"]


def test_existing_session_busy_guard_precedes_global_reset_and_reports_terminal_state(tmp_path):
    source = tmp_path / "board.spd"
    output = tmp_path / "native-session"
    source.write_text(SPD, encoding="utf-8")

    generate_bundle(source, output, session_mode=True)
    skill = (output / "design.il").read_text(encoding="utf-8")

    assert skill.startswith("if(boundp('bsSessionBusy) && bsSessionBusy then")
    assert skill.index("if(boundp('bsSessionBusy)") < skill.index("bsTxn = nil")
    assert 'message = "Another BRD-SPD session job is active"' in skill
    assert 'status = "cancelled"' in skill
    assert skill.index('outfile("' + output.resolve().as_posix() + '/execution.started"') < skill.index(
        'outfile("' + output.resolve().as_posix() + '/result.json"',
    )
    busy_result_close = skill.index("close(port)", skill.index('outfile("' + output.resolve().as_posix() + '/result.json"'))
    busy_finished = skill.index('outfile("' + output.resolve().as_posix() + '/finished.flag"', busy_result_close)
    assert busy_result_close < busy_finished


def test_existing_session_percent_path_is_not_a_fprintf_format_string(tmp_path):
    source = tmp_path / "board.spd"
    output = tmp_path / "native%session"
    source.write_text(SPD.replace("PWR", "전원"), encoding="utf-8")

    generate_bundle(source, output, session_mode=True)
    skill = (output / "design.il").read_text(encoding="utf-8")
    root = output.resolve().as_posix()

    assert f'bsLogPath = "{root}/execution.log"' in skill
    assert f'bsResultJsonPath = "{root}/result.json"' in skill
    assert f'"{root}/execution.log" "{root}/session-before.brd"' in skill
    assert "\\u" not in skill
    assert all(root not in line for line in skill.splitlines() if "fprintf(" in line)


OVERLAP_SPD = """Title overlapping planes
.Shape top_plane
Polygon1::PWR+ 0mm 0mm 10mm 0mm 10mm 10mm 0mm 10mm
Polygon2::PWR+ 5mm 0mm 15mm 0mm 15mm 10mm 5mm 10mm
Circle3::PWR- 7mm 5mm 1mm
.EndShape
Signal$TOP Thickness = 35um Material = COPPER
PatchSignal$TOP Shape = top_plane Layer = Signal$TOP
"""


CIRCULAR_SPD = """Title circular plane
.Shape top_plane
Circle1::PWR+ 5mm 5mm 5mm
Circle2::PWR- 5mm 5mm 4.999mm
.EndShape
Signal$TOP Thickness = 35um Material = COPPER
PatchSignal$TOP Shape = top_plane Layer = Signal$TOP
"""


def _returns_outside_prog(text: str) -> list[int]:
    """``return`` only unwinds an enclosing ``prog``; anywhere else it errors."""
    offsets, start = [], 0
    while (index := text.find("return(", start)) != -1:
        offsets.append(index)
        start = index + 1
    return [index for index in offsets if "prog(" not in text[:index]]


def test_netless_void_inside_a_selected_positive_is_emitted(tmp_path):
    source = tmp_path / "netless.spd"
    source.write_text(SPD.replace("Circle2::PWR-", "Circle2-"), encoding="utf-8")

    result = generate_bundle(source, tmp_path / "native", layers=["TOP"], nets=["PWR"])

    skill = (tmp_path / "native" / "design.il").read_text(encoding="utf-8")
    assert "bsCircleVoid(5 5 1)" in skill
    assert result["counts"]["native_void_emissions"] == 1


def test_unmatched_void_on_a_selected_net_aborts(tmp_path):
    source = tmp_path / "unmatched-net.spd"
    output = tmp_path / "native"
    # The void's own net is selected (no net filter) but nothing on that net
    # contains it; foreign copper around it is not a parent.
    source.write_text(
        """Title unmatched void
.Shape top_plane
Polygon1::GND+ 0mm 0mm 40mm 0mm 40mm 40mm 0mm 40mm
Circle2::PWR- 20mm 20mm 1mm
.EndShape
Signal$TOP Thickness = 35um Material = COPPER
PatchSignal$TOP Shape = top_plane Layer = Signal$TOP
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no unique positive parent"):
        generate_bundle(source, output)

    assert not (output / "design.il").exists()
    assert not (output / "run.scr").exists()


SPLIT_PLANE_SPD = """Title split plane with an island
.Shape top_plane
Polygon1::GND+ 0mm 0mm 40mm 0mm 40mm 40mm 0mm 40mm
Polygon2::GND- 20mm 0mm 40mm 0mm 40mm 40mm 20mm 40mm
Polygon3::PWR+ 21mm 1mm 39mm 1mm 39mm 39mm 21mm 39mm
Circle4::PWR- 30mm 20mm 1mm
.EndShape
Signal$TOP Thickness = 35um Material = COPPER
PatchSignal$TOP Shape = top_plane Layer = Signal$TOP
"""


def test_split_plane_selection_ignores_the_other_nets_voids(tmp_path):
    source = tmp_path / "split.spd"
    source.write_text(SPLIT_PLANE_SPD, encoding="utf-8")

    ground = generate_bundle(source, tmp_path / "gnd", nets=["GND"])
    power = generate_bundle(source, tmp_path / "pwr", nets=["PWR"])

    gnd_skill = (tmp_path / "gnd" / "design.il").read_text(encoding="utf-8")
    assert 'bsBeginPolygon("TOP" "GND"' in gnd_skill
    assert "\n    bsPolygonVoid(" in gnd_skill
    assert "bsCircleVoid(30 20 1)" not in gnd_skill
    assert ground["counts"]["native_void_emissions"] == 1
    assert ground["counts"]["native_voids_outside_selection"] == 1

    pwr_skill = (tmp_path / "pwr" / "design.il").read_text(encoding="utf-8")
    assert 'bsBeginPolygon("TOP" "PWR"' in pwr_skill
    assert "bsCircleVoid(30 20 1)" in pwr_skill
    assert "\n    bsPolygonVoid(" not in pwr_skill
    assert power["counts"]["native_void_emissions"] == 1
    assert power["counts"]["native_voids_outside_selection"] == 1

    both = generate_bundle(source, tmp_path / "both")
    assert both["counts"]["native_void_emissions"] == 2
    assert "native_voids_outside_selection" not in both["counts"]


def test_void_spanning_overlapping_same_net_shapes_names_the_fix(tmp_path):
    source = tmp_path / "seam.spd"
    source.write_text(
        OVERLAP_SPD.replace("Circle3::PWR- 7mm 5mm 1mm", "Circle3::PWR- 5.5mm 5mm 1mm"),
        encoding="utf-8",
    )
    output = tmp_path / "native"

    with pytest.raises(ValueError, match="spans several overlapping same-net positive shapes"):
        generate_bundle(source, output)

    assert "split the void or merge the shapes in the SPD" in json.loads(
        (output / "generation.report.json").read_text(encoding="utf-8")
    )["error"]
    assert not (output / "design.il").exists()


def test_netless_void_over_two_nets_is_rejected_as_a_short(tmp_path):
    source = tmp_path / "short.spd"
    source.write_text(
        """Title overlapping different nets
.Shape top_plane
Polygon1::PWR+ 0mm 0mm 10mm 0mm 10mm 10mm 0mm 10mm
Polygon2::GND+ 5mm 0mm 15mm 0mm 15mm 10mm 5mm 10mm
Circle3- 7mm 5mm 1mm
.EndShape
Signal$TOP Thickness = 35um Material = COPPER
PatchSignal$TOP Shape = top_plane Layer = Signal$TOP
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="positive shapes of different nets"):
        generate_bundle(source, tmp_path / "native")


def test_unit_conversion_overshoot_still_counts_as_contained(tmp_path):
    source = tmp_path / "mil.spd"
    # 1968.5039370078841mil is 50mm plus 2.6e-13mm of conversion overshoot.
    source.write_text(
        """Title unit overshoot
.Shape top_plane
Polygon1::PWR+ 0mm 0mm 50mm 0mm 50mm 50mm 0mm 50mm
Polygon2::PWR- 10mm 10mm 1968.5039370078841mil 10mm 1968.5039370078841mil 40mm 10mm 40mm
.EndShape
Signal$TOP Thickness = 35um Material = COPPER
PatchSignal$TOP Shape = top_plane Layer = Signal$TOP
""",
        encoding="utf-8",
    )

    result = generate_bundle(source, tmp_path / "native")

    assert result["counts"]["native_void_emissions"] == 1


def test_void_outside_the_selection_is_counted_and_does_not_abort(tmp_path):
    source = tmp_path / "outside.spd"
    source.write_text(
        SPD.replace("Circle2::PWR- 5mm 5mm 1mm", "Circle2::PWR- 5mm 5mm 1mm\nCircle3::GND- 20mm 20mm 1mm"),
        encoding="utf-8",
    )

    result = generate_bundle(source, tmp_path / "native", layers=["TOP"], nets=["PWR"])

    assert result["status"] == "generated_unverified"
    assert result["counts"]["native_voids_outside_selection"] == 1
    assert "NATIVE_VOID_OUTSIDE_SELECTION" in result["warnings"]
    skill = (tmp_path / "native" / "design.il").read_text(encoding="utf-8")
    assert "bsCircleVoid(5 5 1)" in skill
    assert "bsCircleVoid(20 20 1)" not in skill


def test_void_in_an_overlap_is_emitted_into_every_containing_parent(tmp_path):
    source = tmp_path / "overlap.spd"
    source.write_text(OVERLAP_SPD, encoding="utf-8")

    result = generate_bundle(source, tmp_path / "native")

    skill = (tmp_path / "native" / "design.il").read_text(encoding="utf-8")
    assert skill.count("bsCircleVoid(7 5 1)") == 2
    assert skill.count('bsBeginPolygon("TOP" "PWR"') == 2
    assert result["counts"]["native_voids"] == 1
    assert result["counts"]["native_void_emissions"] == 2
    assert result["counts"]["native_voids_multiple_parents"] == 1
    assert "NATIVE_VOID_MULTIPLE_PARENTS" in result["warnings"]


def test_concentric_void_in_a_circular_plane_is_contained(tmp_path):
    source = tmp_path / "circular.spd"
    source.write_text(CIRCULAR_SPD, encoding="utf-8")

    result = generate_bundle(source, tmp_path / "native")

    skill = (tmp_path / "native" / "design.il").read_text(encoding="utf-8")
    assert 'bsBeginCircle("TOP" "PWR" 5 5 5)' in skill
    assert "bsCircleVoid(5 5 4.999)" in skill
    assert result["counts"]["native_void_emissions"] == 1


def test_shape_section_patched_onto_two_layers_is_emitted_on_both(tmp_path):
    source = tmp_path / "multilayer.spd"
    source.write_text(
        SPD.replace(
            "PatchSignal$TOP Shape = top_plane Layer = Signal$TOP",
            "PatchSignal$TOP Shape = top_plane Layer = Signal$TOP\n"
            "PatchSignal$BOTTOM Shape = top_plane Layer = Signal$BOTTOM",
        ),
        encoding="utf-8",
    )
    output = tmp_path / "native"

    result = generate_bundle(source, output)

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["selected_pairs"] == [
        {"layer": "BOTTOM", "net": "PWR"},
        {"layer": "TOP", "net": "PWR"},
    ]
    skill = (output / "design.il").read_text(encoding="utf-8")
    assert 'bsBeginPolygon("TOP" "PWR"' in skill
    assert 'bsBeginPolygon("BOTTOM" "PWR"' in skill
    assert result["counts"]["native_plane_shapes"] == 2
    assert result["counts"]["native_layer_net_pairs"] == 2
    # The void is emitted on both layers, but it is one SPD record: the void
    # association runs once per section, not once per patched layer.
    assert skill.count("bsCircleVoid(5 5 1)") == 2
    assert result["counts"]["native_voids"] == 1
    assert result["counts"]["native_void_emissions"] == 1


def test_generated_skill_never_uses_return_outside_prog(tmp_path):
    source = tmp_path / "board.spd"
    source.write_text(SPD, encoding="utf-8")

    generate_bundle(source, tmp_path / "batch")
    generate_bundle(source, tmp_path / "session", session_mode=True)

    for name in ("batch", "session"):
        skill = (tmp_path / name / "design.il").read_text(encoding="utf-8")
        assert _returns_outside_prog(skill) == []


def test_control_characters_are_rejected_before_they_reach_skill():
    from brd_spd.skill import _skill_string

    assert _skill_string("전원") == '"전원"'
    for value in ("PWR\x01", "PWR\x7f", "PWR\nGND"):
        with pytest.raises(ValueError, match="[Cc]ontrol character"):
            _skill_string(value)


def test_object_lookup_prefers_direct_find_and_restores_the_selection(tmp_path):
    source = tmp_path / "board.spd"
    source.write_text(SPD, encoding="utf-8")

    generate_bundle(source, tmp_path / "native", update_components=True)

    skill = (tmp_path / "native" / "design.il").read_text(encoding="utf-8")
    assert "axlDBFindByName('net netName)" in skill
    assert "axlDBFindByName('refdes refdes)" in skill
    assert "component = bsFindComponent(refdes)" in skill
    lines = skill.splitlines()
    selections = [index for index, line in enumerate(lines) if "axlSelectByName(" in line]
    assert selections
    for index in selections:
        before = lines[max(0, index - 3) : index]
        after = lines[index + 1 : index + 4]
        assert any("axlClearSelSet()" in line for line in before)
        assert any("axlSetFindFilter(" in line for line in before)
        assert any("axlGetSelSet()" in line for line in after)
        assert any("axlClearSelSet()" in line for line in after)


def test_every_emitted_polygon_path_returns_to_its_first_point(tmp_path):
    import re

    source = tmp_path / "polygons.spd"
    source.write_text(
        SPD.replace(
            "Circle2::PWR- 5mm 5mm 1mm",
            "Polygon2::PWR- 4mm 4mm 6mm 4mm 6mm 6mm 4mm 6mm",
        ),
        encoding="utf-8",
    )

    generate_bundle(source, tmp_path / "native")

    skill = (tmp_path / "native" / "design.il").read_text(encoding="utf-8")
    assert "bsBeginPolygon(" in skill and "bsPolygonVoid(" in skill
    point_lists = re.findall(r"list\(bsP\([^()]*\)(?: bsP\([^()]*\))*\)", skill)
    assert len(point_lists) == 2
    for text in point_lists:
        points = re.findall(r"bsP\(([^()]*)\)", text)
        assert len(points) >= 5
        assert points[0] == points[-1]


def test_static_etch_deletion_skips_and_logs_auto_generated_fill(tmp_path):
    source = tmp_path / "board.spd"
    source.write_text(SPD, encoding="utf-8")

    generate_bundle(source, tmp_path / "native")

    skill = (tmp_path / "native" / "design.il").read_text(encoding="utf-8")
    assert "if(!shape->shapeBoundary && !shape->shapeIsBoundary && !shape->shapeAuto then" in skill
    assert "Skipped %d dynamic/auto-generated ETCH shapes" in skill


def test_batch_mode_guards_an_already_open_base_design(tmp_path):
    source = tmp_path / "board.spd"
    source.write_text(SPD, encoding="utf-8")

    generate_bundle(source, tmp_path / "batch")
    generate_bundle(source, tmp_path / "session", session_mode=True)

    batch = (tmp_path / "batch" / "design.il").read_text(encoding="utf-8")
    session = (tmp_path / "session" / "design.il").read_text(encoding="utf-8")
    assert "if(axlCurrentDesign() then" in batch
    assert "base.brd already open; skipping axlOpenDesignForBatch" in batch
    assert 'bsError(sprintf(nil "Another design is already open: %s" axlCurrentDesign()))' in batch
    assert batch.index("axlCurrentDesign()") < batch.index("axlOpenDesignForBatch")
    assert "axlCurrentDesign()" not in session


def test_component_update_rotates_about_the_current_origin_then_moves(tmp_path):
    source = tmp_path / "board.spd"
    source.write_text(SPD, encoding="utf-8")

    generate_bundle(source, tmp_path / "native", update_components=True)

    skill = (tmp_path / "native" / "design.il").read_text(encoding="utf-8")
    rotate = (
        "axlTransformObject(symbol ?angle da "
        "?origin list(xCoord(symbol->xy) yCoord(symbol->xy)) ?allOrNone t)"
    )
    move = "axlTransformObject(symbol ?move list(dx dy) ?allOrNone t)"
    assert rotate in skill and move in skill
    # The move offset is read after the rotation, and each step is skipped when
    # it would be a no-op.
    assert skill.index(rotate) < skill.index("dx = bsL(x) - xCoord(symbol->xy)") < skill.index(move)
    assert "unless(da == 0.0" in skill
    assert "unless(dx == 0.0 && dy == 0.0" in skill


def test_unplaced_lookup_hit_falls_back_and_missing_components_fail_the_run(tmp_path):
    source = tmp_path / "board.spd"
    source.write_text(SPD, encoding="utf-8")

    generate_bundle(source, tmp_path / "batch", update_components=True)
    generate_bundle(source, tmp_path / "session", update_components=True, session_mode=True)

    for name in ("batch", "session"):
        skill = (tmp_path / name / "design.il").read_text(encoding="utf-8")
        assert "when(found && null(found->symbol)" in skill
        assert skill.index("when(found && null(found->symbol)") < skill.index("axlSetFindFilter(?enabled '(noall comps)")
        guard = skill.index('bsError(sprintf(nil "%d component(s) could not be updated" bsMissingComponents))')
        assert skill.index("bsApply()", skill.index("procedure(bsMain()")) < guard
        assert guard < skill.index("axlDBTransactionCommit(bsTxn)")


def test_layers_that_collide_after_prefix_removal_are_rejected(tmp_path):
    source = tmp_path / "collide.spd"
    source.write_text(
        """Title colliding layers
.Shape top_plane
Polygon1::PWR+ 0mm 0mm 10mm 0mm 10mm 10mm 0mm 10mm
.EndShape
.Shape plane_top
Polygon1::GND+ 0mm 0mm 10mm 0mm 10mm 10mm 0mm 10mm
.EndShape
Signal$TOP Thickness = 35um Material = COPPER
Plane$TOP Thickness = 35um Material = COPPER
PatchSignal$TOP Shape = top_plane Layer = Signal$TOP
PatchPlane$TOP Shape = plane_top Layer = Plane$TOP
""",
        encoding="utf-8",
    )
    output = tmp_path / "native"

    with pytest.raises(ValueError, match="Layer names collide after removing"):
        generate_bundle(source, output)

    assert not (output / "design.il").exists()
