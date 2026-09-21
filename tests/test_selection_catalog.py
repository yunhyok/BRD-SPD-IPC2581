from __future__ import annotations

from concurrent.futures import CancelledError

import pytest

from brd_spd.selection_catalog import scan_catalog


def test_catalog_contains_only_mapped_positive_conductor_planes(tmp_path):
    source = tmp_path / "board.spd"
    source.write_text(
        """.Shape top_plane
Polygon1::PWR+ 0mm 0mm 1mm 0mm 0mm 1mm
Circle2::GND+ 0mm 0mm 1mm
Polygon3::NEGATIVE_ONLY- 0mm 0mm 1mm 0mm 0mm 1mm
.EndShape
.Shape bottom_plane
Box1::BOTTOM+ 0mm 0mm 1mm 1mm
.EndShape
.Shape dielectric_plane
Polygon1::DIEL+ 0mm 0mm 1mm 0mm 0mm 1mm
.EndShape
.Shape unmapped_plane
Polygon1::UNMAPPED+ 0mm 0mm 1mm 0mm 0mm 1mm
.EndShape
Signal$TOP Thickness = 35um Material = COPPER
Medium$CORE Thickness = 1mm Material = FR4
Signal$BOTTOM Thickness = 35um Material = COPPER
PatchSignal$TOP Shape = top_plane Layer = Signal$TOP
PatchSignal$BOTTOM Shape = bottom_plane Layer = Signal$BOTTOM
PatchMedium$CORE Shape = dielectric_plane Layer = Medium$CORE
Node1::NODE_ONLY X = 0mm Y = 0mm Layer = Signal$TOP
Trace1::TRACE_ONLY StartingNode = Node1::NODE_ONLY EndingNode = Node1::NODE_ONLY Width = 1mm
""",
        encoding="utf-8",
    )

    assert scan_catalog(source) == {
        "layers": ["TOP", "BOTTOM"],
        "nets": ["BOTTOM", "GND", "PWR"],
        "nets_by_layer": {"TOP": ["GND", "PWR"], "BOTTOM": ["BOTTOM"]},
    }


def test_continuations_unicode_and_native_aliases_keep_source_order(tmp_path):
    source = tmp_path / "unicode.spd"
    source.write_text(
        """.Shape 두번째
Polygon1::전원+ 0mm 0mm
+ 1mm 0mm 0mm 1mm
.EndShape
.Shape 첫번째
Circle1::신호+ 0mm 0mm 1mm
.EndShape
.Shape 별칭
Polygon1::보조+ 0mm 0mm 1mm 0mm 0mm 1mm
.EndShape
Plane$L2
+ Thickness = 35um Material = COPPER
Signal$L1 Thickness = 35um Material = COPPER
Signal$L2 Thickness = 35um Material = COPPER
PatchPlane$L2 sHaPe = 두번째
+ LaYeR = Plane$L2
PatchSignal$L1 Shape = 첫번째 Layer = Signal$L1
PatchSignal$L2 Shape = 별칭 Layer = Signal$L2
""",
        encoding="utf-8",
    )

    result = scan_catalog(source)

    assert result["layers"] == ["L2", "L1"]
    assert result["nets"] == ["보조", "신호", "전원"]
    assert result["nets_by_layer"] == {
        "L2": ["보조", "전원"],
        "L1": ["신호"],
    }


def test_catalog_is_strict_utf8_and_rejects_unterminated_shape(tmp_path):
    invalid = tmp_path / "invalid.spd"
    invalid.write_bytes(b".Shape plane\nPolygon1::N+ \xff\n.EndShape\n")
    with pytest.raises(ValueError, match=r"line 2: SPD is not valid UTF-8"):
        scan_catalog(invalid)

    unterminated = tmp_path / "unterminated.spd"
    unterminated.write_text(".Shape plane\nPolygon1::N+ 0mm 0mm\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"unterminated \.Shape"):
        scan_catalog(unterminated)


def test_cancellation_is_checked_before_open_and_after_final_progress(tmp_path):
    missing = tmp_path / "missing.spd"
    with pytest.raises(CancelledError):
        scan_catalog(missing, cancelled=lambda: True)

    source = tmp_path / "board.spd"
    source.write_text("", encoding="utf-8")
    cancel = False

    def on_progress(message):
        nonlocal cancel
        if message.endswith("100%"):
            cancel = True

    with pytest.raises(CancelledError):
        scan_catalog(source, progress=on_progress, cancelled=lambda: cancel)

    many_lines = tmp_path / "many-lines.spd"
    many_lines.write_text("* ignored\n" * 20_000, encoding="utf-8")
    checks = 0

    def cancel_during_scan():
        nonlocal checks
        checks += 1
        return checks >= 3

    with pytest.raises(CancelledError):
        scan_catalog(many_lines, cancelled=cancel_during_scan)
    assert checks == 3


def test_source_change_during_scan_is_rejected(tmp_path):
    source = tmp_path / "board.spd"
    source.write_text("Signal$TOP Thickness = 1mm\n", encoding="utf-8")
    changed = False

    def change_after_signature(message):
        nonlocal changed
        if not changed and message.endswith("0%"):
            source.write_text(
                "Signal$TOP Thickness = 1mm\n# changed\n",
                encoding="utf-8",
            )
            changed = True

    with pytest.raises(RuntimeError, match="changed"):
        scan_catalog(source, progress=change_after_signature)
