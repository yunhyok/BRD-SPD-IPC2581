"""Streaming parser for the text based Sigrity SPD layout format.

The parser intentionally keeps the multi-million-row geometry in SQLite and
returns only the comparatively small design metadata.  Lengths are normalized
to millimetres.  The input file is opened read-only and is never rewritten.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Iterator


_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_LENGTH_RE = re.compile(rf"^({_NUMBER})(mm|um|u|mil|inch|in|m)?$", re.IGNORECASE)
_LENGTH_TOKEN_RE = re.compile(rf"(?<![\w.]){_NUMBER}(?:mm|um|u|mil|inch|in|m)?(?!\w)", re.IGNORECASE)
_ASSIGN_RE = re.compile(r"(?<!\S)([A-Za-z][A-Za-z0-9_]*)\s*=\s*")
_SHAPE_RE = re.compile(r"^(Polygon|Circle|Box)(.*)$", re.IGNORECASE)
_SECTION_RE = re.compile(r"^\*\s*(.*?)\s+description lines\s*$", re.IGNORECASE)
_UNIT_TO_MM = {
    "": 1.0,
    "mm": 1.0,
    "um": 1e-3,
    "u": 1e-3,
    "mil": 0.0254,
    "inch": 25.4,
    "in": 25.4,
    "m": 1000.0,
}
_BATCH_SIZE = 10_000


def _warn(report, code: str, message: str, line: int | None = None) -> None:
    if report is not None and hasattr(report, "warn"):
        report.warn(code, message, line=line)


def _count(report, key: str, amount: int = 1) -> None:
    counts = getattr(report, "counts", None)
    if counts is not None:
        counts[key] += amount


def _length(token: str, line: int, field: str) -> float:
    """Parse an SPD length and normalize it to millimetres."""
    token = token.strip().strip(",()")
    match = _LENGTH_RE.fullmatch(token)
    if not match:
        raise ValueError(f"line {line}: invalid {field} length {token!r}")
    value = float(match.group(1)) * _UNIT_TO_MM[(match.group(2) or "").lower()]
    if not math.isfinite(value):
        raise ValueError(f"line {line}: non-finite {field} length {token!r}")
    return value


def _number(token: str, line: int, field: str) -> float:
    token = token.strip().strip(",()")
    try:
        value = float(token)
    except ValueError as exc:
        raise ValueError(f"line {line}: invalid {field} number {token!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"line {line}: non-finite {field} number {token!r}")
    return value


def _assignments(record: str) -> dict[str, str]:
    """Return case-insensitive ``key = value`` fields without losing lists."""
    matches = list(_ASSIGN_RE.finditer(record))
    result: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(record)
        value = record[match.end() : end].strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        result[match.group(1).lower()] = value
    return result


def _canonical_node(value: str) -> str:
    value = value.strip()
    return value[len("$Package.") :] if value.startswith("$Package.") else value


def _first_attr(attrs: dict[str, str], key: str) -> str | None:
    values = attrs.get(key, "").split()
    return values[0] if values else None


def _net_from_name(value: str) -> str:
    return value.split("::", 1)[1] if "::" in value else ""


def _logical_records(source: Path, progress=None) -> Iterator[tuple[int, str]]:
    """Yield continuation-folded records while retaining their first line."""
    total = source.stat().st_size
    processed = 0
    next_progress = 64 * 1024 * 1024
    first_line = 0
    current: str | None = None
    with source.open("rb") as stream:
        for line_number, raw in enumerate(stream, 1):
            processed += len(raw)
            try:
                text = raw.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    f"line {line_number}: SPD is not valid UTF-8 "
                    f"(byte offset {exc.start} within the line)"
                ) from exc
            if text.lstrip().startswith("+") and current is not None:
                continuation = text.lstrip()[1:].strip()
                current += " " + continuation
            else:
                if current is not None:
                    yield first_line, current
                first_line = line_number
                current = text
            if progress and processed >= next_progress:
                progress(f"SPD parse: {processed / total:.0%}")
                next_progress += 64 * 1024 * 1024
    if current is not None:
        yield first_line, current


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=FILE;
        PRAGMA locking_mode=EXCLUSIVE;
        PRAGMA cache_size=-65536;
        DROP TABLE IF EXISTS nodes;
        DROP TABLE IF EXISTS traces;
        DROP TABLE IF EXISTS vias;
        DROP TABLE IF EXISTS shapes;
        DROP TABLE IF EXISTS connections;
        CREATE TABLE nodes(
            id TEXT PRIMARY KEY, net TEXT, x REAL, y REAL, layer TEXT,
            padstack TEXT, rotation REAL
        );
        CREATE TABLE traces(
            id TEXT, net TEXT, start TEXT, end TEXT, width REAL, attrs TEXT
        );
        CREATE TABLE vias(
            id TEXT, net TEXT, upper TEXT, lower TEXT, padstack TEXT
        );
        CREATE TABLE shapes(
            id TEXT, section TEXT, net TEXT, polarity TEXT,
            kind TEXT, data TEXT, line INTEGER
        );
        CREATE TABLE connections(ref TEXT, part TEXT, pins TEXT);
        """
    )


