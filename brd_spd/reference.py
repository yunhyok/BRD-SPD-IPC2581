"""Bounded-memory reader for IPC-2581 reference metadata.

The reference file is a donor for dictionaries, package geometry, stackup,
profile, and component metadata.  Copper features are deliberately not read:
the parser stops at the first ``LayerFeature`` in the step.
"""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from lxml import etree


IPC_2581_NAMESPACE = "http://webstds.ipc.org/2581"
_MM_SCALE = {"MILLIMETER": 1.0, "MICRON": 1000.0, "INCH": 1.0 / 25.4}
_DICTIONARIES = {
    "DictionaryStandard": "standard",
    "DictionaryUser": "user",
    "DictionaryLineDesc": "line",
    "DictionaryFillDesc": "fill",
    "DictionaryColor": "color",
    "DictionaryFont": "font",
    "DictionaryFirmware": "firmware",
}
_SERIALIZED_ROOTS = {
    "Content",
    "LogisticHeader",
    "HistoryRecord",
    "CadHeader",
    "Stackup",
    "PadStackDef",
    "Datum",
    "Profile",
    "Package",
    "Component",
}
_DECLARATION = re.compile(br"<\?xml\s+[^>]*encoding\s*=\s*['\"]([^'\"]+)", re.I)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml(element: etree._Element) -> bytes:
    """Serialize a self-contained namespace-qualified subtree."""
    return etree.tostring(element, encoding="UTF-8", with_tail=False)


def _child(element: etree._Element, name: str) -> etree._Element | None:
    for item in element:
        if isinstance(item.tag, str) and _local(item.tag) == name:
            return item
    return None


def _emit(report: Any, level: str, code: str, message: str, **details: Any) -> None:
    """Add a finding without requiring a particular report implementation."""
    if report is None:
        return
    finding = {"level": level, "code": code, "message": message, **details}
    if isinstance(report, list):
        report.append(finding)
    elif isinstance(report, dict):
        report.setdefault("findings", []).append(finding)
    else:
        method_name = "warn" if level == "warning" else level
        method = getattr(report, method_name, None) or getattr(report, "add", None)
        if method:
            try:
                method(code, message, **details)
            except TypeError:
                try:
                    method(code, message)
                except TypeError:
                    method(finding)


def _reject_dtd_or_entities(path: Path) -> None:
    # XML permits a DTD only in the prolog.  One MiB is deliberately generous
    # for finding the root start while keeping this guard bounded.
    prefix = path.open("rb").read(1024 * 1024)
    upper = prefix.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("DTD/entity declarations are not allowed in IPC reference input")


def _declared_encoding(path: Path) -> str | None:
    with path.open("rb") as source:
        match = _DECLARATION.search(source.read(1024))
    return match.group(1).decode("ascii", "replace") if match else None


def _component(element: etree._Element) -> dict[str, Any]:
    attrs = dict(element.attrib)
    xform = _child(element, "Xform")
    location = _child(element, "Location")
    nonstandard = [dict(child.attrib) for child in element
                   if isinstance(child.tag, str) and _local(child.tag) == "NonstandardAttribute"]
    result: dict[str, Any] = {
        "attrs": attrs,
        "packageRef": attrs.get("packageRef"),
        "part": attrs.get("part"),
        "layerRef": attrs.get("layerRef"),
        "location": dict(location.attrib) if location is not None else {},
        "xform": dict(xform.attrib) if xform is not None else {},
        "nonstandard_attributes": nonstandard,
    }
    if xform is not None:
        result.update({key: value for key, value in xform.attrib.items()
                       if key in {"rotation", "mirror", "scale", "face"}})
    return result


def _empty(path: Path, encoding: str | None) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "encoding": encoding,
        "namespace_uri": None,
        "namespaces": {},
        "root_attrs": {},
        "revision": None,
        "content": None,
        "content_attrs": {},
        "dictionaries": {name: None for name in _DICTIONARIES.values()},
        "logistic_header": None,
        "history_record": None,
        "bom_present": False,
        "bom_names": [],
        "ecad_attrs": {},
        "cad_header": None,
        "units": None,
        "mm_scale": None,
        "layers": [],
        "stackups": [],
        "padstack_defs": {},
        "step_name": None,
        "step_attrs": {},
        "datum": None,
        "datum_attrs": {},
        "profile": None,
        "profile_has_geometry": False,
        "packages": {},
        "components": {},
        "stopped_at_layer_features": False,
        "warnings": [
            "Reference data is metadata only; its routed copper and unchanged geometry are not authoritative.",
            "The reference does not supply SPD simulation settings, constraints, or DRC rules.",
            "Reference BOM data is inventoried but is not copied because it may be stale.",
        ],
    }


