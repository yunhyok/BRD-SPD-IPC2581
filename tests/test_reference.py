from pathlib import Path

import pytest

from lxml import etree

from brd_spd.reference import IPC_2581_NAMESPACE, normalized_reference, read_reference
from brd_spd.report import Report


def _reference_xml(units="MICRON", tail=b"</LayerFeature></Step></CadData></Ecad></IPC-2581>"):
    ns = IPC_2581_NAMESPACE
    head = f'''<?xml version="1.0" encoding="UTF-8"?>
<IPC-2581 xmlns="{ns}" revision="B">
  <Content roleRef="Owner">
    <FunctionMode mode="ASSEMBLY" level="3"/><StepRef name="board"/><LayerRef name="TOP"/>
    <DictionaryStandard units="MICRON">
      <EntryStandard id="ROUND"><Circle diameter="450"><FillDescRef id="SOLID"/></Circle></EntryStandard>
      <EntryStandard id="RECT"><RectCenter width="2" height="1"><FillDescRef id="SOLID"/></RectCenter></EntryStandard>
      <EntryStandard id="CONTOUR"><Contour><Polygon><PolyBegin x="0" y="0"/><PolyStepSegment x="1" y="0"/><LineDescRef id="EDGE"/><FillDescRef id="SOLID"/></Polygon></Contour></EntryStandard>
    </DictionaryStandard>
    <DictionaryUser units="MICRON"/><DictionaryFont units="MICRON"/>
    <DictionaryLineDesc units="MICRON"><EntryLineDesc id="EDGE"><LineDesc lineEnd="ROUND" lineWidth="1"/></EntryLineDesc></DictionaryLineDesc>
    <DictionaryFillDesc units="MICRON"><EntryFillDesc id="SOLID"><FillDesc fillProperty="FILL"><ColorRef id="COLOR_(TOP)"/></FillDesc></EntryFillDesc></DictionaryFillDesc>
    <DictionaryColor><EntryColor id="COLOR_(TOP)"><Color r="1" g="2" b="3"/></EntryColor></DictionaryColor>
  </Content>
  <LogisticHeader><Role id="Owner" roleFunction="SENDER"/><Enterprise id="E" code="E"/><Person name="P" enterpriseRef="E" roleRef="Owner"/></LogisticHeader>
  <HistoryRecord number="1" origination="2026-01-01T00:00:00" software="test" lastChange="2026-01-01T00:00:00"><FileRevision fileRevisionId="1" comment=""><SoftwarePackage name="test" revision="1" vendor="test"/></FileRevision></HistoryRecord>
  <Bom name="stale"><BomHeader assembly="board" revision="1"/></Bom>
  <Ecad name="board"><CadHeader units="{units}"/><CadData>
    <Layer name="TOP" layerFunction="CONDUCTOR" side="TOP" polarity="POSITIVE"/>
    <Stackup name="PRIMARY" overallThickness="1000" tolPlus="0" tolMinus="0" whereMeasured="METAL"/>
    <Step name="board"><PadStackDef name="PAD"><PadstackPadDef layerRef="TOP" padUse="REGULAR"><Location x="0" y="0"/><Circle diameter="1"/></PadstackPadDef></PadStackDef><Datum x="0" y="0"/><Profile><Polygon><PolyBegin x="0" y="0"/><PolyStepSegment x="1" y="0"/></Polygon></Profile>
      <Package name="PKG" type="OTHER" pinOneOrientation="OTHER"><Outline><Polygon><PolyBegin x="0" y="0"/><PolyStepSegment x="1" y="0"/></Polygon><LineDesc lineEnd="ROUND" lineWidth="1"/></Outline><LandPattern><Pad padstackDefRef="PAD"><Location x="0" y="0"/><Circle diameter="1"/></Pad></LandPattern><SilkScreen><Marking markingUsage="NONE"><Location x="0" y="0"/><Polyline><PolyBegin x="0" y="0"/><PolyStepSegment x="2" y="3"/><LineDescRef id="EDGE"/></Polyline></Marking></SilkScreen></Package>
      <Component refDes="C1" packageRef="PKG" part="CAP" layerRef="TOP" mountType="SMT"><Xform rotation="270" mirror="true"/><Location x="10" y="20"/></Component>
      <LayerFeature layerRef="TOP">'''.encode()
    return head + tail


def test_reads_metadata_and_stops_before_geometry(tmp_path: Path):
    source = tmp_path / "reference.xml"
    # The unread suffix is deliberately malformed: reaching it would fail.
    source.write_bytes(_reference_xml(tail=b"<this-is-not-valid"))
    report = {}
    progress = []

    result = read_reference(source, report=report, progress=progress.append)

    assert result["revision"] == "B"
    assert result["namespace_uri"] == IPC_2581_NAMESPACE
    assert result["namespaces"][None] == IPC_2581_NAMESPACE
    assert result["units"] == "MICRON"
    assert result["mm_scale"] == 1000.0
    assert result["step_name"] == "board"
    assert result["layers"] == [{"name": "TOP", "layerFunction": "CONDUCTOR", "side": "TOP", "polarity": "POSITIVE"}]
    assert b'xmlns="http://webstds.ipc.org/2581"' in result["profile"]
    assert b'<Package' in result["packages"]["PKG"]
    assert b'<PadStackDef' in result["padstack_defs"]["PAD"]
    assert result["components"]["C1"]["packageRef"] == "PKG"
    assert result["components"]["C1"]["part"] == "CAP"
    assert result["components"]["C1"]["location"] == {"x": "10", "y": "20"}
    assert result["components"]["C1"]["rotation"] == "270"
    assert result["components"]["C1"]["mirror"] == "true"
    assert result["bom_present"] is True
    assert result["bom_names"] == ["stale"]
    assert result["datum_attrs"] == {"x": "0", "y": "0"}
    assert result["profile_has_geometry"] is True
    assert result["stopped_at_layer_features"] is True
    assert any(item["code"] == "REFERENCE_INVENTORY" for item in report["findings"])
    assert progress[-1].endswith("1 packages, 1 components")