def _shape_identity(token: str) -> tuple[str, str, str, str] | None:
    match = _SHAPE_RE.match(token)
    if not match:
        return None
    kind = match.group(1).capitalize()
    suffix = match.group(2)
    if "::" in suffix:
        identifier, net = suffix.split("::", 1)
    else:
        identifier, net = suffix, ""
    polarity = "+"
    if net.endswith(("+", "-")):
        polarity, net = net[-1], net[:-1]
    elif not net and identifier.endswith(("+", "-")):
        polarity, identifier = identifier[-1], identifier[:-1]
    return kind, kind + identifier, net, polarity


def _parse_shape(
    record: str, section: str, line: int, report
) -> tuple[str, str, str, str, str, str, int] | None:
    tokens = record.split()
    if not tokens:
        return None
    identity = _shape_identity(tokens[0])
    if identity is None:
        _warn(report, "SPD_UNSUPPORTED_SHAPE_RECORD", f"Unsupported shape record: {record[:180]}", line)
        return None
    kind, identifier, net, polarity = identity
    numeric_tokens = [m.group(0) for m in _LENGTH_TOKEN_RE.finditer(" ".join(tokens[1:]))]
    values = [_length(token, line, f"{kind} coordinate") for token in numeric_tokens]
    if kind == "Polygon":
        if len(values) < 6 or len(values) % 2:
            raise ValueError(f"line {line}: polygon {identifier!r} needs coordinate pairs")
        stored_kind, data = "Polygon", values
    elif kind == "Circle":
        if len(values) != 3:
            raise ValueError(f"line {line}: circle {identifier!r} needs x, y, and radius")
        if values[2] < 0:
            raise ValueError(f"line {line}: circle {identifier!r} has a negative radius")
        stored_kind, data = "Circle", values
    else:  # SPD Box is represented as its four polygon corners in the DB contract.
        if len(values) != 4:
            raise ValueError(f"line {line}: box {identifier!r} needs x, y, width, and height")
        x, y, width, height = values
        if width < 0 or height < 0:
            raise ValueError(f"line {line}: box {identifier!r} has a negative size")
        stored_kind = "Polygon"
        data = [x, y, x + width, y, x + width, y + height, x, y + height]
    return (
        identifier,
        section,
        net,
        polarity,
        stored_kind,
        json.dumps(data, separators=(",", ":")),
        line,
    )


def _flush(connection: sqlite3.Connection, table: str, rows: list[tuple]) -> None:
    if not rows:
        return
    placeholders = {
        "nodes": "(?,?,?,?,?,?,?)",
        "traces": "(?,?,?,?,?,?)",
        "vias": "(?,?,?,?,?)",
        "shapes": "(?,?,?,?,?,?,?)",
        "connections": "(?,?,?)",
    }[table]
    connection.executemany(f"INSERT INTO {table} VALUES {placeholders}", rows)
    rows.clear()


