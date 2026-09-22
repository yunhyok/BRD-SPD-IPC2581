import json
import os
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from brd_spd.remote import AgentConfig, RemoteClient, WorkstationAgent


class FakeAgent(WorkstationAgent):
    mode = "success"

    def _generate_bundle(self, source, bundle, options):
        bundle.mkdir()
        assert source.name == "input.spd"
        assert source.read_bytes() == b"spd-data"
        (bundle / "run.scr").write_text("fake script", encoding="utf-8")
        (bundle / "generation.log").write_text("generated\n", encoding="utf-8")
        (bundle / "generation.report.json").write_text(
            json.dumps({"options": options, "source_name": source.name}), encoding="utf-8"
        )

    def _allegro_command(self, bundle):
        if self.mode == "sleep":
            code = "import time; time.sleep(30)"
        elif self.mode == "failure":
            code = (
                "import json,pathlib;"
                "pathlib.Path('execution.log').write_text('native failed');"
                "pathlib.Path('result.json').write_text(json.dumps({'status':'failed','error':'test'}))"
            )
        else:
            code = (
                "import json,pathlib;"
                "pathlib.Path('execution.log').write_text('native output');"
                "pathlib.Path('result.brd').write_bytes(b'board-result');"
                "pathlib.Path('result.json').write_text(json.dumps({'status':'success','result_brd':'result.brd'}))"
            )
        return [sys.executable, "-c", code]

    def _start_process(self, command, bundle, log_stream):
        return subprocess.Popen(command, cwd=bundle, stdout=log_stream, stderr=subprocess.STDOUT)


def _start(tmp_path, agent_type=FakeAgent, **config):
    agent = agent_type(AgentConfig(tmp_path / "agent", host="127.0.0.1", port=0, **config))
    info = agent.start()
    client = RemoteClient("127.0.0.1", info["port"], info["token"], info["fingerprint"], timeout=5)
    return agent, client, info


def _inputs(tmp_path):
    spd, brd = tmp_path / "source.spd", tmp_path / "source.brd"
    spd.write_bytes(b"spd-data")
    brd.write_bytes(b"base-board")
    return spd, brd


