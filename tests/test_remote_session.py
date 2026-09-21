import json
import threading
import time
import zipfile
from pathlib import Path

import pytest

from brd_spd.remote import AgentConfig, RemoteClient, WorkstationAgent
from tests.test_remote import _wait


class SessionAgent(WorkstationAgent):
    pid = 4242

    def __init__(self, config, mode="success", fail_identity_check=None,
                 identity_failure=None):
        self.mode = mode
        self.fail_identity_check = fail_identity_check
        self.identity_failure = identity_failure or RuntimeError(
            "process identity changed; the PID may have been reused"
        )
        self.identity_checks = 0
        self.check_calls = []
        self.commands = []
        self.generation_calls = 0
        self.native_ready = threading.Event()
        self.native_release = threading.Event()
        self.native_threads = []
        super().__init__(config)

    @property
    def identity(self):
        return {
            "pid": self.pid,
            "executable": str(self._allegro_executable()),
            "creation_time": 987654321,
        }

    def check_pid(self, pid, expected_identity=None):
        self.check_calls.append((pid, expected_identity))
        assert pid == self.pid
        if expected_identity is not None:
            self.identity_checks += 1
            if self.fail_identity_check == self.identity_checks:
                raise self.identity_failure
            assert expected_identity == self.identity
        return dict(self.identity)

    def _generate_bundle(self, source, bundle, options):
        self.generation_calls += 1
        assert source.read_bytes() == b"session-spd"
        assert options["allegro_pid"] == self.pid
        bundle.mkdir()
        (bundle / "design.il").write_text("; synthetic session bundle\n", encoding="utf-8")
        (bundle / "generation.log").write_text("generated session bundle\n", encoding="utf-8")
        (bundle / "generation.report.json").write_text(
            json.dumps({"status": "generated_unverified", "session_mode": True}),
            encoding="utf-8",
        )
        return {"status": "generated_unverified"}

    def _start_process(self, command, bundle, log_stream):
        raise AssertionError("PID session mode must not launch Allegro")

    def _terminate(self, process):
        raise AssertionError("PID session mode must not terminate the user's Allegro process")

    def _dispatch_session(self, state, command):
        self.commands.append((state, command))
        bundle = self._job_dir(state["id"]) / "bundle"
        assert "skill load(" in command
        assert "design.il" in command
        (bundle / "execution.started").touch()
        if self.mode != "late_success":
            self.native_ready.set()
        if self.mode == "stalled":
            return {"accepted": True, "timed_out": False, "hwnd": 12345}

        def native_side():
            if self.mode == "cancel":
                deadline = time.monotonic() + 5
                while not (bundle / "cancel.flag").is_file() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert (bundle / "cancel.flag").is_file()
                (bundle / "execution.log").write_text("cancel observed\n", encoding="utf-8")
                (bundle / "result.json").write_text(
                    json.dumps({"status": "cancelled"}), encoding="utf-8"
                )
            else:
                backup = bundle / "session-before.brd"
                if self.mode != "missing_backup":
                    backup.write_bytes(b"original-session-board")
                (bundle / "result.brd").write_bytes(b"updated-session-board")
                (bundle / "execution.log").write_text("session success\n", encoding="utf-8")
                (bundle / "result.json").write_text(
                    json.dumps({
                        "status": "success",
                        "result_brd": "result.brd",
                        "design_modified": True,
                        "recovery_brd": (
                            "C:/wrong/session-before.brd"
                            if self.mode == "wrong_backup"
                            else backup.as_posix()
                        ),
                    }),
                    encoding="utf-8",
                )
                if self.mode == "late_success":
                    self.native_ready.set()
                    assert self.native_release.wait(5)
            (bundle / "finished.flag").touch()

        thread = threading.Thread(target=native_side, daemon=True)
        self.native_threads.append(thread)
        thread.start()
        return {"accepted": True, "timed_out": False, "hwnd": 12345}


def _start_session(tmp_path, mode="success", fail_identity_check=None,
                   identity_failure=None, session_timeout_seconds=None):
    executable = tmp_path / "allegro.exe"
    executable.touch()
    config = {
        "host": "127.0.0.1", "port": 0, "allegro_exe": executable,
    }
    if session_timeout_seconds is not None:
        config["session_timeout_seconds"] = session_timeout_seconds
    agent = SessionAgent(
        AgentConfig(tmp_path / "agent", **config),
        mode=mode,
        fail_identity_check=fail_identity_check,
        identity_failure=identity_failure,
    )
    info = agent.start()
    client = RemoteClient(
        "127.0.0.1", info["port"], info["token"], info["fingerprint"], timeout=5
    )
    return agent, client


