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