def _wait(client, job_id, states, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get_job(job_id)
        if job["status"] in states:
            return job
        time.sleep(0.03)
    raise AssertionError(f"job did not reach {states}: {client.get_job(job_id)}")


def test_secure_lifecycle_and_result_archive(tmp_path):
    agent, client, info = _start(tmp_path)
    progress = []
    try:
        assert client.health()["status"] == "ok"
        created = client.create_job({"layers": ["TOP"], "update_components": True})
        spd, brd = _inputs(tmp_path)
        client.upload_file(created["id"], "spd", spd, progress.append)
        client.upload_file(created["id"], "brd", brd, progress.append)
        assert client.submit_job(created["id"])["status"] == "queued"
        finished = _wait(client, created["id"], {"succeeded"})
        assert finished["result_bytes"] == len(b"board-result")
        assert client.list_jobs()[0]["id"] == created["id"]
        logs = client.get_logs(created["id"])
        assert isinstance(logs["next_offset"], int)
        assert "native conversion succeeded" in logs["text"]
        destination = tmp_path / "result.zip"
        assert client.download_result(created["id"], destination, progress.append) == destination.resolve()
        with zipfile.ZipFile(destination) as archive:
            assert set(archive.namelist()) >= {
                "result.brd", "execution.log", "generation.log",
                "generation.report.json", "result.json", "agent-runner.log", "job.json",
            }
            assert "input.spd" not in archive.namelist()
            assert "base.brd" not in archive.namelist()
            assert archive.read("result.brd") == b"board-result"
            assert json.loads(archive.read("job.json"))["status"] == "succeeded"
        assert any(message.startswith("Uploading spd:") for message in progress)
        assert (tmp_path / "agent" / "token.txt").read_text().strip() == info["token"]
    finally:
        agent.stop()


def test_authentication_pinning_and_path_rejection(tmp_path):
    agent, client, info = _start(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="HTTP 401"):
            RemoteClient("127.0.0.1", info["port"], "wrong", info["fingerprint"]).health()
        with pytest.raises(ssl.SSLError, match="fingerprint mismatch"):
            RemoteClient("127.0.0.1", info["port"], info["token"], "00" * 32).health()
        with pytest.raises(ValueError, match="fingerprint is required"):
            RemoteClient("192.0.2.16", info["port"], info["token"])
        response, connection = client._request("GET", "/v1/jobs/%2e%2e/result")
        try:
            assert response.status in {400, 404}
        finally:
            response.read()
            connection.close()
        with pytest.raises(ValueError, match="slot"):
            client.upload_file(str(__import__("uuid").uuid4()), "../spd", tmp_path / "missing")
    finally:
        agent.stop()


def test_cancel_terminates_only_owned_runner_and_returns_logs(tmp_path):
    class SleepingAgent(FakeAgent):
        mode = "sleep"

    agent, client, _ = _start(tmp_path, SleepingAgent)
    try:
        job = client.create_job({})
        spd, brd = _inputs(tmp_path)
        client.upload_file(job["id"], "spd", spd)
        client.upload_file(job["id"], "brd", brd)
        client.submit_job(job["id"])
        _wait(client, job["id"], {"running"})
        client.cancel_job(job["id"])
        assert _wait(client, job["id"], {"cancelled"})["status"] == "cancelled"
        result = client.download_result(job["id"], tmp_path / "cancelled.zip")
        with zipfile.ZipFile(result) as archive:
            assert "agent-runner.log" in archive.namelist()
            assert "result.brd" not in archive.namelist()
    finally:
        agent.stop()


def test_failed_job_has_debug_archive_without_board(tmp_path):
    class FailingAgent(FakeAgent):
        mode = "failure"

    agent, client, _ = _start(tmp_path, FailingAgent)
    try:
        job = client.create_job({"nets": ["PWR"]})
        spd, brd = _inputs(tmp_path)
        client.upload_file(job["id"], "spd", spd)
        client.upload_file(job["id"], "brd", brd)
        client.submit_job(job["id"])
        failed = _wait(client, job["id"], {"failed"})
        assert "native run failed" in failed["error"]
        result = client.download_result(job["id"], tmp_path / "failed.zip")
        with zipfile.ZipFile(result) as archive:
            assert "execution.log" in archive.namelist()
            assert "result.json" in archive.namelist()
            assert "result.brd" not in archive.namelist()
    finally:
        agent.stop()


def test_persistent_token_and_running_job_recovery(tmp_path):
    agent, client, first = _start(tmp_path)
    job = client.create_job({})
    directory = tmp_path / "agent" / "jobs" / job["id"]
    state_path = directory / "job.json"
    state = json.loads(state_path.read_text())
    state["status"] = "running"
    state_path.write_text(json.dumps(state))
    agent.stop()

    replacement, second_client, second = _start(tmp_path)
    try:
        assert second["token"] == first["token"]
        assert second["fingerprint"] == first["fingerprint"]
        recovered = second_client.get_job(job["id"])
        assert recovered["status"] == "interrupted"
        archive = second_client.download_result(job["id"], tmp_path / "interrupted.zip")
        assert zipfile.is_zipfile(archive)
    finally:
        replacement.stop()


def test_upload_limit_and_allowed_client(tmp_path):
    agent, client, _ = _start(tmp_path, max_upload_bytes=4, allowed_clients=["127.0.0.1/32"])
    try:
        job = client.create_job({})
        source = tmp_path / "large.spd"
        source.write_bytes(b"12345")
        with pytest.raises(ValueError, match="upload size"):
            client.upload_file(job["id"], "spd", source)
    finally:
        agent.stop()


def test_idle_tcp_peer_does_not_block_tls_server(tmp_path):
    agent, client, info = _start(tmp_path)
    idle = socket.create_connection(("127.0.0.1", info["port"]), timeout=2)
    try:
        assert client.health()["status"] == "ok"
    finally:
        idle.close()
        agent.stop()


def test_terminal_state_is_published_only_after_archive(tmp_path):
    class SlowArchiveAgent(FakeAgent):
        def __init__(self, config):
            super().__init__(config)
            self.archive_started = threading.Event()
            self.archive_release = threading.Event()

        def _make_result_archive(self, directory, bundle, include_board, job_state=None):
            if job_state and job_state["status"] == "succeeded":
                self.archive_started.set()
                assert self.archive_release.wait(5)
            return super()._make_result_archive(directory, bundle, include_board, job_state)

    agent, client, _ = _start(tmp_path, SlowArchiveAgent)
    try:
        job = client.create_job({})
        spd, brd = _inputs(tmp_path)
        client.upload_file(job["id"], "spd", spd)
        client.upload_file(job["id"], "brd", brd)
        client.submit_job(job["id"])
        assert agent.archive_started.wait(5)
        assert client.get_job(job["id"])["status"] == "running"
        with pytest.raises(RuntimeError, match="HTTP 409"):
            client.download_result(job["id"], tmp_path / "too-early.zip")
        agent.archive_release.set()
        assert _wait(client, job["id"], {"succeeded"})["status"] == "succeeded"
    finally:
        agent.archive_release.set()
        agent.stop()


def test_datadir_cannot_be_used_by_two_running_agents(tmp_path):
    datadir = tmp_path / "agent"
    first = FakeAgent(AgentConfig(datadir, host="127.0.0.1", port=0))
    second = FakeAgent(AgentConfig(datadir, host="127.0.0.1", port=0))
    first.start()
    try:
        with pytest.raises(RuntimeError, match="already using"):
            second.start()
    finally:
        first.stop()
    info = second.start()
    try:
        assert info["port"] > 0
    finally:
        second.stop()


def test_submit_cannot_race_active_reupload(tmp_path):
    agent = FakeAgent(AgentConfig(tmp_path / "agent", host="127.0.0.1", port=0))
    job = agent.create_job({})
    agent.upload(job["id"], "spd", BytesIO(b"old"), 3)
    agent.upload(job["id"], "brd", BytesIO(b"brd"), 3)
    entered, release = threading.Event(), threading.Event()

    class SlowReader:
        def read(self, _size):
            entered.set()
            release.wait(5)
            return b"new"

    errors = []

    def upload():
        try:
            agent.upload(job["id"], "spd", SlowReader(), 3)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=upload)
    thread.start()
    assert entered.wait(2)
    with pytest.raises(RuntimeError, match="active upload"):
        agent.submit(job["id"])
    agent.cancel(job["id"])
    release.set()
    thread.join(5)
    assert errors and "changed state" in str(errors[0])
    assert not (tmp_path / "agent" / "jobs" / job["id"] / "input.spd.upload").exists()


