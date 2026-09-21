"""Fast, read-only discovery of native plane layer and net choices in SPD."""
from __future__ import annotations

import os
from concurrent.futures import CancelledError
from pathlib import Path
from typing import Callable

from .spd import _assignments, _shape_identity


_PROGRESS_BYTES = 64 * 1024 * 1024
_CANCEL_LINES = 16_384
_MAX_METADATA_RECORD = 1024 * 1024


def _signature(stat_result) -> tuple[int, int, int, int]:
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
    )


def _native_layer(name: str) -> str:
    for prefix in ("Signal$", "Plane$"):
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def _cancel_if_requested(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise CancelledError("SPD catalog scan cancelled")


def scan_catalog(
    source: Path,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Return selectable native plane layers and nets without parsing geometry."""
    source = Path(source)
    _cancel_if_requested(cancelled)
    if not source.is_file():
        raise FileNotFoundError(source)
    before = source.stat()
    expected_signature = _signature(before)
    total = before.st_size

    if progress is not None:
        progress("SPD catalog: 0%")
    _cancel_if_requested(cancelled)

    conductor_order: list[str] = []
    conductor_seen: set[str] = set()
    shape_layers: dict[str, str] = {}
    positive_nets: dict[str, set[str]] = {}
    shape_section: str | None = None
    pending: str | None = None
    pending_line = 0
    processed = 0
    next_progress = _PROGRESS_BYTES
    next_cancel_line = _CANCEL_LINES

    def process_metadata(record: str, line_number: int) -> None:
        nonlocal shape_section
        record = record.strip()
        if not record:
            return
        if record.startswith(".Shape "):
            if shape_section is not None:
                raise ValueError(
                    f"line {line_number}: nested .Shape before .EndShape for {shape_section!r}"
                )
            fields = record.split()
            if len(fields) < 2:
                raise ValueError(f"line {line_number}: .Shape requires a name")
            shape_section = fields[1]
            return
        if record == ".EndShape":
            shape_section = None
            return
        if record.startswith("Patch"):
            attrs = _assignments(record)
            if "shape" in attrs and "layer" in attrs:
                shape_values = attrs["shape"].split()
                layer_values = attrs["layer"].split()
                if shape_values and layer_values:
                    shape_layers[shape_values[0]] = layer_values[0]
            return
        if record.startswith(("Signal", "Plane", "Medium")) and "=" in record:
            fields = record.split(None, 1)
            if len(fields) < 2:
                return
            name = fields[0]
            if name.startswith(("Signal", "Plane")) and name not in conductor_seen:
                conductor_seen.add(name)
                conductor_order.append(name)

    def start_metadata(text: str, line_number: int) -> None:
        nonlocal pending, pending_line, shape_section
        stripped = text.strip()
        if stripped.startswith(".Shape "):
            process_metadata(stripped, line_number)
        elif stripped == ".EndShape":
            shape_section = None
        elif stripped.startswith(("Patch", "Signal", "Plane", "Medium")):
            pending = stripped
            pending_line = line_number

    def process_shape_line(text: str, line_number: int) -> None:
        nonlocal shape_section
        stripped = text.lstrip()
        if not stripped:
            return
        end = len(stripped)
        for index, character in enumerate(stripped):
            if character.isspace():
                end = index
                break
        token = stripped[:end]
        remainder = stripped[end:]
        if token == ".EndShape" and not remainder.strip():
            shape_section = None
            return
        if token == ".Shape":
            raise ValueError(
                f"line {line_number}: nested .Shape before .EndShape for {shape_section!r}"
            )
        identity = _shape_identity(token)
        if identity is None:
            return
        _kind, _identifier, net, polarity = identity
        if polarity == "+" and net:
            positive_nets.setdefault(shape_section, set()).add(net)

    with source.open("rb") as stream:
        if _signature(source.stat()) != expected_signature or _signature(
            os.fstat(stream.fileno())
        ) != expected_signature:
            raise RuntimeError("SPD source changed before catalog scan opened it")

        for line_number, raw in enumerate(stream, 1):
            processed += len(raw)
            try:
                text = raw.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    f"line {line_number}: SPD is not valid UTF-8 "
                    f"(byte offset {exc.start} within the line)"
                ) from exc

            continuation = text.lstrip().startswith("+")
            if shape_section is not None:
                if not continuation:
                    process_shape_line(text, line_number)
            elif continuation:
                if pending is not None:
                    addition = text.lstrip()[1:].strip()
                    if len(pending) + len(addition) + 1 > _MAX_METADATA_RECORD:
                        raise ValueError(
                            f"line {pending_line}: SPD metadata record exceeds "
                            f"{_MAX_METADATA_RECORD} characters"
                        )
                    pending += " " + addition
            else:
                if pending is not None:
                    process_metadata(pending, pending_line)
                    pending = None
                if shape_section is not None:
                    process_shape_line(text, line_number)
                else:
                    start_metadata(text, line_number)

            if line_number >= next_cancel_line:
                _cancel_if_requested(cancelled)
                next_cancel_line += _CANCEL_LINES
            if processed >= next_progress:
                if progress is not None:
                    progress(f"SPD catalog: {processed / total:.0%}" if total else "SPD catalog")
                _cancel_if_requested(cancelled)
                while processed >= next_progress:
                    next_progress += _PROGRESS_BYTES

        if pending is not None:
            process_metadata(pending, pending_line)
        if shape_section is not None:
            raise ValueError(f"unterminated .Shape {shape_section!r} section at end of file")
        handle_signature = _signature(os.fstat(stream.fileno()))

    _cancel_if_requested(cancelled)
    after_signature = _signature(source.stat())
    if handle_signature != expected_signature or after_signature != expected_signature:
        raise RuntimeError("SPD source changed during catalog scan")

    nets_for_original: dict[str, set[str]] = {}
    for section, layer in shape_layers.items():
        if layer in conductor_seen and positive_nets.get(section):
            nets_for_original.setdefault(layer, set()).update(positive_nets[section])

    layers: list[str] = []
    layer_seen: set[str] = set()
    nets_by_layer_sets: dict[str, set[str]] = {}
    for original in conductor_order:
        layer_nets = nets_for_original.get(original)
        if not layer_nets:
            continue
        native = _native_layer(original)
        if native not in layer_seen:
            layer_seen.add(native)
            layers.append(native)
        nets_by_layer_sets.setdefault(native, set()).update(layer_nets)

    nets_by_layer = {layer: sorted(nets_by_layer_sets[layer]) for layer in layers}
    nets = sorted({net for values in nets_by_layer.values() for net in values})

    if progress is not None:
        progress("SPD catalog: 100%")
    _cancel_if_requested(cancelled)
    return {"layers": layers, "nets": nets, "nets_by_layer": nets_by_layer}