def _session_job(client, tmp_path):
    job = client.create_job({"allegro_pid": SessionAgent.pid, "layers": ["TOP"]})
    source = tmp_path / "session.spd"
    source.write_bytes(b"session-spd")
    client.upload_file(job["id"], "spd", source)
    return job


def test_https_check_pid_and_job_pins_identity(tmp_path):
    agent, client = _start_session(tmp_path)
    try:
        assert "allegro_pid" in client.health()["capabilities"]
        assert client.check_pid(agent.pid) == agent.identity
        job = client.create_job({"allegro_pid": agent.pid})
        state = client.get_job(job["id"])
        assert state["options"]["allegro_pid"] == agent.pid
        assert state["target_process"] == agent.identity
        assert agent.check_calls[-1] == (agent.pid, None)
        matched = client.create_job({
            "allegro_pid": agent.pid,
            "allegro_creation_time": agent.identity["creation_time"],
        })
        assert client.get_job(matched["id"])["target_process"] == agent.identity
        with pytest.raises(RuntimeError, match="PID was reused"):
            client.create_job({
                "allegro_pid": agent.pid,
                "allegro_creation_time": agent.identity["creation_time"] - 1,
            })
        with pytest.raises(RuntimeError, match="requires allegro_pid"):
            client.create_job({"allegro_creation_time": agent.identity["creation_time"]})
    finally:
        agent.stop()


def test_session_requires_only_spd_rejects_brd_and_revalidates_on_submit(tmp_path):
    agent, client = _start_session(tmp_path)
    try:
        job = client.create_job({"allegro_pid": agent.pid})
        with pytest.raises(RuntimeError, match="required uploads are missing: spd"):
            client.submit_job(job["id"])
        brd = tmp_path / "base.brd"
        brd.write_bytes(b"must-not-upload")
        with pytest.raises(ValueError, match="do not upload a base BRD"):
            client.upload_file(job["id"], "brd", brd)
        # Send only the request headers: the server rejects this slot before
        # consuming a potentially huge body, so a full-body client could see
        # a TCP reset instead of the deliberate HTTP error on some platforms.
        connection = client._connect()
        connection.putrequest("PUT", f"/v1/jobs/{job['id']}/files/brd")
        connection.putheader("Authorization", "Bearer " + client.token)
        connection.putheader("Content-Type", "application/octet-stream")
        connection.putheader("Content-Length", str(brd.stat().st_size))
        connection.endheaders()
        response = connection.getresponse()
        try:
            assert response.status == 400
            assert "do not upload a base BRD" in response.read().decode("utf-8")
        finally:
            connection.close()
        spd = tmp_path / "session.spd"
        spd.write_bytes(b"session-spd")
        client.upload_file(job["id"], "spd", spd)
        assert client.submit_job(job["id"])["status"] == "queued"
        assert agent.identity_checks >= 1
        assert agent.check_calls[-1] == (agent.pid, agent.identity)
    finally:
        agent.stop()


def test_session_success_polls_markers_and_archives_original_board(tmp_path):
    agent, client = _start_session(tmp_path)
    try:
        job = _session_job(client, tmp_path)
        client.submit_job(job["id"])
        finished = _wait(client, job["id"], {"succeeded"})
        assert finished["result_bytes"] == len(b"updated-session-board")
        assert finished["delivery"] == {
            "accepted": True, "timed_out": False, "hwnd": 12345,
        }
        assert len(agent.commands) == 1
        bundle = agent._job_dir(job["id"]) / "bundle"
        assert (bundle / "execution.started").is_file()
        assert (bundle / "finished.flag").is_file()
        result = client.download_result(job["id"], tmp_path / "session-result.zip")
        with zipfile.ZipFile(result) as archive:
            assert archive.read("result.brd") == b"updated-session-board"
            assert archive.read("session-before.brd") == b"original-session-board"
            assert json.loads(archive.read("result.json"))["status"] == "success"
    finally:
        agent.stop()


def test_session_cancel_is_cooperative_and_does_not_terminate_process(tmp_path):
    agent, client = _start_session(tmp_path, mode="cancel")
    try:
        job = _session_job(client, tmp_path)
        client.submit_job(job["id"])
        assert agent.native_ready.wait(5)
        client.cancel_job(job["id"])
        finished = _wait(client, job["id"], {"cancelled"})
        bundle = agent._job_dir(job["id"]) / "bundle"
        assert (bundle / "cancel.flag").is_file()
        assert finished["status"] == "cancelled"
        archive = client.download_result(job["id"], tmp_path / "cancelled-session.zip")
        with zipfile.ZipFile(archive) as result:
            assert "result.brd" not in result.namelist()
    finally:
        agent.stop()