def _local_agent(tmp_path, agent_type=FakeAgent):
    """Agent without the HTTPS server, for deterministic runner-state tests."""
    return agent_type(AgentConfig(tmp_path / "agent", host="127.0.0.1", port=0))


def _upload_inputs(agent, job_id):
    agent.upload(job_id, "spd", BytesIO(b"spd-data"), len(b"spd-data"))
    agent.upload(job_id, "brd", BytesIO(b"base-board"), len(b"base-board"))


def test_cancel_while_queued_is_not_resurrected_into_running(tmp_path):
    generated = []

    class ObservingAgent(FakeAgent):
        def _generate_bundle(self, source, bundle, options):
            generated.append(self._state(bundle.parent.name)["status"])
            return super()._generate_bundle(source, bundle, options)

    agent = _local_agent(tmp_path, ObservingAgent)
    job = agent.create_job({})
    _upload_inputs(agent, job["id"])
    assert agent.submit(job["id"])["status"] == "queued"
    assert agent.cancel(job["id"])["status"] == "cancelled"
    # The runner still holds the queue entry handed over before the cancel;
    # a published terminal state must not be reopened as "running".
    agent._run(job["id"])
    assert generated == []
    assert agent.get_job(job["id"])["status"] == "cancelled"
    with zipfile.ZipFile(agent.result_path(job["id"])) as archive:
        assert "agent-runner.log" in archive.namelist()
        assert "result.brd" not in archive.namelist()


