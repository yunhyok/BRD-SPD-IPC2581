"""Optional, licensed Cadence import. Never claims native routing reconstruction."""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .report import Report


def import_brd(source, output, base_brd, cadence_root=None):
    source, output, base_brd = (Path(p).resolve() for p in (source, output, base_brd))
    if output.exists() or output in (source, base_brd):
        raise ValueError("Choose a new output BRD; the original board must not be overwritten")
    if not source.is_file() or not base_brd.is_file():
        raise FileNotFoundError("Both IPC XML and original BRD are required")
    binary = (Path(cadence_root) / "tools/bin/ipc2581_in.exe") if cadence_root else None
    if binary is None:
        found = shutil.which("ipc2581_in.exe")
        binary = Path(found) if found else Path("C:/Cadence/SPB_24.1/tools/bin/ipc2581_in.exe")
    if not binary.is_file():
        raise FileNotFoundError("Cadence ipc2581_in.exe not found; provide --cadence-root")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = Report(output)
    report.warn("MANUFACTURING_LAYER_IMPORT", "-x -g imports stackup and manufacturing-layer features; native clines, components, nets, and dynamic shapes are not updated by this command")
    try:
        with tempfile.TemporaryDirectory(prefix="cadence-ipc-", dir=output.parent) as directory:
            staged = Path(directory) / "imported.brd"
            command = [str(binary.resolve()), str(source), "-x", "-g", "-i", str(base_brd), "-o", str(staged)]
            environment = dict(os.environ)
            environment["PATH"] = str(binary.parent.resolve()) + os.pathsep + environment.get("PATH", "")
            result = subprocess.run(command, cwd=directory, env=environment, capture_output=True,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            report.data.update(command=command, exit_code=result.returncode,
                               stdout=result.stdout.decode("utf-8", "replace"), stderr=result.stderr.decode("utf-8", "replace"))
            logs = {}
            for file in Path(directory).glob("*.log"):
                logs[file.name] = file.read_text(encoding="utf-8", errors="replace")
            report.data["cadence_logs"] = logs
            if result.returncode or not staged.is_file() or not staged.stat().st_size:
                raise RuntimeError("Cadence importer failed or did not create a board; see adjacent JSON report")
            if os.name == "nt":
                os.rename(staged, output)
            else:
                os.link(staged, output)
                staged.unlink()
        return report.save("cadence_import_completed_requires_review")
    except BaseException as exc:
        report.save("failed", error=str(exc))
        raise