def test_native_success_wins_when_cancel_arrives_after_dispatch(tmp_path):
    agent, client = _start_session(tmp_path, mode="late_success")
    try:
        job = _session_job(client, tmp_path)
        client.submit_job(job["id"])
        assert agent.native_ready.wait(5)
        client.cancel_job(job["id"])
        agent.native_release.set()
        finished = _wait(client, job["id"], {"succeeded"})
        assert finished["cancellation_too_late"] is True
        assert finished["status"] == "succeeded"
    finally:
        agent.native_release.set()
        agent.stop()


@pytest.mark.parametrize("failure,fragment", [
    (RuntimeError("process identity changed; the PID may have been reused"), "PID may have been reused"),
    (ProcessLookupError("process closed before dispatch"), "process closed before dispatch"),
])
def test_reused_or_closed_pid_before_dispatch_fails_with_debug_archive(
        tmp_path, failure, fragment):
    # First expected-identity check occurs at submit; the second occurs just
    # before dispatch after bundle generation.
    agent, client = _start_session(
        tmp_path, fail_identity_check=2, identity_failure=failure
    )
    try:
        job = _session_job(client, tmp_path)
        client.submit_job(job["id"])
        failed = _wait(client, job["id"], {"failed"})
        assert fragment in failed["error"]
        assert not agent.commands
        archive = client.download_result(job["id"], tmp_path / "pid-failed.zip")
        with zipfile.ZipFile(archive) as result:
            assert "agent-runner.log" in result.namelist()
            assert "generation.report.json" in result.namelist()
            assert "result.brd" not in result.namelist()
    finally:
        agent.stop()


def _persist_dispatched_job(agent, status, finished):
    job = agent.create_job({"allegro_pid": agent.pid})
    directory = agent._job_dir(job["id"])
    bundle = directory / "bundle"
    bundle.mkdir()
    (bundle / "design.il").write_text("; already dispatched\n", encoding="utf-8")
    (bundle / "execution.started").touch()
    (bundle / "execution.log").write_text("recovered native success\n", encoding="utf-8")
    (bundle / "session-before.brd").write_bytes(b"recovered-original")
    (bundle / "result.brd").write_bytes(b"recovered-result")
    (bundle / "result.json").write_text(
        json.dumps({
            "status": "success",
            "result_brd": "result.brd",
            "design_modified": True,
            "recovery_brd": (bundle / "session-before.brd").as_posix(),
        }),
        encoding="utf-8",
    )
    if finished:
        (bundle / "finished.flag").touch()
    agent._update(
        job["id"], status=status, session_dispatched=True,
        delivery={"accepted": True, "timed_out": False, "hwnd": 12345},
    )
    return job["id"], bundle


@pytest.mark.parametrize("persisted_status", ["running", "cancel_requested"])
def test_restart_recovers_finished_dispatched_session_without_redispatch(
        tmp_path, persisted_status):
    executable = tmp_path / "allegro.exe"
    executable.touch()
    agent = SessionAgent(AgentConfig(
        tmp_path / "agent", host="127.0.0.1", port=0, allegro_exe=executable
    ))
    job_id, _bundle = _persist_dispatched_job(agent, persisted_status, finished=True)
    info = agent.start()
    client = RemoteClient(
        "127.0.0.1", info["port"], info["token"], info["fingerprint"], timeout=5
    )
    try:
        finished = _wait(client, job_id, {"succeeded"})
        assert finished["status"] == "succeeded"
        assert agent.generation_calls == 0
        assert agent.commands == []
        result = client.download_result(job_id, tmp_path / f"recovered-{persisted_status}.zip")
        with zipfile.ZipFile(result) as archive:
            assert archive.read("result.brd") == b"recovered-result"
            assert archive.read("session-before.brd") == b"recovered-original"
    finally:
        agent.stop()


def test_cancelled_recovered_queue_still_observes_late_native_success(tmp_path):
    executable = tmp_path / "allegro.exe"
    executable.touch()
    agent = SessionAgent(AgentConfig(
        tmp_path / "agent", host="127.0.0.1", port=0, allegro_exe=executable
    ))
    job_id, bundle = _persist_dispatched_job(agent, "running", finished=False)

    # Recover without starting the worker so cancellation deterministically
    # lands while the dispatched observation job is queued.
    agent._recover()
    assert agent.get_job(job_id)["status"] == "queued"
    cancelled = agent.cancel(job_id)
    assert cancelled["status"] == "cancel_requested"
    assert (bundle / "cancel.flag").is_file()
    assert not (agent._job_dir(job_id) / "result.zip").exists()

    # Native execution had already committed success and publishes its final
    # marker after the cancellation request. Observation must preserve it.
    (bundle / "finished.flag").touch()
    agent._run(job_id)
    finished = agent.get_job(job_id)
    assert finished["status"] == "succeeded"
    assert finished["cancellation_too_late"] is True
    assert agent.generation_calls == 0
    assert agent.commands == []
    with zipfile.ZipFile(agent.result_path(job_id)) as archive:
        assert archive.read("result.brd") == b"recovered-result"


