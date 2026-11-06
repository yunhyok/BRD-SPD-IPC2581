"""Bounded-memory inventories; source files are never modified."""
from __future__ import annotations

import hashlib
import re
import time
from collections import Counter
from pathlib import Path
from xml.parsers import expat


def inspect_spd(path: str | Path, progress=None) -> dict:
    path = Path(path)
    counts, directives, primitives = Counter(), Counter(), Counter()
    layer_shapes = {}
    layers, components = [], {}
    shape = None
    lines = 0
    digest = hashlib.sha256()
    start = time.monotonic()
    processed = 0
    headers = []
    with path.open("rb") as source:
        for lines, raw in enumerate(source, 1):
            digest.update(raw)
            processed += len(raw)
            if lines <= 2:
                headers.append(raw.decode("utf-8", "replace").strip())
            line = raw.lstrip()
            if not line or line[:1] in (b"*", b"+", b"\r", b"\n"):
                continue
            first = line.split(None, 1)[0]
            if first.startswith(b"."):
                key = first.decode("ascii", "replace")
                directives[key] += 1
                if first == b".Shape":
                    fields = line.split()
                    if len(fields) < 2:
                        raise ValueError(f"line {lines}: .Shape requires a name")
                    shape = fields[1].decode("utf-8", "replace")
                    layer_shapes.setdefault(shape, Counter())
                elif first == b".EndShape":
                    shape = None
                elif first == b".Component":
                    tokens = line.decode("utf-8", "replace").split()
                    if len(tokens) < 2:
                        raise ValueError(f"line {lines}: .Component requires a refdes")
                    components[tokens[1]] = " ".join(tokens[2:])
            elif shape is not None:
                match = re.match(rb"([A-Za-z]+)", first)
                kind = match[1].decode() if match else "unknown"
                polarity = chr(first[-1]) if first[-1:] in (b"+", b"-") else "?"
                primitives[kind] += 1
                layer_shapes[shape][kind + polarity] += 1
            elif first.startswith((b"Signal$", b"Medium$")):
                layers.append(line.decode("utf-8", "replace").strip())
            elif first.startswith(b"Node"):
                counts["nodes"] += 1
            elif first.startswith(b"Trace"):
                counts["traces"] += 1
            elif first.startswith(b"Via"):
                counts["vias"] += 1
            if progress and lines % 500000 == 0:
                progress(f"SPD inventory: {processed / path.stat().st_size:.0%}")
    counts.update({"components": directives[".Component"],
                   "parts": directives[".Part"],
                   "padstacks": directives[".PadStackDef"],
                   "connections": directives[".Connect"],
                   "shape_sections": directives[".Shape"],
                   "conductor_layers": sum(x.startswith("Signal$") for x in layers),
                   "dielectric_layers": sum(x.startswith("Medium$") for x in layers)})
    return {"format": "SPD", "path": str(path.resolve()), "bytes": processed,
            "sha256": digest.hexdigest(), "headers": headers, "lines": lines,
            "counts": dict(counts), "shape_primitives": dict(primitives),
            "layer_shapes": {k: dict(v) for k, v in layer_shapes.items()},
            "layers": layers, "directives": dict(directives),
            "components": components, "seconds": round(time.monotonic() - start, 3)}


def inspect_ipc(path: str | Path, progress=None) -> dict:
    """SAX inventory: never materialize the multi-gigabyte element tree."""
    path = Path(path)
    counts, roots, layers = Counter(), {}, []
    components, nets = {}, set()
    parser = expat.ParserCreate(namespace_separator="}")
    digest = hashlib.sha256()
    stack = []
    current_component = None

    def start(name, attrs):
        nonlocal current_component
        tag = name.rsplit("}", 1)[-1]
        stack.append(tag)
        counts[tag] += 1
        if tag == "IPC-2581":
            roots.update(attrs)
        elif tag == "Layer":
            layers.append(dict(attrs))
        elif tag == "Component":
            current_component = attrs.get("refDes")
            components[current_component] = dict(attrs)
        elif tag in ("Location", "Xform") and current_component and len(stack) > 1 and stack[-2] == "Component":
            components[current_component][tag] = dict(attrs)
        elif tag == "LogicalNet":
            nets.add(attrs.get("name", ""))

    def end(name):
        nonlocal current_component
        if name.rsplit("}", 1)[-1] == "Component":
            current_component = None
        stack.pop()

    def reject_doctype(*args):
        raise ValueError("DTD/entity declarations are not allowed in IPC input")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.StartDoctypeDeclHandler = reject_doctype
    parser.ExternalEntityRefHandler = lambda *args: 0
    processed = 0
    before = time.monotonic()
    with path.open("rb") as source:
        while chunk := source.read(4 * 1024 * 1024):
            digest.update(chunk)
            parser.Parse(chunk, False)
            processed += len(chunk)
            if progress and processed % (256 * 1024 * 1024) == 0:
                progress(f"IPC inventory: {processed / path.stat().st_size:.0%}")
        parser.Parse(b"", True)
    return {"format": "IPC-2581", "path": str(path.resolve()), "bytes": processed,
            "sha256": digest.hexdigest(), "root": roots, "counts": dict(counts),
            "layers": layers, "components": components, "net_count": len(nets),
            "seconds": round(time.monotonic() - before, 3)}
