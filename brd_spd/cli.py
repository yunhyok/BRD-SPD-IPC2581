import argparse
import json
import sys
from pathlib import Path

from . import __version__


def main(argv=None):
    parser = argparse.ArgumentParser(description="PowerSI SPD to IPC-2581B with loss reports")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    convert = commands.add_parser("convert", help="Convert SPD into IPC-2581B XML")
    convert.add_argument("source", type=Path)
    convert.add_argument("-o", "--output", type=Path, required=True)
    convert.add_argument("--template", type=Path, help="Original IPC-2581 XML supplies profile and library metadata")
    convert.add_argument("--xsd", type=Path, help="Optional official IPC-2581B/B1 XSD validation")
    inspect = commands.add_parser("inspect", help="Streaming SPD/IPC inventory and SHA-256")
    inspect.add_argument("source", type=Path)
    inspect.add_argument("-o", "--output", type=Path, required=True)
    validate = commands.add_parser("validate", help="Validate XML against a local official XSD")
    validate.add_argument("source", type=Path)
    validate.add_argument("--xsd", type=Path, required=True)
    native = commands.add_parser("import-brd", help="Run installed Cadence geometry/stackup importer; not full native reconstruction")
    native.add_argument("source", type=Path)
    native.add_argument("-o", "--output", type=Path, required=True)
    native.add_argument("--base-brd", type=Path, required=True)
    native.add_argument("--cadence-root", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "convert":
            from .convert import convert as run
            result = run(args.source, args.output, template=args.template,
                         xsd=args.xsd, progress=lambda s: print(s, flush=True))
            print(json.dumps({k: result[k] for k in ("status", "report_path", "log_path")}, indent=2))
        elif args.command == "inspect":
            from .audit import inspect_spd, inspect_ipc
            if args.output.resolve() == args.source.resolve() or args.output.exists():
                raise ValueError("Inventory output must be a new file, distinct from the input")
            func = inspect_spd if args.source.suffix.lower() == ".spd" else inspect_ipc
            result = func(args.source, lambda s: print(s, flush=True))
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(args.output)
        elif args.command == "validate":
            from .convert import validate_xml
            print(json.dumps(validate_xml(args.source, args.xsd), indent=2))
        elif args.command == "import-brd":
            from .native import import_brd
            print(json.dumps(import_brd(args.source, args.output, args.base_brd, args.cadence_root), indent=2))
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