def _write_late_session_success(bundle):
    backup = bundle / "session-before.brd"
    backup.write_bytes(b"late-original")
    (bundle / "result.brd").write_bytes(b"late-result")
    (bundle / "execution.log").write_text("late native success\n", encoding="utf-8")
    (bundle / "result.json").write_text(json.dumps({
        "status": "success",
        "result_brd": "result.brd",
        "design_modified": True,
        "recovery_brd": backup.as_posix(),
    }), encoding="utf-8")
    (bundle / "finished.flag").touch()


@pytest.mark.parametrize("trigger", ["get", "list", "download"])
def test_stalled_session_reconciles_late_success_without_redispatch(tmp_path, trigger):
    agent, client = _start_session(
        tmp_path, mode="stalled", session_timeout_seconds=0.1
    )
    try:
        job = _session_job(client, tmp_path)
        client.submit_job(job["id"])
        interrupted = _wait(client, job["id"], {"interrupted"})
        assert interrupted["native_pending"] is True
        assert interrupted["design_modified"] is None
        bundle = agent._job_dir(job["id"]) / "bundle"
        assert (bundle / "execution.started").is_file()
        assert (bundle / "cancel.flag").is_file()
        assert len(agent.commands) == 1
        assert agent.generation_calls == 1

        _write_late_session_success(bundle)
        archive_path = tmp_path / f"late-{trigger}.zip"
        if trigger == "get":
            reconciled = client.get_job(job["id"])
        elif trigger == "list":
            reconciled = next(item for item in client.list_jobs() if item["id"] == job["id"])
        else:
            client.download_result(job["id"], archive_path)
            reconciled = client.get_job(job["id"])

        assert reconciled["status"] == "succeeded"
        assert reconciled["native_pending"] is False
        assert reconciled["design_modified"] is True
        assert len(agent.commands) == 1
        assert agent.generation_calls == 1
        if not archive_path.exists():
            client.download_result(job["id"], archive_path)
        with zipfile.ZipFile(archive_path) as archive:
            assert archive.read("result.brd") == b"late-result"
            assert archive.read("session-before.brd") == b"late-original"
    finally:
        agent.stop()


@pytest.mark.parametrize("mode", ["missing_backup", "wrong_backup"])
def test_session_success_without_valid_recovery_contract_fails(tmp_path, mode):
    agent, client = _start_session(tmp_path, mode=mode)
    try:
        job = _session_job(client, tmp_path)
        client.submit_job(job["id"])
        failed = _wait(client, job["id"], {"failed"})
        assert "recovery contract incomplete" in failed["error"]
        assert failed["design_modified"] is True
        archive = client.download_result(job["id"], tmp_path / f"{mode}.zip")
        with zipfile.ZipFile(archive) as result:
            assert "result.brd" not in result.namelist()
    finally:
        agent.stop()


def test_download_rejects_stale_archive_during_late_result_refresh(tmp_path, monkeypatch):
    agent, client = _start_session(tmp_path, mode="stalled", session_timeout_seconds=0.1)
    entered, release = threading.Event(), threading.Event()
    original = agent._make_result_archive
    results = []

    def slow_archive(directory, bundle, include_board, job_state=None):
        if job_state and job_state["status"] == "succeeded":
            entered.set()
            assert release.wait(5)
        return original(directory, bundle, include_board, job_state)

    thread = None
    try:
        job = _session_job(client, tmp_path)
        client.submit_job(job["id"])
        _wait(client, job["id"], {"interrupted"})
        monkeypatch.setattr(agent, "_make_result_archive", slow_archive)
        _write_late_session_success(agent._job_dir(job["id"]) / "bundle")
        thread = threading.Thread(target=lambda: results.append(client.get_job(job["id"])))
        thread.start()
        assert entered.wait(3)
        with pytest.raises(RuntimeError, match="refresh is in progress"):
            client.download_result(job["id"], tmp_path / "stale.zip")
        assert not (tmp_path / "stale.zip").exists()
        release.set()
        thread.join(5)
        assert results[0]["status"] == "succeeded"
    finally:
        release.set()
        if thread is not None:
            thread.join(5)
        agent.stop()
