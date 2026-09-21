from __future__ import annotations

import json
import sqlite3
from collections import Counter

import pytest

from brd_spd.spd import parse_spd


class Report:
    def __init__(self):
        self.counts = Counter()
        self.warnings = []

    def warn(self, code, message, line=None):
        self.warnings.append((code, message, line))


SAMPLE = """Title synthetic board
* Package description lines
.Package $Package
* Shape description lines
.Shape copper
Polygon1::PWR+ 0mm 0mm 1mm 0mm
+ 1mm 1mm 0mm 1mm
Circle2::PWR- 0.5mm 0.5mm 0.1mm
Box3::PWR+ 2mm 3mm 4mm 5mm
.EndShape
* Layer description lines
Signal$TOP Thickness = 35u Material = COPPER Width = 4mil
PatchSignal$TOP Shape = copper Layer = Signal$TOP
Medium$CORE Thickness = 0.001m Material = FR4
Signal$BOTTOM Thickness = 0.035mm Material = COPPER
* Node description lines
Node1!!A::PWR X = 1in Y = 2mil Layer = Signal$TOP PadStack = VIA AbsoluteRotation = 90
Node2::PWR X = 25.4mm Y = 0.0508mm Layer = Signal$BOTTOM
* Trace description lines
Trace1::PWR Fillet StartingNode = $Package.Node1::PWR EndingNode = Node2::PWR
+ Width = 0.2mm EndingWidth = 0.3mm
* Via description lines
Via1::PWR UpperNode = $Package.Node1!!A::PWR LowerNode = Node2::PWR PadStack = VIA
* PadStack collection description lines
.PadStackDef VIA 0.2mm 0.1mm Material = COPPER
.PadDef Signal$TOP
Regular Circle 0.3mm OffsetX = 0.01mm OffsetY = -0.02mm
.EndPadDef
.PadDef Signal$BOTTOM
Regular Box 0.4mm 0.5mm
.EndPadDef
.EndPadStackDef
.PadStackDef SMT 0.3mm Material = COPPER
.PadDef Signal$TOP
Regular Square 0.4mm
.EndPadDef
.EndPadStackDef
* Circuit description lines
.PartialCkt CAP ExtNode = 1 2
.EndPartialCkt
.Connect C1 CAP Usage = 0b1000
1 $Package.Node1!!A::PWR
2 $Package.Node2::PWR
.EndC
* ComponentDefinition description lines
.Part CAP Outline = 1mm 2mm Tags = "DISCRETE"
* Component description lines
.Component C1 10mm 20mm Rotation = 180 StartLayer = Signal$TOP
"""


def test_parse_spd_streams_geometry_and_metadata(tmp_path):
    source = tmp_path / "sample.spd"
    database = tmp_path / "geometry.sqlite"
    source.write_text(SAMPLE, encoding="utf-8")
    report = Report()

    metadata = parse_spd(source, database, report)

    assert metadata["title"] == "synthetic board"
    assert [layer["kind"] for layer in metadata["layers"]] == ["CONDUCTOR", "DIELECTRIC", "CONDUCTOR"]
    assert metadata["layers"][0]["thickness"] == pytest.approx(0.035)
    assert metadata["layers"][1]["thickness"] == pytest.approx(1.0)
    assert metadata["shape_layers"] == {"copper": "Signal$TOP"}
    assert metadata["padstacks"]["VIA"]["outer_radius"] == pytest.approx(0.2)
    assert metadata["padstacks"]["VIA"]["inner_radius"] == pytest.approx(0.1)
    assert metadata["padstacks"]["VIA"]["hole"] == pytest.approx(0.2)
    assert metadata["padstacks"]["VIA"]["pads"]["Signal$TOP"]["offset_x"] == pytest.approx(0.01)
    assert metadata["padstacks"]["VIA"]["pads"]["Signal$TOP"]["offset_y"] == pytest.approx(-0.02)
    assert metadata["padstacks"]["SMT"]["hole"] is None
    assert metadata["padstacks"]["VIA"]["pads"]["Signal$BOTTOM"] == {
        "kind": "Rectangle",
        "data": [0.4, 0.5],
    }
    assert metadata["parts"]["CAP"]["outline"] == [1.0, 2.0]
    assert metadata["components"]["C1"]["part"] == "CAP"

    with sqlite3.connect(database) as db:
        nodes = db.execute("SELECT id,x,y,rotation FROM nodes ORDER BY id").fetchall()
        trace = db.execute("SELECT start,end,width,attrs FROM traces").fetchone()
        via = db.execute("SELECT upper,lower,padstack FROM vias").fetchone()
        shapes = db.execute("SELECT id,polarity,kind,data,line FROM shapes ORDER BY line").fetchall()
        connection = db.execute("SELECT ref,part,pins FROM connections").fetchone()

    assert nodes[0][0] == "Node1!!A::PWR"
    assert nodes[0][1:] == pytest.approx((25.4, 0.0508, 90.0))
    assert trace[:3] == ("Node1!!A::PWR", "Node2::PWR", 0.2)
    assert json.loads(trace[3])["flags"] == ["Fillet"]
    assert via == ("Node1!!A::PWR", "Node2::PWR", "VIA")
    assert [row[2] for row in shapes] == ["Polygon", "Circle", "Polygon"]
    assert json.loads(shapes[2][3]) == [2.0, 3.0, 6.0, 3.0, 6.0, 8.0, 2.0, 8.0]
    assert json.loads(connection[2]) == {"1": "Node1!!A::PWR", "2": "Node2::PWR"}
    assert report.counts["spd_nodes"] == 2