@pytest.mark.parametrize(
    ("units", "scale"), [("MILLIMETER", 1.0), ("MICRON", 1000.0), ("INCH", 1.0 / 25.4)]
)
def test_mm_scale(tmp_path: Path, units: str, scale: float):
    source = tmp_path / f"{units}.xml"
    source.write_bytes(_reference_xml(units=units))
    assert read_reference(source)["mm_scale"] == pytest.approx(scale)


def test_rejects_dtd_and_entities(tmp_path: Path):
    source = tmp_path / "hostile.xml"
    source.write_text(
        '<?xml version="1.0"?><!DOCTYPE IPC-2581 [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        f'<IPC-2581 xmlns="{IPC_2581_NAMESPACE}" revision="B">&x;</IPC-2581>',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="DTD/entity"):
        read_reference(source)


def test_recovers_mislabeled_cp949_reference(tmp_path: Path):
    source = tmp_path / "cp949.xml"
    xml = _reference_xml().replace(b'name="P"', 'name="\ucd5c\uc724\ud601"'.encode("cp949"))
    source.write_bytes(xml)

    result = read_reference(source)

    assert "\ucd5c\uc724\ud601".encode("utf-8") in result["logistic_header"]
    assert result["encoding"] == "cp949"
    assert any("CP949" in warning for warning in result["warnings"])


def test_recovers_cp949_reference_without_declaration(tmp_path: Path):
    source = tmp_path / "undeclared.xml"
    xml = _reference_xml().replace(b'<?xml version="1.0" encoding="UTF-8"?>\n', b"")
    xml = xml.replace(b'name="P"', 'name="\ucd5c\uc724\ud601"'.encode("cp949"))
    source.write_bytes(xml)

    result = read_reference(source)

    assert "\ucd5c\uc724\ud601".encode("utf-8") in result["logistic_header"]
    assert result["encoding"] == "cp949"


def test_structural_error_is_not_retried_as_cp949(tmp_path: Path):
    source = tmp_path / "broken.xml"
    source.write_bytes(_reference_xml().replace(b'<Content roleRef="Owner">', b'<Content roleRef="Owner"'))
    with pytest.raises(etree.XMLSyntaxError):
        read_reference(source)


def test_reference_findings_reach_the_conversion_report(tmp_path: Path):
    source = tmp_path / "cp949.xml"
    xml = _reference_xml().replace(b'name="P"', 'name="\ucd5c\uc724\ud601"'.encode("cp949"))
    source.write_bytes(xml)
    report = Report(tmp_path / "result.xml")

    read_reference(source, report)

    inventory = report.data["info"]["REFERENCE_INVENTORY"]["samples"][0]
    assert inventory["layers"] == 1 and inventory["units"] == "MICRON"
    metadata = report.warnings["REFERENCE_METADATA_ONLY"]["samples"]
    assert any("CP949" in sample["message"] and sample["reference"] for sample in metadata)
    saved = report.save("success")
    assert "REFERENCE_INVENTORY" in saved["info"]
    assert "[REFERENCE_INVENTORY]" in (tmp_path / "result.xml.log").read_text(encoding="utf-8")


def test_rejects_multiple_steps_before_geometry(tmp_path: Path):
    source = tmp_path / "multiple.xml"
    source.write_bytes(
        _reference_xml().replace(
            b'<Step name="board">',
            b'<Step name="panel"></Step><Step name="board">',
        )
    )
    with pytest.raises(ValueError, match="multiple Step"):
        read_reference(source)


def test_normalizes_description_refs_without_mutating_raw_donor(tmp_path: Path):
    source = tmp_path / "reference.xml"
    source.write_bytes(_reference_xml())
    raw = read_reference(source)
    report = {}

    cooked = normalized_reference(raw, report)

    assert b"FillDescRef" in raw["dictionaries"]["standard"]
    assert b"LineDescRef" in raw["dictionaries"]["standard"]
    assert b"FillDescRef" not in cooked["dictionaries"]["standard"]
    assert b"LineDescRef" not in cooked["dictionaries"]["standard"]
    standard = etree.fromstring(cooked["dictionaries"]["standard"])
    tags = [node.tag.rsplit("}", 1)[-1] for node in standard.iter()]
    assert tags.count("FillDesc") == 3
    assert tags.count("LineDesc") == 1
    assert cooked["schema_normalizations"]["description_refs_inlined"] >= 4
    assert cooked["schema_normalizations"]["color_ids_sanitized"] >= 1
    assert cooked["schema_normalizations"]["two_point_polylines_as_lines"] == 1
    assert b"COLOR__TOP_" in cooked["dictionaries"]["color"]
    assert b"COLOR_(TOP)" not in cooked["dictionaries"]["standard"]
    package = etree.fromstring(cooked["packages"]["PKG"])
    lines = [node for node in package.iter() if node.tag.rsplit("}", 1)[-1] == "Line"]
    assert [dict(node.attrib) for node in lines] == [
        {"startX": "0", "startY": "0", "endX": "2", "endY": "3"}
    ]
    assert any(item["code"] == "REFERENCE_SCHEMA_NORMALIZED" for item in report["findings"])
