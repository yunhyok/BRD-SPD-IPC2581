"""Cross-module tests. The process simulator is deliberately NOT an Allegro test."""
import json
import sys
import time
import zipfile
from pathlib import Path

from brd_spd.cli import main
from brd_spd.remote import AgentConfig, RemoteClient, WorkstationAgent


SOURCE = """Title integration synthetic
.Package $Package
.Shape shape_top
Polygon1::PWR+ 0mm 0mm 10mm 0mm 10mm 10mm 0mm 10mm
Circle2::PWR- 5mm 5mm 1mm
.EndShape
Signal$TOP Thickness = 35u Material = COPPER
PatchSignal$TOP Shape = shape_top Layer = Signal$TOP
.EndPackage
"""


class ProcessSimulator(WorkstationAgent):
    """Use the real generator/transport; substitute only the licensed executable."""
    def _allegro_command(self, bundle):
        assert (bundle / "design.il").stat().st_size > 0
        assert (bundle / "run.scr").stat().st_size > 0
        assert (bundle / "generation.report.json").is_file()
        return [sys.executable, "-c", (
            "from pathlib import Path; import json; "
            "assert Path('base.brd').read_bytes() == b'synthetic-base'; "
            "Path('execution.log').write_text('SIMULATED process, no Allegro validation\\n'); "
            "Path('result.brd').write_bytes(b'SIMULATED-RESULT'); "
            "Path('result.json').write_text(json.dumps({'status':'success'}))"
        )]


def test_cli_generates_bundle_and_preserves_input(tmp_path):
    source = tmp_path / "edited.spd"
    source.write_text(SOURCE, encoding="utf-8")
    output = tmp_path / "scripts"
    assert main(["skill", str(source), "-o", str(output), "--layer", "TOP", "--net", "PWR"]) == 0
    assert (output / "design.il").is_file()
    assert (output / "generation.report.json").is_file()
    assert source.read_text(encoding="utf-8") == SOURCE
    before = (output / "design.il").read_bytes()
    assert main(["skill", str(source), "-o", str(output)]) == 1
    assert (output / "design.il").read_bytes() == before


def test_real_generator_over_https_with_simulated_process(tmp_path):
    source, base = tmp_path / "edited.spd", tmp_path / "original.brd"
    source.write_text(SOURCE, encoding="utf-8")
    base.write_bytes(b"synthetic-base")
    agent = ProcessSimulator(AgentConfig(tmp_path / "agent", host="127.0.0.1", port=0))
    info = agent.start()
    client = RemoteClient("127.0.0.1", info["port"], info["token"], info["fingerprint"], timeout=5)
    try:
        job = client.create_job({"layers": ["TOP"], "nets": ["PWR"], "update_components": False})
        client.upload_file(job["id"], "spd", source)
        client.upload_file(job["id"], "brd", base)
        client.submit_job(job["id"])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = client.get_job(job["id"])
            if state["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.05)
        assert state["status"] == "succeeded", state
        archive_path = client.download_result(job["id"], tmp_path / "result.zip")
        with zipfile.ZipFile(archive_path) as archive:
            assert archive.read("result.brd") == b"SIMULATED-RESULT"
            assert b"no Allegro validation" in archive.read("execution.log")
            report = json.loads(archive.read("generation.report.json"))
            assert report["counts"]
            assert "input.spd" not in archive.namelist()
        assert base.read_bytes() == b"synthetic-base"
        # Also exercise the CLI client against the actual HTTPS service.
        token_file = tmp_path / "pairing-token.txt"
        token_file.write_text(info["token"], encoding="ascii")
        assert main(["remote", "--host", "127.0.0.1", "--port", str(info["port"]),
                     "--token-file", str(token_file), "--fingerprint", info["fingerprint"],
                     "status", job["id"]]) == 0
    finally:
        agent.stop()
