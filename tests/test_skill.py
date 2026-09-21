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