def _read_once(path: Path, encoding: str | None,
               progress: Callable[[str], None] | None) -> dict[str, Any]:
    result = _empty(path, encoding or _declared_encoding(path))
    stack: list[str] = []
    protected_depth: int | None = None
    events = 0
    step_count = 0

    if progress:
        progress("Reference metadata: reading header, dictionaries, and placements")

    with path.open("rb") as source:
        context = etree.iterparse(
            source,
            events=("start", "end"),
            huge_tree=True,
            resolve_entities=False,
            load_dtd=False,
            no_network=True,
            remove_comments=False,
            encoding=encoding,
        )
        for event, element in context:
            if not isinstance(element.tag, str):
                continue
            tag = _local(element.tag)
            if event == "start":
                stack.append(tag)
                if tag == "IPC-2581":
                    docinfo = element.getroottree().docinfo
                    if docinfo.doctype:
                        raise ValueError("DTD/entity declarations are not allowed in IPC reference input")
                    result["root_attrs"] = dict(element.attrib)
                    result["revision"] = element.attrib.get("revision")
                    result["namespaces"] = dict(element.nsmap)
                    result["namespace_uri"] = element.nsmap.get(None) or (
                        element.tag.split("}", 1)[0][1:] if element.tag.startswith("{") else None
                    )
                elif tag == "Ecad":
                    result["ecad_attrs"] = dict(element.attrib)
                elif tag == "Bom":
                    result["bom_present"] = True
                    if element.attrib.get("name"):
                        result["bom_names"].append(element.attrib["name"])
                elif tag == "Layer":
                    result["layers"].append(dict(element.attrib))
                elif tag == "Step":
                    step_count += 1
                    if step_count > 1:
                        raise ValueError(
                            "Reference contains multiple Step elements before its first LayerFeature; "
                            "the metadata donor is ambiguous"
                        )
                    result["step_attrs"] = dict(element.attrib)
                    result["step_name"] = element.attrib.get("name")
                elif tag == "LayerFeature":
                    result["stopped_at_layer_features"] = True
                    break
                if protected_depth is None and tag in _SERIALIZED_ROOTS:
                    protected_depth = len(stack)
                continue

            # End event: the element is complete and safe to serialize.
            if tag == "Content":
                result["content"] = _xml(element)
                result["content_attrs"] = dict(element.attrib)
            elif tag in _DICTIONARIES:
                result["dictionaries"][_DICTIONARIES[tag]] = _xml(element)
            elif tag == "LogisticHeader":
                result["logistic_header"] = _xml(element)
            elif tag == "HistoryRecord":
                result["history_record"] = _xml(element)
            elif tag == "CadHeader":
                result["cad_header"] = _xml(element)
                result["units"] = element.attrib.get("units")
            elif tag == "Stackup":
                result["stackups"].append(_xml(element))
            elif tag == "PadStackDef":
                name = element.attrib.get("name")
                if name:
                    result["padstack_defs"][name] = _xml(element)
            elif tag == "Datum":
                result["datum"] = _xml(element)
                result["datum_attrs"] = dict(element.attrib)
            elif tag == "Profile":
                result["profile"] = _xml(element)
                result["profile_has_geometry"] = _child(element, "Polygon") is not None
            elif tag == "Package":
                name = element.attrib.get("name")
                if name:
                    result["packages"][name] = _xml(element)
            elif tag == "Component":
                refdes = element.attrib.get("refDes")
                if refdes:
                    result["components"][refdes] = _component(element)

            closing_protected = protected_depth == len(stack)
            stack.pop()
            if closing_protected:
                protected_depth = None
            if protected_depth is None:
                element.clear(keep_tail=True)
                parent = element.getparent()
                if parent is not None:
                    while element.getprevious() is not None:
                        del parent[0]

            events += 1
            if progress and events % 100000 == 0:
                progress(f"Reference metadata: {source.tell() / max(path.stat().st_size, 1):.0%}")

    units = (result["units"] or "").upper()
    if units not in _MM_SCALE:
        raise ValueError(f"Unsupported or missing IPC-2581 CadHeader units: {result['units']!r}")
    result["mm_scale"] = _MM_SCALE[units]
    if result["namespace_uri"] != IPC_2581_NAMESPACE:
        result["warnings"].append(
            f"Unexpected IPC-2581 namespace: {result['namespace_uri']!r}"
        )
    if not result["stopped_at_layer_features"]:
        result["warnings"].append("No LayerFeature was found in the reference step.")
    if not result["profile_has_geometry"]:
        result["warnings"].append(
            "The reference Profile has no polygon; a board outline must come from SPD data or be reported missing."
        )
    datum = result["datum_attrs"]
    if datum and (float(datum.get("x", 0.0)) != 0.0 or float(datum.get("y", 0.0)) != 0.0):
        result["warnings"].append(
            "The reference Datum is nonzero; generated SPD coordinates must be transformed into its coordinate frame."
        )
    if progress:
        progress(
            f"Reference metadata: {len(result['packages'])} packages, "
            f"{len(result['components'])} components"
        )
    return result


