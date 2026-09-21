import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__


def main(argv=None):
    parser = argparse.ArgumentParser(description="PowerSI conversion, native Allegro scripts, and workstation jobs")
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
    skill = commands.add_parser("skill", help="Generate native Allegro plane-update SKILL for a copy of an original BRD")
    skill.add_argument("source", type=Path)
    skill.add_argument("-o", "--output", type=Path, required=True, help="New bundle directory")
    skill.add_argument("--layer", action="append", dest="layers")
    skill.add_argument("--net", action="append", dest="nets")
    skill.add_argument("--update-components", action="store_true")
    serve = commands.add_parser("serve", help="Run the authenticated HTTPS workstation agent until Ctrl+C")
    serve.add_argument("--data-dir", type=Path, default=Path(os.environ.get("LOCALAPPDATA", Path.home())) / "BRD-SPD-IPC2581" / "agent")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--allegro-exe", type=Path, default=Path(r"C:\Cadence\SPB_24.1\tools\bin\allegro.exe"))
    serve.add_argument("--allow-client", action="append", dest="allowed_clients")
    remote = commands.add_parser("remote", help="Control a paired workstation; host/IP can change")
    remote.add_argument("--host", default=os.environ.get("BRDSPD_HOST", "127.0.0.1"))
    remote.add_argument("--port", type=int, default=8765)
    remote.add_argument("--token-file", type=Path, help="Pairing token file, otherwise BRDSPD_TOKEN environment variable")
    remote.add_argument("--fingerprint", required=True, help="Workstation SHA-256 certificate fingerprint")
    actions = remote.add_subparsers(dest="action", required=True)
    actions.add_parser("health")
    actions.add_parser("jobs", help="List the latest workstation jobs for reconnection")
    submit = actions.add_parser("submit")
    submit.add_argument("source", type=Path)
    submit.add_argument("--base-brd", type=Path, required=True)
    submit.add_argument("--layer", action="append", dest="layers")
    submit.add_argument("--net", action="append", dest="nets")
    submit.add_argument("--update-components", action="store_true")
    for action in ("status", "logs", "cancel", "download"):
        command = actions.add_parser(action)
        command.add_argument("job_id")
        if action == "logs":
            command.add_argument("--offset", type=int, default=0)
        if action == "download":
            command.add_argument("-o", "--output", type=Path, required=True)
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
        elif args.command == "skill":
            from .skill import generate_bundle
            result = generate_bundle(args.source, args.output, layers=args.layers,
                                     nets=args.nets, update_components=args.update_components,
                                     progress=lambda s: print(s, flush=True))
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "serve":
            from .remote import AgentConfig, WorkstationAgent
            agent = WorkstationAgent(AgentConfig(datadir=args.data_dir, host=args.host,
                port=args.port, allegro_exe=args.allegro_exe, allowed_clients=args.allowed_clients))
            try:
                info = agent.start()
                print(json.dumps({k: v for k, v in info.items() if k != "token"}, default=str, indent=2), flush=True)
                print(f"Pairing token file: {args.data_dir / 'token.txt'}", flush=True)
                print("Press Ctrl+C to stop the agent.", flush=True)
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
            finally:
                agent.stop()
        elif args.command == "remote":
            from .remote import RemoteClient
            token = (args.token_file.read_text(encoding="utf-8").strip() if args.token_file
                     else os.environ.get("BRDSPD_TOKEN", ""))
            if not token:
                raise ValueError("Provide --token-file or BRDSPD_TOKEN for workstation pairing")
            client = RemoteClient(args.host, port=args.port, token=token, fingerprint=args.fingerprint)
            if args.action == "health":
                result = client.health()
            elif args.action == "jobs":
                result = client.list_jobs()
            elif args.action == "submit":
                # Fail locally before creating a remote job when either input is absent.
                for path in (args.source, args.base_brd):
                    if not path.is_file():
                        raise FileNotFoundError(path)
                job = client.create_job({"layers": args.layers, "nets": args.nets,
                                         "update_components": args.update_components})
                try:
                    client.upload_file(job["id"], "spd", args.source, progress=lambda s: print(s, flush=True))
                    client.upload_file(job["id"], "brd", args.base_brd, progress=lambda s: print(s, flush=True))
                    result = client.submit_job(job["id"])
                except BaseException:
                    try:
                        client.cancel_job(job["id"])
                    except Exception:
                        pass
                    print(f"Upload/submission interrupted; job ID: {job['id']}", file=sys.stderr)
                    raise
            elif args.action == "status":
                result = client.get_job(args.job_id)
            elif args.action == "logs":
                result = client.get_logs(args.job_id, offset=args.offset)
            elif args.action == "cancel":
                result = client.cancel_job(args.job_id)
            else:
                result = client.download_result(args.job_id, args.output, progress=lambda s: print(s, flush=True))
            print(json.dumps(result, default=str, ensure_ascii=False, indent=2))
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