def _resolve_missing_node_references(connection: sqlite3.Connection, report) -> None:
    """Resolve the rare SPD reference that omits ``!!pin`` from a node name."""
    for table, column in (("traces", "start"), ("traces", "end"), ("vias", "upper"), ("vias", "lower")):
        missing = connection.execute(
            f"SELECT DISTINCT t.{column} FROM {table} t "
            f"LEFT JOIN nodes n ON n.id=t.{column} WHERE n.id IS NULL"
        )
        for (reference,) in missing:
            if not reference or "!!" in reference:
                _warn(report, "SPD_MISSING_NODE", f"Unresolved {table}.{column} node {reference!r}")
                continue
            base, separator, net = reference.partition("::")
            pattern = base + "!!*" + (separator + net if separator else "")
            candidates = connection.execute(
                "SELECT id FROM nodes WHERE id GLOB ? LIMIT 2", (pattern,)
            ).fetchall()
            if len(candidates) == 1:
                connection.execute(
                    f"UPDATE {table} SET {column}=? WHERE {column}=?",
                    (candidates[0][0], reference),
                )
            else:
                reason = "ambiguous" if candidates else "unresolved"
                _warn(report, "SPD_MISSING_NODE", f"{reason.title()} {table}.{column} node {reference!r}")


def _resolve_default_trace_widths(
    connection: sqlite3.Connection, layer_widths: dict[str, float]
) -> None:
    """Fill omitted trace widths from the starting node's layer default."""
    if layer_widths:
        connection.execute("CREATE TEMP TABLE layer_widths(layer TEXT PRIMARY KEY, width REAL NOT NULL)")
        connection.executemany("INSERT INTO layer_widths VALUES (?,?)", layer_widths.items())
        connection.execute(
            "UPDATE traces SET width=("
            "SELECT lw.width FROM nodes n JOIN layer_widths lw ON lw.layer=n.layer "
            "WHERE n.id=traces.start"
            ") WHERE width IS NULL"
        )
    unresolved = connection.execute(
        "SELECT t.id,t.start,n.layer FROM traces t "
        "LEFT JOIN nodes n ON n.id=t.start WHERE t.width IS NULL LIMIT 1"
    ).fetchone()
    if unresolved:
        trace, start, layer = unresolved
        raise ValueError(
            f"Trace {trace!r} has no Width and starting node {start!r} "
            f"has no layer default Width (layer={layer!r})"
        )


