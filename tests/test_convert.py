import json
import tempfile
import unittest
from pathlib import Path

from lxml import etree as E

from brd_spd.convert import convert, validate_xml


SPD = """Title synthetic conversion test
.Package $Package
.Shape shape_top
Polygon1::PWR+ 0mm 0mm 10mm 0mm 10mm 10mm 0mm 10mm
Circle2::PWR- 5mm 5mm 1mm
.EndShape
Signal$TOP Thickness = 35u Material = COPPER
PatchSignal$TOP Shape = shape_top Layer = Signal$TOP
Medium$CORE Thickness = 1mm Material = FR4
Signal$BOTTOM Thickness = 35u Material = COPPER
Node1!!1::PWR X = 1mm Y = 2mm Layer = Signal$TOP PadStack = PAD
Node2!!2::GND X = 2mm Y = 2mm Layer = Signal$TOP PadStack = PAD
Node3::PWR X = 1mm Y = 3mm Layer = Signal$TOP
Node4::PWR X = 1mm Y = 3mm Layer = Signal$BOTTOM
Trace1::PWR StartingNode = Node1!!1::PWR EndingNode = Node3::PWR Width = 0.2mm
Via1::PWR UpperNode = Node3::PWR LowerNode = Node4::PWR PadStack = VIA
.PadStackDef PAD 0.25mm
.PadDef Signal$TOP
Regular Circle 0.25mm
.EndPadDef
.EndPadStackDef
.PadStackDef VIA 0.15mm 0.1mm
.PadDef Signal$TOP
Regular Circle 0.3mm
.EndPadDef
.PadDef Signal$BOTTOM
Regular Circle 0.3mm
.EndPadDef
.EndPadStackDef
.EndPackage
.Connect C1 CAP
1 $Package.Node1!!1::PWR
2 $Package.Node2!!2::GND
.EndC
.Part CAP Outline = 1mm 0.5mm
.Component C1 1.5mm 2mm StartLayer = Signal$TOP Rotation = 90
"""


class ConversionTests(unittest.TestCase):
    def test_actual_conversion_preserves_geometry_and_reports_losses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "board.spd", root / "board.xml"
            source.write_text(SPD, encoding="utf-8")
            result = convert(source, output)
            tree = E.parse(str(output))
            ns = {"i": "http://webstds.ipc.org/2581"}
            self.assertEqual(tree.xpath("count(//i:Cutout)", namespaces=ns), 1)
            self.assertEqual(tree.xpath("count(//i:Cutout/i:PolyStepCurve)", namespaces=ns), 4)
            self.assertEqual(tree.xpath("//i:Pad/i:Circle/@diameter", namespaces=ns), ["0.5", "0.5", "0.6", "0.6"])
            self.assertEqual(tree.xpath("//i:Hole/@diameter", namespaces=ns), ["0.2"])
            self.assertEqual(result["counts"]["exported_components"], 1)
            self.assertEqual(result["counts"]["exported_traces"], 1)
            self.assertIn("BOARD_OUTLINE_UNAVAILABLE", result["warnings"])
            self.assertFalse(result["allegro_import_verified"])
            self.assertEqual(source.read_text(), SPD)
            for filename in ("IPC-2581B1.xsd",):
                schema = Path("schemas") / filename
                if schema.is_file():
                    self.assertTrue(validate_xml(output, schema)["valid"])

    def test_failure_and_overwrite_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "bad.spd", root / "new.xml"
            source.write_text(SPD.replace("EndingNode = Node3::PWR", "EndingNode = Missing"))
            with self.assertRaises(ValueError):
                convert(source, output)
            self.assertFalse(output.exists())
            self.assertEqual(json.loads(output.with_suffix(".xml.report.json").read_text())["status"], "failed")
            output.write_text("keep")
            with self.assertRaises(ValueError):
                convert(source, output)
            self.assertEqual(output.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