def test_invalid_known_geometry_is_not_silently_skipped(tmp_path):
    source = tmp_path / "bad.spd"
    source.write_text("* Shape description lines\n.Shape bad\nCircle1::N+ 0mm 1mm\n.EndShape\n", encoding="utf-8")

    with pytest.raises(ValueError, match="circle"):
        parse_spd(source, tmp_path / "bad.sqlite", Report())


def test_unknown_shape_record_is_reported(tmp_path):
    source = tmp_path / "unknown.spd"
    source.write_text(
        "* Shape description lines\n.Shape bad\n"
        "Spline1::N+ 0mm 0mm 1mm 1mm\nSpline2::N+ 1mm 1mm 2mm 2mm\n.EndShape\n",
        encoding="utf-8",
    )
    report = Report()

    parse_spd(source, tmp_path / "unknown.sqlite", report)

    assert report.warnings[0][0] == "SPD_UNSUPPORTED_SHAPE_RECORD"
    assert report.warnings[0][2] == 3
    assert len(report.warnings) == 2


def test_physical_records_do_not_require_description_comments(tmp_path):
    source = tmp_path / "bare.spd"
    source.write_text(
        """Title bare records
Signal$TOP Thickness = 35um Material = COPPER Width = 4mil
Medium$CORE Thickness = 0.1mm Material = FR4
Node1::N X = 0mm Y = 0mm Layer = Signal$TOP
Node2::N X = 1mm Y = 0mm Layer = Signal$TOP
Trace1::N StartingNode = Node1::N EndingNode = Node2::N
Via1::N UpperNode = Node1::N LowerNode = Node2::N PadStack = V
""",
        encoding="utf-8",
    )

    database = tmp_path / "bare.sqlite"
    metadata = parse_spd(source, database, Report())

    assert [layer["kind"] for layer in metadata["layers"]] == ["CONDUCTOR", "DIELECTRIC"]
    assert metadata["counts"]["nodes"] == 2
    assert metadata["counts"]["traces"] == 1
    assert metadata["counts"]["vias"] == 1
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT width FROM traces").fetchone()[0] == pytest.approx(0.1016)


def test_trace_without_explicit_or_layer_default_width_fails(tmp_path):
    source = tmp_path / "missing-width.spd"
    source.write_text(
        """Signal$TOP Thickness = 35um Material = COPPER
Node1::N X = 0mm Y = 0mm Layer = Signal$TOP
Node2::N X = 1mm Y = 0mm Layer = Signal$TOP
Trace1::N StartingNode = Node1::N EndingNode = Node2::N
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no layer default Width"):
        parse_spd(source, tmp_path / "missing-width.sqlite", Report())


def test_incomplete_bare_physical_record_fails(tmp_path):
    source = tmp_path / "incomplete.spd"
    source.write_text("Trace1::N StartingNode = Node1::N Width = 0.1mm\n", encoding="utf-8")

    with pytest.raises(ValueError, match="incomplete trace"):
        parse_spd(source, tmp_path / "incomplete.sqlite", Report())


def test_non_utf8_input_reports_source_line(tmp_path):
    source = tmp_path / "encoded.spd"
    source.write_bytes(b"Title valid\nTitle bad \xff\n")

    with pytest.raises(ValueError, match=r"line 2: SPD is not valid UTF-8"):
        parse_spd(source, tmp_path / "encoded.sqlite", Report())


def test_duplicate_component_and_connection_pin_are_rejected(tmp_path):
    duplicate_component = tmp_path / "duplicate-component.spd"
    duplicate_component.write_text(
        ".Component C1 0mm 0mm\n.Component C1 1mm 1mm\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate component 'C1'"):
        parse_spd(duplicate_component, tmp_path / "duplicate-component.sqlite", Report())

    duplicate_pin = tmp_path / "duplicate-pin.spd"
    duplicate_pin.write_text(
        ".Connect C1 CAP\n1 Node1::N\n1 Node2::N\n.EndC\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate pin '1'"):
        parse_spd(duplicate_pin, tmp_path / "duplicate-pin.sqlite", Report())


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (".Shape copper\nPolygon1::N+ 0mm 0mm 1mm 0mm 0mm 1mm\n", "unterminated .Shape"),
        (".PadStackDef VIA 0.2mm 0.1mm\n", "unterminated .PadStackDef"),
    ],
)
def test_unterminated_known_sections_are_rejected(tmp_path, body, message):
    source = tmp_path / "unterminated.spd"
    source.write_text(body, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        parse_spd(source, tmp_path / "unterminated.sqlite", Report())