def test_non_ascii_authorization_header_is_answered_with_json_401(tmp_path):
    agent, _client, info = _start(tmp_path)
    try:
        hostile = RemoteClient("127.0.0.1", info["port"], "tökén", info["fingerprint"], timeout=5)
        with pytest.raises(RuntimeError, match="HTTP 401"):
            hostile.health()
        # The connection stays usable for a correct client afterwards.
        assert RemoteClient("127.0.0.1", info["port"], info["token"],
                            info["fingerprint"], timeout=5).health()["status"] == "ok"
    finally:
        agent.stop()


def test_failed_archive_build_leaves_no_temporary_file(tmp_path):
    agent = _local_agent(tmp_path)
    job = agent.create_job({})
    directory = agent._job_dir(job["id"])
    with pytest.raises(TypeError):
        agent._make_result_archive(directory, directory / "bundle", include_board=False,
                                   job_state={"status": "failed", "error": object()})
    assert list(directory.glob("result.zip*")) == []


def test_stop_survives_a_removed_job_directory(tmp_path):
    agent, _client, _info = _start(tmp_path)
    job = agent.create_job({})
    agent._cancel[job["id"]] = threading.Event()
    shutil.rmtree(agent._job_dir(job["id"]))
    agent.stop()
    restarted = agent.start()
    try:
        assert restarted["port"] > 0
    finally:
        agent.stop()


def test_stop_releases_the_agent_when_the_runner_does_not_join(tmp_path):
    class StuckThread:
        def join(self, timeout=None):
            return None

        def is_alive(self):
            return True

    agent, _client, _info = _start(tmp_path)
    agent._worker_thread = StuckThread()
    with pytest.warns(RuntimeWarning, match="cooperative cancellation"):
        agent.stop()
    assert agent._started is False
    restarted = agent.start()
    try:
        assert restarted["port"] > 0
    finally:
        agent.stop()


def test_secrets_are_created_with_owner_only_permissions(tmp_path):
    _local_agent(tmp_path)
    datadir = tmp_path / "agent"
    assert (datadir / "token.txt").is_file()
    if os.name != "nt":
        assert stat.S_IMODE((datadir / "token.txt").stat().st_mode) == 0o600
        assert stat.S_IMODE((datadir / "agent-key.pem").stat().st_mode) == 0o600


def test_running_job_ids_reports_only_non_terminal_jobs(tmp_path):
    agent = _local_agent(tmp_path)
    job = agent.create_job({})
    _upload_inputs(agent, job["id"])
    assert agent.running_job_ids == []
    agent.submit(job["id"])
    assert agent.running_job_ids == [job["id"]]
    agent.cancel(job["id"])
    assert agent.running_job_ids == []


def test_fingerprint_is_normalized_once_and_validated(tmp_path):
    agent, _client, info = _start(tmp_path)
    try:
        formatted = ":".join(info["fingerprint"][i:i + 2] for i in range(0, 64, 2)).upper()
        pinned = RemoteClient("127.0.0.1", info["port"], info["token"],
                              f"  {formatted}  ", timeout=5)
        assert pinned.fingerprint == info["fingerprint"]
        assert pinned.health()["status"] == "ok"
        for value in (":", "00" * 31, "zz" * 32, info["fingerprint"] + "0"):
            with pytest.raises(ValueError, match="64 hex characters"):
                RemoteClient("127.0.0.1", info["port"], info["token"], value)
    finally:
        agent.stop()


def test_cli_turns_an_invalid_fingerprint_into_an_error_exit(tmp_path, capsys):
    from brd_spd.cli import main

    token_file = tmp_path / "pairing-token.txt"
    token_file.write_text("x" * 40, encoding="ascii")
    assert main(["remote", "--host", "192.0.2.16", "--token-file", str(token_file),
                 "--fingerprint", ":", "health"]) == 1
    assert "64 hex characters" in capsys.readouterr().err