def read_reference(path: Path, report: Any = None,
                   progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Read reusable metadata from an IPC-2581 reference without reading copper.

    ``mm_scale`` converts SPD millimetres to the reference coordinate units.
    For example, a value of 1.0 mm becomes 1000.0 when ``units`` is MICRON.
    """
    source = Path(path)
    _reject_dtd_or_entities(source)
    try:
        result = _read_once(source, None, progress)
    except etree.XMLSyntaxError as exc:
        declared = (_declared_encoding(source) or "").upper().replace("_", "-")
        message = str(exc).lower()
        if declared not in {"UTF-8", "UTF8"} or "utf-8" not in message:
            raise
        # Some Cadence exports declare UTF-8 but contain Windows Korean text.
        # Reparse with CP949 while preserving all XML text as UTF-8 on output.
        result = _read_once(source, "cp949", progress)
        result["warnings"].append(
            "The reference declared UTF-8 but required CP949 decoding; serialized metadata was normalized to UTF-8."
        )

    for warning in result["warnings"]:
        _emit(report, "warning", "REFERENCE_METADATA_ONLY", warning,
              reference=str(source.resolve()))
    _emit(report, "info", "REFERENCE_INVENTORY", "Reference metadata loaded.",
          layers=len(result["layers"]), packages=len(result["packages"]),
          components=len(result["components"]), units=result["units"])
    return result


def _description_maps(reference: dict[str, Any]) -> dict[str, dict[str, etree._Element]]:
    maps: dict[str, dict[str, etree._Element]] = {
        "FillDescRef": {},
        "LineDescRef": {},
    }
    specifications = (
        ("fill", "EntryFillDesc", "FillDesc", "FillDescRef"),
        ("line", "EntryLineDesc", "LineDesc", "LineDescRef"),
    )
    for dictionary_key, entry_name, value_name, ref_name in specifications:
        raw = reference.get("dictionaries", {}).get(dictionary_key)
        if not raw:
            continue
        root = etree.fromstring(raw, parser=etree.XMLParser(resolve_entities=False, no_network=True))
        for entry in root.iter():
            if not isinstance(entry.tag, str) or _local(entry.tag) != entry_name:
                continue
            identifier = entry.attrib.get("id")
            value = next(
                (child for child in entry if isinstance(child.tag, str) and _local(child.tag) == value_name),
                None,
            )
            if identifier and value is not None:
                maps[ref_name][identifier] = value
    return maps


def _inline_description_refs(raw: bytes | None,
                             maps: dict[str, dict[str, etree._Element]]) -> tuple[bytes | None, int, list[str]]:
    if not raw:
        return raw, 0, []
    root = etree.fromstring(raw, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    replacements = 0
    unresolved: list[str] = []
    # Materialize the list because replacing a node mutates the tree iterator.
    for node in list(root.iter()):
        if not isinstance(node.tag, str):
            continue
        tag = _local(node.tag)
        if tag not in maps:
            continue
        identifier = node.attrib.get("id")
        definition = maps[tag].get(identifier or "")
        if definition is None:
            unresolved.append(f"{tag}:{identifier or '(missing id)'}")
            continue
        parent = node.getparent()
        if parent is None:
            unresolved.append(f"{tag}:{identifier or '(missing id)'}")
            continue
        concrete = deepcopy(definition)
        concrete.tail = node.tail
        parent.replace(node, concrete)
        replacements += 1
    return _xml(root), replacements, unresolved


def _color_id_map(reference: dict[str, Any]) -> dict[str, str]:
    raw = reference.get("dictionaries", {}).get("color")
    if not raw:
        return {}
    root = etree.fromstring(raw, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for entry in root.iter():
        if not isinstance(entry.tag, str) or _local(entry.tag) != "EntryColor":
            continue
        identifier = entry.attrib.get("id")
        if not identifier:
            continue
        candidate = re.sub(r"[^A-Za-z0-9_\-.+><:]", "_", identifier)
        while "::" in candidate:
            candidate = candidate.replace("::", ":_")
        base, suffix = candidate or "COLOR", 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        mapping[identifier] = candidate
        used.add(candidate)
    return mapping


def _rewrite_color_ids(raw: bytes | None, mapping: dict[str, str]) -> tuple[bytes | None, int]:
    if not raw or not mapping:
        return raw, 0
    root = etree.fromstring(raw, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    changes = 0
    for node in root.iter():
        if not isinstance(node.tag, str) or _local(node.tag) not in {"EntryColor", "ColorRef"}:
            continue
        identifier = node.attrib.get("id")
        replacement = mapping.get(identifier or "")
        if replacement and replacement != identifier:
            node.attrib["id"] = replacement
            changes += 1
    return _xml(root), changes


def _normalize_two_point_polylines(raw: bytes | None) -> tuple[bytes | None, int]:
    if not raw:
        return raw, 0
    root = etree.fromstring(raw, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    changes = 0
    for node in list(root.iter()):
        if not isinstance(node.tag, str) or _local(node.tag) != "Polyline":
            continue
        begin = _child(node, "PolyBegin")
        steps = [child for child in node if isinstance(child.tag, str)
                 and _local(child.tag) in {"PolyStepSegment", "PolyStepCurve"}]
        line_desc = next((child for child in node if isinstance(child.tag, str)
                          and _local(child.tag) in {"LineDesc", "LineDescRef"}), None)
        if begin is None or len(steps) != 1 or _local(steps[0].tag) != "PolyStepSegment" or line_desc is None:
            continue
        namespace = node.tag.split("}", 1)[0][1:] if node.tag.startswith("{") else None
        line_tag = f"{{{namespace}}}Line" if namespace else "Line"
        line = etree.Element(line_tag, {
            "startX": begin.attrib["x"],
            "startY": begin.attrib["y"],
            "endX": steps[0].attrib["x"],
            "endY": steps[0].attrib["y"],
        })
        line.append(deepcopy(line_desc))
        line.tail = node.tail
        parent = node.getparent()
        if parent is not None:
            parent.replace(node, line)
            changes += 1
    return _xml(root), changes


def normalized_reference(reference: dict[str, Any], report: Any = None) -> dict[str, Any]:
    """Return a donor copy with line/fill references replaced by definitions.

    Cadence can emit ``FillDescRef`` or ``LineDescRef`` in primitive positions
    where the official B1 schema accepts only the concrete description.  A
    concrete description is also a valid substitution-group member wherever a
    reference is accepted, so universal inlining is schema-safe and keeps the
    original reference bytes available for audit.
    """
    maps = _description_maps(reference)
    color_ids = _color_id_map(reference)
    result = dict(reference)
    result["dictionaries"] = dict(reference.get("dictionaries", {}))
    result["packages"] = dict(reference.get("packages", {}))
    result["padstack_defs"] = dict(reference.get("padstack_defs", {}))
    result["stackups"] = list(reference.get("stackups", []))
    result["warnings"] = list(reference.get("warnings", []))
    replacement_count = 0
    color_change_count = 0
    polyline_change_count = 0
    unresolved: list[str] = []

    def normalize(raw: bytes | None) -> bytes | None:
        nonlocal replacement_count, color_change_count, polyline_change_count
        cooked, count, missing = _inline_description_refs(raw, maps)
        replacement_count += count
        unresolved.extend(missing)
        cooked, changes = _rewrite_color_ids(cooked, color_ids)
        color_change_count += changes
        cooked, changes = _normalize_two_point_polylines(cooked)
        polyline_change_count += changes
        return cooked

    for key, raw in result["dictionaries"].items():
        result["dictionaries"][key] = normalize(raw)
    for key, raw in result["packages"].items():
        result["packages"][key] = normalize(raw)
    for key, raw in result["padstack_defs"].items():
        result["padstack_defs"][key] = normalize(raw)
    result["cad_header"] = normalize(reference.get("cad_header"))
    result["profile"] = normalize(reference.get("profile"))
    result["stackups"] = [normalize(raw) for raw in result["stackups"]]
    result["content"] = normalize(reference.get("content"))
    result["schema_normalizations"] = {
        "description_refs_inlined": replacement_count,
        "color_ids_sanitized": color_change_count,
        "two_point_polylines_as_lines": polyline_change_count,
        "unresolved_description_refs": sorted(set(unresolved)),
    }

    if replacement_count or color_change_count or polyline_change_count:
        message = (
            f"Inlined {replacement_count} FillDescRef/LineDescRef element(s) and "
            f"sanitized {color_change_count} color ID/reference occurrence(s) to satisfy "
            f"the official IPC-2581B1 schema; converted {polyline_change_count} two-point "
            "Polyline element(s) to equivalent Line elements."
        )
        result["warnings"].append(message)
        _emit(report, "warning", "REFERENCE_SCHEMA_NORMALIZED", message)
    if unresolved:
        identifiers = ", ".join(sorted(set(unresolved))[:10])
        message = f"Reference description IDs could not be resolved: {identifiers}"
        result["warnings"].append(message)
        _emit(report, "warning", "REFERENCE_DESCRIPTION_REF_UNRESOLVED", message)
    return result
