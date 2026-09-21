"""Disk-backed PowerSI SPD -> IPC-2581B conversion.

This produces exchange geometry, not an equivalent Allegro authoring database.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from lxml import etree as E

from . import __version__
from .geometry import NS, el, sub, xy, num, ring, primitive, feature_set
from .planes import write_planes
from .names import IPCWriter
from .reference import read_reference, normalized_reference
from .report import Report


def validate_xml(path, xsd):
    schema = E.XMLSchema(E.parse(str(xsd), E.XMLParser(resolve_entities=False, no_network=True)))
    context = E.iterparse(str(path), events=("end",), schema=schema, huge_tree=True,
                          resolve_entities=False, no_network=True, load_dtd=False)
    for _, node in context:
        if node.getroottree().docinfo.doctype:
            raise ValueError("DTD declarations are not permitted")
        node.clear()
        parent = node.getparent()
        if parent is not None:
            while node.getprevious() is not None:
                del parent[0]
    return {"valid": True, "schema": str(Path(xsd).resolve()), "scope": "XSD only; not electrical/manufacturing equivalence"}


def _xml(data):
    return E.fromstring(data, E.XMLParser(resolve_entities=False, no_network=True))


def _serialize(node):
    return E.tostring(node, encoding="UTF-8", with_tail=False) + b"\n"


def _layer_name(raw):
    return raw.split("$", 1)[-1]


def _pad(padstacks, name, layer):
    stack = padstacks.get(name)
    if not stack:
        return None
    pads = stack.get("pads", {})
    return pads.get(layer) or pads.get("Default") or pads.get("default")


def _pad_element(pad, x, y, rotation, scale, stack=None, pin=None):
    rotation = rotation or 0
    angle = math.radians(rotation)
    dx, dy = pad.get("offset_x", 0), pad.get("offset_y", 0)
    x, y = x + dx*math.cos(angle)-dy*math.sin(angle), y + dx*math.sin(angle)+dy*math.cos(angle)
    node = el("Pad", padstackDefRef=stack)
    if rotation:
        sub(node, "Xform", rotation=num(rotation % 360))
    xy(node, "Location", x, y, scale)
    node.append(primitive(pad["kind"], pad["data"], scale))
    if pin:
        sub(node, "PinRef", componentRef=pin[0], pin=pin[1])
    return node


def _header(metadata, reference, names, drill_names, scale):
    step = (reference or {}).get("step_name") or "SPD_DESIGN"
    content = el("Content", roleRef="OWNER")
    sub(content, "FunctionMode", mode="ASSEMBLY", level="3", comment="SPD geometry reconstruction; see loss report")
    sub(content, "StepRef", name=step)
    for name in list(names.values()) + list(drill_names.values()):
        sub(content, "LayerRef", name=name)
    if reference:
        for key in ("standard", "user", "font", "line", "fill", "color", "firmware"):
            data = reference["dictionaries"].get(key)
            if data:
                content.append(_xml(data))
    logistics = el("LogisticHeader")
    sub(logistics, "Role", id="OWNER", roleFunction="OWNER")
    sub(logistics, "Enterprise", id="CONVERTER", name="BRD-SPD-IPC2581", code="UNSPECIFIED")
    sub(logistics, "Person", name="Unspecified", enterpriseRef="CONVERTER", roleRef="OWNER")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    history = el("HistoryRecord", number="1", origination=now, lastChange=now, software="BRD-SPD-IPC2581")
    revision = sub(history, "FileRevision", fileRevisionId="1", comment="Generated from SPD; see adjacent report")
    software = sub(revision, "SoftwarePackage", name="BRD-SPD-IPC2581", vendor="BRD-SPD-IPC2581", revision=__version__)
    sub(software, "Certification", certificationStatus="SELFTEST")
    return step, content, logistics, history


def _components(db, metadata, reference, names, scale, report):
    packages, components, logical_nets, node_pins = {}, [], defaultdict(list), {}
    source_components = metadata["components"]
    donors = (reference or {}).get("components", {})
    donors_by_part = defaultdict(list)
    for ref, data in donors.items():
        donors_by_part[data.get("part")].append(data)
    for ref, part, encoded in db.execute("SELECT ref,part,pins FROM connections"):
        placement = source_components.get(ref)
        if placement is None:
            report.warn("CONNECTION_WITHOUT_PLACEMENT", ref)
            continue
        layer = placement["layer"]
        if layer not in names:
            raise ValueError(f"Component {ref} has unknown layer {layer}")
        rotation = placement.get("rotation", 0)
        pins = json.loads(encoded)
        donor = donors.get(ref)
        if donor and donor.get("part") != part:
            donor = None
        if donor is None:
            matches = donors_by_part.get(part, [])
            variants = {d.get("packageRef") for d in matches}
            if len(variants) == 1:
                donor = matches[0]
            elif len(variants) > 1:
                report.warn("AMBIGUOUS_PACKAGE_DONOR", f"{ref} / {part}; rebuilding package from SPD pins")
        pin_nodes = []
        for pin, nodeid in pins.items():
            node = db.execute("SELECT net,x,y,layer,padstack,rotation FROM nodes WHERE id=?", (nodeid,)).fetchone()
            if node is None:
                raise ValueError(f"Component {ref}.{pin} refers to missing node {nodeid}")
            net, x, y, pin_layer, padstack, pad_rotation = node
            pad_rotation = pad_rotation or 0
            if net:
                logical_nets[net].append((ref, pin))
            node_pins[nodeid] = (ref, pin)
            pin_nodes.append((pin, x, y, pin_layer, padstack, pad_rotation))
        if donor and donor.get("packageRef") in reference["packages"]:
            package_name = donor["packageRef"]
            packages[package_name] = _xml(reference["packages"][package_name])
            component = el("Component", **{**donor["attrs"], "refDes": ref, "part": part, "layerRef": names[layer]})
            transform = dict(donor.get("xform", {}))
            transform["rotation"] = num(rotation % 360)
            if donor.get("layerRef") != names[layer]:
                report.warn("COMPONENT_LAYER_CHANGE", f"{ref}: template {donor.get('layerRef')} -> {names[layer]}; mirror must be reviewed")
            for attributes in donor.get("nonstandard_attributes", []):
                sub(component, "NonstandardAttribute", **attributes)
            sub(component, "Xform", **transform)
            report.counts["components_with_reference_package"] += 1
        else:
            package_name = "SPD_PACKAGE_" + ref
            package = el("Package", name=package_name, type="OTHER", pinOneOrientation="OTHER")
            outline = metadata["parts"].get(part, {}).get("outline", [])
            if len(outline) != 2 or min(outline) <= 0:
                report.warn("MISSING_PACKAGE_OUTLINE", f"{ref}/{part}: package body outline unavailable; pin extent used (not physical body)")
                width = max([abs(p[1] - placement["x"]) * 2 for p in pin_nodes] + [0.001])
                height = max([abs(p[2] - placement["y"]) * 2 for p in pin_nodes] + [0.001])
            else:
                width, height = outline
            boundary = sub(package, "Outline")
            boundary.append(ring([-width/2, -height/2, width/2, -height/2, width/2, height/2, -width/2, height/2], scale))
            sub(boundary, "LineDesc", lineEnd="ROUND", lineWidth="0")
            angle = math.radians(-rotation)
            for pin, x, y, pin_layer, padstack, pad_rotation in pin_nodes:
                pad = _pad(metadata["padstacks"], padstack, pin_layer)
                if pad is None:
                    report.warn("PACKAGE_PIN_WITHOUT_PAD", f"{ref}.{pin}: {padstack} / {pin_layer}")
                    continue
                dx, dy = x - placement["x"], y - placement["y"]
                pad_angle = math.radians(pad_rotation)
                ox, oy = pad.get("offset_x", 0), pad.get("offset_y", 0)
                dx += ox*math.cos(pad_angle)-oy*math.sin(pad_angle)
                dy += ox*math.sin(pad_angle)+oy*math.cos(pad_angle)
                p = sub(package, "Pin", number=pin, type="SURFACE", electricalType="UNDEFINED", mountType="UNDEFINED")
                if pad_rotation != rotation:
                    sub(p, "Xform", rotation=num((pad_rotation - rotation) % 360))
                xy(p, "Location", dx*math.cos(angle)-dy*math.sin(angle), dx*math.sin(angle)+dy*math.cos(angle), scale)
                p.append(primitive(pad["kind"], pad["data"], scale))
            packages[package_name] = package
            component = el("Component", refDes=ref, packageRef=package_name, part=part,
                           layerRef=names[layer], mountType="OTHER")
            sub(component, "Xform", rotation=num(rotation % 360))
            report.counts["components_with_reconstructed_package"] += 1
        xy(component, "Location", placement["x"], placement["y"], scale)
        components.append(component)
    missing = set(source_components) - {x.get("refDes") for x in components}
    for ref in missing:
        report.warn("PLACEMENT_WITHOUT_CONNECTION", f"{ref}: no part/pin mapping; component not exported")
    if reference:
        removed = set(donors) - set(source_components)
        report.data["reference_components_absent_from_spd"] = sorted(removed)
        if removed:
            report.warn("REFERENCE_COMPONENTS_NOT_COPIED", f"{len(removed)} components absent from SPD; see JSON list")
    return packages, components, logical_nets, node_pins


def _write_design(db, metadata, reference, target, work, report, progress):
    scale = reference["mm_scale"] if reference else 1
    units = reference["units"] if reference else "MILLIMETER"
    names = {layer["name"]: _layer_name(layer["name"]) for layer in metadata["layers"]}
    if len(set(names.values())) != len(names):
        raise ValueError("Layer names collide after removing Signal$/Medium$ prefixes")
    copper = [l["name"] for l in metadata["layers"] if l["kind"] == "CONDUCTOR"]
    if not copper:
        raise ValueError("No conductor stackup layers were found")
    layer_widths = {l["name"]: l.get("attrs", {}).get("width") for l in metadata["layers"]}
    db.execute("CREATE INDEX IF NOT EXISTS shapes_section ON shapes(section,polarity)")
    for table, columns in (("traces", "start"), ("traces", "end"), ("vias", "upper"), ("vias", "lower")):
        db.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_{columns} ON {table}("{columns}")')
    for table, left, right in (("traces", "start", "end"), ("vias", "upper", "lower")):
        missing = db.execute(f'SELECT COUNT(*) FROM {table} t LEFT JOIN nodes a ON a.id=t."{left}" LEFT JOIN nodes b ON b.id=t."{right}" WHERE a.id IS NULL OR b.id IS NULL').fetchone()[0]
        if missing:
            raise ValueError(f"{missing} {table} have unresolved node references")
    spans = list(db.execute('SELECT DISTINCT a.layer,b.layer FROM vias v JOIN nodes a ON a.id=v.upper JOIN nodes b ON b.id=v.lower'))
    drill_names = {(a,b): f"SPD_DRILL_{i+1}" for i,(a,b) in enumerate(spans)}
    packages, components, logical_nets, node_pins = _components(db, metadata, reference, names, scale, report)
    streams = {name: (work / f"layer_{i}.part").open("wb", buffering=1024*1024)
               for i, name in enumerate(list(names.values()) + list(drill_names.values()))}
    try:
        def write(layer, node):
            if layer not in streams:
                raise ValueError(f"Undefined output layer {layer}")
            streams[layer].write(_serialize(node))

        for section, layer in metadata["shape_layers"].items():
            if layer not in names:
                raise ValueError(f"Shape section {section} maps to unknown layer {layer}")
            progress(f"Plane contours: {names[layer]}")
            write_planes(db, section, lambda node: write(names[layer], node), report, scale)
        unmapped = db.execute("SELECT DISTINCT section FROM shapes").fetchall()
        for (section,) in unmapped:
            if section not in metadata["shape_layers"]:
                raise ValueError(f"Shape section {section} has no PatchSignal layer mapping")

        progress("Writing traces")
        for traceid, net, width, attrs_json, x1, y1, layer, x2, y2, layer2 in db.execute(
                'SELECT t.id,t.net,t.width,t.attrs,a.x,a.y,a.layer,b.x,b.y,b.layer FROM traces t JOIN nodes a ON a.id=t.start JOIN nodes b ON b.id=t.end'):
            if layer != layer2:
                raise ValueError(f"Trace {traceid} crosses layers")
            attrs = json.loads(attrs_json)
            if width is None:
                from .spd import _length
                if not layer_widths.get(layer):
                    raise ValueError(f"Trace {traceid}: width and layer default width both absent")
                width = _length(layer_widths[layer], 0, "default trace width")
            if width <= 0:
                raise ValueError(f"Trace {traceid}: width must be positive")
            if attrs and not attrs.get("arc"):
                report.warn("TRACE_ATTRIBUTES_NOT_EXPORTED", f"{traceid}: {attrs}; exported straight uniform-width centerline")
            if attrs.get("arc"):
                arc = attrs["arc"]
                line = el("Arc", startX=num(x1*scale), startY=num(y1*scale), endX=num(x2*scale), endY=num(y2*scale),
                          centerX=num(arc["x"]*scale), centerY=num(arc["y"]*scale), clockwise=str(arc["clockwise"]).lower())
            else:
                line = el("Line", startX=num(x1*scale), startY=num(y1*scale), endX=num(x2*scale), endY=num(y2*scale))
            sub(line, "LineDesc", lineEnd="ROUND", lineWidth=num(width*scale))
            write(names[layer], feature_set(net, line))
            report.counts["exported_traces"] += 1
        progress("Writing node pads")
        for nodeid, net, x, y, layer, stack, rotation in db.execute("SELECT id,net,x,y,layer,padstack,rotation FROM nodes WHERE padstack IS NOT NULL AND padstack!=''"):
            pad = _pad(metadata["padstacks"], stack, layer)
            if pad is None:
                report.warn("NODE_PAD_NOT_DEFINED", f"{nodeid}: {stack}/{layer}")
                continue
            group = el("Set", net=net or None, polarity="POSITIVE")
            group.append(_pad_element(pad, x, y, rotation, scale, pin=node_pins.get(nodeid)))
            write(names[layer], group)
            report.counts["exported_node_pads"] += 1

        progress("Writing via pads and drill spans")
        for viaid, net, stackname, x, y, upper, lx, ly, lower in db.execute(
                'SELECT v.id,v.net,v.padstack,a.x,a.y,a.layer,b.x,b.y,b.layer FROM vias v JOIN nodes a ON a.id=v.upper JOIN nodes b ON b.id=v.lower'):
            stack = metadata["padstacks"].get(stackname)
            if not stack:
                raise ValueError(f"Via {viaid}: undefined padstack {stackname}")
            if abs(x-lx) > 1e-8 or abs(y-ly) > 1e-8:
                raise ValueError(f"Via {viaid} has non-coincident endpoints; angled vias are unsupported")
            if upper not in copper or lower not in copper:
                raise ValueError(f"Via {viaid}: unknown conductor span")
            begin, end = sorted((copper.index(upper), copper.index(lower)))
            for layer in copper[begin:end+1]:
                pad = _pad(metadata["padstacks"], stackname, layer)
                if pad:
                    group = el("Set", net=net or None, polarity="POSITIVE", padUsage="VIA")
                    group.append(_pad_element(pad, x, y, 0, scale))
                    write(names[layer], group)
                    report.counts["exported_via_pads"] += 1
            radius = stack.get("inner_radius") or stack.get("outer_radius")
            if radius is None:
                report.warn("VIA_WITHOUT_BARREL_RADIUS", f"{viaid}: {stackname}; hole omitted")
            else:
                if not stack.get("inner_radius"):
                    report.warn("VIA_DRILL_APPROXIMATION", f"Padstack {stackname}: outer barrel radius used because finished drill radius is absent")
                group = el("Set", net=net or None, polarity="POSITIVE", plate="true")
                sub(group, "Hole", name=f"SPD_HOLE_{report.counts['exported_vias']+1}", diameter=num(2*radius*scale), platingStatus="VIA", plusTol="0", minusTol="0", x=num(x*scale), y=num(y*scale))
                write(drill_names[(upper, lower)], group)
                report.counts["exported_via_holes"] += 1
            report.counts["exported_vias"] += 1
    finally:
        for stream in streams.values():
            stream.close()

    step_name, content, logistics, history = _header(metadata, reference, names, drill_names, scale)
    with E.xmlfile(str(target), encoding="UTF-8", buffered=True) as raw_output:
        output = IPCWriter(raw_output, report)
        output.write_declaration()
        with output.element("IPC-2581", {"revision": "B"}, nsmap={None: NS}):
            output.write(content, logistics, history)
            with output.element("Ecad", name=step_name):
                # Material specifications cannot be inferred from simulation properties alone.
                if reference and reference.get("cad_header"):
                    output.write(_xml(reference["cad_header"]))
                else:
                    output.write(el("CadHeader", units=units))
                with output.element("CadData"):
                    for layer in metadata["layers"]:
                        raw = layer["name"]
                        side = "TOP" if raw == copper[0] else "BOTTOM" if raw == copper[-1] else "INTERNAL"
                        output.write(el("Layer", name=names[raw], layerFunction="CONDUCTOR" if layer["kind"] == "CONDUCTOR" else "DIELBASE", side=side, polarity="POSITIVE"))
                    for (a,b), name in drill_names.items():
                        layer = el("Layer", name=name, layerFunction="DRILL", side="ALL", polarity="POSITIVE")
                        sub(layer, "Span", fromLayer=names[a], toLayer=names[b])
                        output.write(layer)
                    thickness = sum(l["thickness"] for l in metadata["layers"])
                    stackup = el("Stackup", name="SPD_STACKUP", overallThickness=num(thickness*scale), tolPlus="0", tolMinus="0", whereMeasured="OTHER")
                    group = sub(stackup, "StackupGroup", name="SPD_LAYERS", thickness=num(thickness*scale), tolPlus="0", tolMinus="0")
                    for index, layer in enumerate(metadata["layers"], 1):
                        sub(group, "StackupLayer", layerOrGroupRef=names[layer["name"]], thickness=num(layer["thickness"]*scale), tolPlus="0", tolMinus="0", sequence=index)
                    output.write(stackup)
                    with output.element("Step", name=step_name):
                        definitions = {name: _xml(data) for name, data in (reference or {}).get("padstack_defs", {}).items()}
                        for name, stack in metadata["padstacks"].items():
                            definition = el("PadStackDef", name=name)
                            for raw_layer, pad in stack["pads"].items():
                                if raw_layer not in names:
                                    report.warn("PADSTACK_LAYER_NOT_EXPORTED", f"{name}/{raw_layer}: non-physical/default pad layer")
                                    continue
                                paddef = sub(definition, "PadstackPadDef", layerRef=names[raw_layer], padUse="REGULAR")
                                xy(paddef, "Location", pad.get("offset_x", 0), pad.get("offset_y", 0), scale)
                                paddef.append(primitive(pad["kind"], pad["data"], scale))
                            definitions[name] = definition
                        for definition in definitions.values():
                            output.write(definition)
                        output.write(el("Datum", x="0", y="0"))
                        if reference and reference.get("profile_has_geometry"):
                            output.write(_xml(reference["profile"]))
                            report.counts["reference_profiles_preserved"] = 1
                        else:
                            report.warn("BOARD_OUTLINE_UNAVAILABLE", "No reference profile supplied; copper bounding box is not used as an invented board outline")
                        for package in packages.values():
                            output.write(package)
                        for component in components:
                            output.write(component)
                        for name, pins in logical_nets.items():
                            net = el("LogicalNet", name=name)
                            for ref, pin in pins:
                                sub(net, "PinRef", componentRef=ref, pin=pin)
                            output.write(net)
                        # xmlfile writes raw bytes as escaped text; parse each spooled Set incrementally.
                        for index, layer in enumerate(streams):
                            path = work / f"layer_{index}.part"
                            if path.stat().st_size == 0:
                                continue
                            progress(f"Assembling IPC layer: {layer}")
                            with output.element("LayerFeature", layerRef=layer):
                                with path.open("rb") as source:
                                    for line in source:
                                        output.write(_xml(line))
    report.counts["exported_components"] = len(components)
    report.counts["exported_packages"] = len(packages)
    report.counts["exported_logical_nets"] = len(logical_nets)
    report.data["layer_mapping"] = {raw: output.name(name) for raw, name in names.items()}
    report.data["identifier_mapping"] = output.mapping
    report.data["units"] = units


def convert(source, output, *, template=None, progress=None, xsd=None):
    from .spd import parse_spd
    source, output = Path(source).resolve(), Path(output).resolve()
    template = Path(template).resolve() if template else None
    if output in (source, template) or output.exists():
        raise ValueError("Output must be a new file, distinct from all input files")
    if output.suffix.lower() != ".xml":
        raise ValueError("Use an .xml output; BRD generation requires Cadence import-brd")
    if not source.is_file() or (template and not template.is_file()):
        raise FileNotFoundError("Input SPD/reference XML does not exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = Report(output)
    report.data.update(source=str(source), reference=str(template) if template else None,
                       fidelity="geometry_exchange_with_reported_losses")
    progress = progress or (lambda message: None)
    start = time.monotonic()
    try:
        reference = normalized_reference(read_reference(template, report, progress), report) if template else None
        with tempfile.TemporaryDirectory(prefix="spd-ipc-", dir=output.parent) as directory:
            work = Path(directory)
            metadata = parse_spd(source, work / "design.sqlite", report, progress)
            report.data["source_metadata"] = {k: v for k, v in metadata.items() if k not in ("components", "parts", "padstacks")}
            report.data["padstack_source_parameters"] = metadata["padstacks"]
            report.warn("NATIVE_DESIGN_LIMITATION", "Allegro IPC importer exchanges stackup and manufacturing-layer features, not native components/clines/dynamic shape rules")
            report.warn("SIMULATION_AND_RULES_NOT_EXPORTED", "SPD simulation settings, solvers, thermal models, Allegro constraints, dynamic repour settings, and design history are not recreated")
            report.warn("MANUFACTURING_METADATA_INCOMPLETE", "BOM/AVL, assembly variants, soldermask/paste/silkscreen, tolerances and fabrication specifications require source CAD review; existing reference BOM is not copied")
            report.warn("STACKUP_MATERIAL_REVIEW", "SPD layer thickness is exported; source material properties are retained in JSON, not asserted as manufacturing material specifications")
            with closing(sqlite3.connect(work / "design.sqlite")) as db:
                progress("Resolving geometry and connectivity")
                staged = work / "result.xml"
                _write_design(db, metadata, reference, staged, work, report, progress)
            if xsd is None:
                bundled = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "schemas/IPC-2581B1.xsd"
                xsd = bundled if bundled.is_file() else None
            if xsd:
                progress("Validating official IPC schema")
                report.data["schema_validation"] = validate_xml(staged, xsd)
            else:
                report.data["schema_validation"] = {"valid": None, "reason": "No --xsd supplied"}
            # Publish only the complete, validated file. Both paths are on the same volume.
            if os.name == "nt":
                os.rename(staged, output)  # Windows refuses an existing destination.
            else:
                os.link(staged, output)  # Exclusive publication on POSIX.
                staged.unlink()
        return report.save("completed_with_warnings", seconds=round(time.monotonic()-start, 3), output_bytes=output.stat().st_size)
    except BaseException as exc:
        report.save("failed", error=str(exc), seconds=round(time.monotonic()-start, 3))
        raise