def parse_spd(source: Path, db_path: Path, report, progress=None) -> dict:
    """Parse *source* into a bounded-memory SQLite geometry store.

    The returned dictionary contains layer, padstack, part, and component
    metadata.  Physical rows live in the five tables documented by the module's
    public contract.
    """
    source = Path(source)
    db_path = Path(db_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    metadata: dict = {
        "title": "",
        "layers": [],
        "shape_layers": {},
        "padstacks": {},
        "parts": {},
        "components": {},
        "counts": {},
    }
    local_counts: Counter[str] = Counter()
    layer_widths: dict[str, float] = {}
    buffers: dict[str, list[tuple]] = {
        "nodes": [],
        "traces": [],
        "vias": [],
        "shapes": [],
        "connections": [],
    }
    section = ""
    shape_section: str | None = None
    padstack_name: str | None = None
    pad_layer: str | None = None
    connection_ref: str | None = None
    connection_part: str | None = None
    connection_pins: dict[str, str] = {}
    component_parts: dict[str, str] = {}
    connection_refs: set[str] = set()
    via_padstacks: set[str] = set()

    connection = sqlite3.connect(db_path)
    try:
        _create_schema(connection)
        connection.execute("BEGIN")
        for line_number, raw_record in _logical_records(source, progress):
            record = raw_record.strip()
            if not record:
                continue
            section_match = _SECTION_RE.match(record)
            if section_match:
                section = section_match.group(1).strip().lower()
                continue
            if record.startswith("*"):
                continue
            if record.startswith("Title ") and not metadata["title"]:
                metadata["title"] = record[len("Title ") :].strip()
                continue

            if record.startswith(".Shape "):
                if shape_section is not None:
                    raise ValueError(
                        f"line {line_number}: nested .Shape before .EndShape for {shape_section!r}"
                    )
                fields = record.split()
                if len(fields) < 2:
                    raise ValueError(f"line {line_number}: .Shape requires a name")
                shape_section = fields[1]
                local_counts["shape_sections"] += 1
                continue
            if record == ".EndShape":
                shape_section = None
                continue
            if shape_section is not None:
                row = _parse_shape(record, shape_section, line_number, report)
                if row is not None:
                    buffers["shapes"].append(row)
                    local_counts["shapes"] += 1
                if len(buffers["shapes"]) >= _BATCH_SIZE:
                    _flush(connection, "shapes", buffers["shapes"])
                continue

            if record.startswith(".PadStackDef "):
                if padstack_name is not None:
                    raise ValueError(
                        f"line {line_number}: nested .PadStackDef before .EndPadStackDef for {padstack_name!r}"
                    )
                fields = record.split()
                if len(fields) < 2:
                    raise ValueError(f"line {line_number}: .PadStackDef requires a name")
                padstack_name = fields[1]
                if padstack_name in metadata["padstacks"]:
                    raise ValueError(f"line {line_number}: duplicate padstack {padstack_name!r}")
                before_attrs = record[: _ASSIGN_RE.search(record).start()] if _ASSIGN_RE.search(record) else record
                positional = before_attrs.split()[2:]
                radii = [_length(token, line_number, "padstack radius") for token in positional]
                if len(radii) > 2:
                    raise ValueError(f"line {line_number}: too many padstack radii")
                attrs = _assignments(record)
                metadata["padstacks"][padstack_name] = {
                    "hole": (2.0 * radii[1]) if len(radii) > 1 and radii[1] > 0 else None,
                    "outer_radius": radii[0] if radii else None,
                    "inner_radius": radii[1] if len(radii) > 1 else None,
                    "pads": {},
                    "attrs": attrs,
                }
                local_counts["padstacks"] += 1
                continue
            if record == ".EndPadStackDef":
                padstack_name = None
                pad_layer = None
                continue
            if record.startswith(".PadDef ") and padstack_name:
                fields = record.split()
                if len(fields) < 2:
                    raise ValueError(f"line {line_number}: .PadDef requires a layer")
                pad_layer = fields[1]
                continue
            if record == ".EndPadDef":
                pad_layer = None
                continue
            if padstack_name and pad_layer and record.startswith("Regular "):
                pad_attrs = _assignments(record)
                cleaned = re.sub(rf"(?i)Offset[XY]\s*=\s*{_NUMBER}(?:mm|um|u|mil|inch|in|m)?", "", record)
                fields = cleaned.split()
                if len(fields) < 3:
                    raise ValueError(f"line {line_number}: incomplete regular pad geometry")
                source_kind = fields[1]
                kind_map = {"circle": "Circle", "square": "Square", "box": "Rectangle", "rectangle": "Rectangle", "polygon": "Polygon"}
                kind = kind_map.get(source_kind.lower())
                if kind is None:
                    _warn(report, "SPD_UNSUPPORTED_PAD_GEOMETRY", f"Unsupported regular pad geometry: {record[:180]}", line_number)
                    continue
                values = [_length(match.group(0), line_number, "pad geometry") for match in _LENGTH_TOKEN_RE.finditer(" ".join(fields[2:]))]
                required = {"Circle": 1, "Square": 1, "Rectangle": 2}.get(kind)
                if required is not None and len(values) != required:
                    raise ValueError(f"line {line_number}: {source_kind} pad expects {required} size value(s)")
                if kind == "Polygon" and (len(values) < 6 or len(values) % 2):
                    raise ValueError(f"line {line_number}: polygon pad needs coordinate pairs")
                pad = {"kind": kind, "data": values}
                if "offsetx" in pad_attrs:
                    pad["offset_x"] = _length(pad_attrs["offsetx"].split()[0], line_number, "pad X offset")
                if "offsety" in pad_attrs:
                    pad["offset_y"] = _length(pad_attrs["offsety"].split()[0], line_number, "pad Y offset")
                metadata["padstacks"][padstack_name]["pads"][pad_layer] = pad
                continue

            if record.startswith("Patch") and "shape" in _assignments(record) and "layer" in _assignments(record):
                attrs = _assignments(record)
                shape_name = attrs["shape"].split()[0]
                layer_name = attrs["layer"].split()[0]
                metadata["shape_layers"][shape_name] = layer_name
                continue

            # Descriptive ``* ... description lines`` comments are conventional,
            # but not required by the SPD grammar.  Recognize physical records
            # from their keywords and mandatory fields as well as section state.
            if re.match(r"^(?:Signal|Plane|Medium)\S*(?:\s|$)", record) and "=" in record:
                fields = record.split(None, 1)
                if len(fields) < 2:
                    raise ValueError(f"line {line_number}: incomplete layer record")
                name = fields[0]
                attrs = _assignments(record)
                if "thickness" not in attrs:
                    raise ValueError(f"line {line_number}: layer {name!r} has no thickness")
                thickness_token = attrs["thickness"].split()[0]
                metadata["layers"].append({
                    "name": name,
                    "kind": "DIELECTRIC" if name.startswith("Medium") else "CONDUCTOR",
                    "thickness": _length(thickness_token, line_number, "layer thickness"),
                    "attrs": attrs,
                })
                if "width" in attrs:
                    layer_widths[name] = _length(attrs["width"].split()[0], line_number, "layer default width")
                local_counts["layers"] += 1
                continue

            if record.startswith("Node") and re.search(
                r"\b(?:X|Y|Layer|PadStack)\s*=", record, re.IGNORECASE
            ):
                identifier = _canonical_node(record.split(None, 1)[0])
                attrs = _assignments(record)
                try:
                    x_token = attrs["x"].split()[0]
                    y_token = attrs["y"].split()[0]
                    layer = attrs["layer"].split()[0]
                except (KeyError, IndexError) as exc:
                    raise ValueError(f"line {line_number}: incomplete node {identifier!r}") from exc
                rotation = _number(attrs["absoluterotation"].split()[0], line_number, "node rotation") if "absoluterotation" in attrs else None
                buffers["nodes"].append((
                    identifier,
                    _net_from_name(identifier),
                    _length(x_token, line_number, "node X"),
                    _length(y_token, line_number, "node Y"),
                    layer,
                    _first_attr(attrs, "padstack"),
                    rotation,
                ))
                local_counts["nodes"] += 1
                if len(buffers["nodes"]) >= _BATCH_SIZE:
                    _flush(connection, "nodes", buffers["nodes"])
                continue

            if record.startswith("Trace") and re.search(
                r"\b(?:StartingNode|EndingNode|Width)\s*=", record, re.IGNORECASE
            ):
                identifier = record.split(None, 1)[0]
                attrs = _assignments(record)
                try:
                    start = _canonical_node(attrs["startingnode"].split()[0])
                    end = _canonical_node(attrs["endingnode"].split()[0])
                except (KeyError, IndexError) as exc:
                    raise ValueError(f"line {line_number}: incomplete trace {identifier!r}") from exc
                width = _length(attrs["width"].split()[0], line_number, "trace width") if "width" in attrs else None
                flag_text = record[len(identifier) : record.lower().find("startingnode")].strip()
                extra: dict[str, object] = {"flags": flag_text.split()} if flag_text else {}
                for key, value in attrs.items():
                    if key not in {"startingnode", "endingnode", "width"}:
                        extra[key] = value
                if re.search(r"\b(arc|radius|center|sweepangle)\b", record, re.IGNORECASE):
                    _warn(report, "SPD_TRACE_ARC_PRESERVED", f"Arc-like trace attributes preserved in JSON for {identifier}", line_number)
                buffers["traces"].append((identifier, _net_from_name(identifier), start, end, width, json.dumps(extra, separators=(",", ":"))))
                local_counts["traces"] += 1
                if len(buffers["traces"]) >= _BATCH_SIZE:
                    _flush(connection, "traces", buffers["traces"])
                continue

            if record.startswith("Via") and re.search(
                r"\b(?:UpperNode|LowerNode|PadStack)\s*=", record, re.IGNORECASE
            ):
                identifier = record.split(None, 1)[0]
                attrs = _assignments(record)
                try:
                    upper = _canonical_node(attrs["uppernode"].split()[0])
                    lower = _canonical_node(attrs["lowernode"].split()[0])
                except (KeyError, IndexError) as exc:
                    raise ValueError(f"line {line_number}: incomplete via {identifier!r}") from exc
                padstack = _first_attr(attrs, "padstack")
                if padstack:
                    via_padstacks.add(padstack)
                buffers["vias"].append((identifier, _net_from_name(identifier), upper, lower, padstack))
                local_counts["vias"] += 1
                if len(buffers["vias"]) >= _BATCH_SIZE:
                    _flush(connection, "vias", buffers["vias"])
                continue

            if record.startswith(".Part "):
                fields = record.split()
                if len(fields) < 2:
                    raise ValueError(f"line {line_number}: .Part requires a name")
                attrs = _assignments(record)
                if fields[1] in metadata["parts"]:
                    raise ValueError(f"line {line_number}: duplicate part {fields[1]!r}")
                outline: list[float] = []
                if "outline" in attrs:
                    outline = [_length(m.group(0), line_number, "part outline") for m in _LENGTH_TOKEN_RE.finditer(attrs["outline"])]
                metadata["parts"][fields[1]] = {"attrs": attrs, "outline": outline}
                local_counts["parts"] += 1
                continue

            if record.startswith(".Component "):
                fields = record.split()
                if len(fields) < 4:
                    raise ValueError(f"line {line_number}: incomplete component")
                ref = fields[1]
                if ref in metadata["components"]:
                    raise ValueError(f"line {line_number}: duplicate component {ref!r}")
                attrs = _assignments(record)
                metadata["components"][ref] = {
                    "x": _length(fields[2], line_number, "component X"),
                    "y": _length(fields[3], line_number, "component Y"),
                    "rotation": _number(attrs["rotation"].split()[0], line_number, "component rotation") if "rotation" in attrs else 0.0,
                    "layer": _first_attr(attrs, "startlayer"),
                    "attrs": attrs,
                }
                local_counts["components"] += 1
                continue

            if record.startswith(".Connect "):
                if connection_ref is not None:
                    raise ValueError(f"line {line_number}: nested .Connect record")
                fields = record.split()
                if len(fields) < 3:
                    raise ValueError(f"line {line_number}: incomplete .Connect record")
                connection_ref, connection_part = fields[1], fields[2]
                if connection_ref in connection_refs:
                    raise ValueError(f"line {line_number}: duplicate connection {connection_ref!r}")
                connection_refs.add(connection_ref)
                connection_pins = {}
                continue
            if record == ".EndC" and connection_ref is not None:
                buffers["connections"].append((connection_ref, connection_part, json.dumps(connection_pins, separators=(",", ":"))))
                component_parts[connection_ref] = connection_part
                component = metadata["components"].get(connection_ref)
                if component is not None:
                    component["part"] = connection_part
                local_counts["connections"] += 1
                connection_ref = connection_part = None
                connection_pins = {}
                if len(buffers["connections"]) >= _BATCH_SIZE:
                    _flush(connection, "connections", buffers["connections"])
                continue
            if connection_ref is not None:
                fields = record.split()
                if len(fields) != 2:
                    raise ValueError(f"line {line_number}: invalid connection pin mapping {record!r}")
                if fields[0] in connection_pins:
                    raise ValueError(
                        f"line {line_number}: duplicate pin {fields[0]!r} in connection {connection_ref!r}"
                    )
                connection_pins[fields[0]] = _canonical_node(fields[1])
                continue

            # Unexpected records in physical sections must be visible to users.
            expected_prefix = {"node": "Node", "trace": "Trace", "via": "Via"}.get(section)
            if expected_prefix and not record.startswith("."):
                code = f"SPD_UNSUPPORTED_{section.upper()}_RECORD"
                _warn(report, code, f"Unsupported {section} record: {record[:180]}", line_number)

        if connection_ref is not None:
            raise ValueError("unterminated .Connect section at end of file")
        if shape_section is not None:
            raise ValueError(f"unterminated .Shape {shape_section!r} section at end of file")
        if pad_layer is not None:
            raise ValueError(f"unterminated .PadDef {pad_layer!r} section at end of file")
        if padstack_name is not None:
            raise ValueError(f"unterminated .PadStackDef {padstack_name!r} section at end of file")
        for table, rows in buffers.items():
            _flush(connection, table, rows)
        _resolve_missing_node_references(connection, report)
        _resolve_default_trace_widths(connection, layer_widths)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    for ref, part in component_parts.items():
        component = metadata["components"].get(ref)
        if component is not None:
            component["part"] = part

    # ``hole`` is deliberately not inferred from OuterRadius.  The SPD guide
    # calls the header fields OuterRadius/InnerRadius, and explicitly says they
    # are optional for SMD pads.  A known inner radius can safely yield a hole
    # diameter; callers may choose and report any OuterRadius approximation.
    for name in via_padstacks:
        padstack = metadata["padstacks"].get(name)
        if padstack is None:
            _warn(report, "SPD_MISSING_PADSTACK", f"Via references undefined padstack {name!r}")

    metadata["counts"] = dict(local_counts)
    for key, value in local_counts.items():
        _count(report, f"spd_{key}", value)
    return metadata
