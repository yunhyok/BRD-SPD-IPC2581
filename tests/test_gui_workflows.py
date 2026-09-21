"""Exercise Tk workers/events against local generation and a simulated runner."""
import gc
import time
import tkinter as tk

import pytest

from brd_spd.gui import SkillPane, RemoteClientPane, parse_allegro_pid, pid_identity_key
from brd_spd.remote import AgentConfig
from test_integration import ProcessSimulator, SOURCE


@pytest.mark.parametrize("module_name", ["gui", "agent_gui"])
def test_actual_entrypoint_initializes_font_and_window(tmp_path, monkeypatch, module_name):
    from importlib import import_module
    module = import_module("brd_spd." + module_name)
    monkeypatch.setenv("APPDATA", str(tmp_path / "settings"))
    original = tk.Tk
    roots = []
    def hidden_root():
        try:
            root = original()
        except tk.TclError:
            pytest.skip("Tk display unavailable")
        root.withdraw()
        root.after(50, root.quit)
        roots.append(root)
        return root
    monkeypatch.setattr(tk, "Tk", hidden_root)
    try:
        module.main()
        assert roots
    finally:
        for root in roots:
            # ``mainloop`` returns after ``quit`` but panes retain their periodic
            # event-queue timers.  Cancel those timers before tearing down this
            # short-lived interpreter, so they cannot fire during the next Tk
            # test after their Python command has been removed.
            try:
                for callback in root.tk.call("after", "info"):
                    root.after_cancel(callback)
                # Release panes and their Tk variables while the interpreter is
                # still valid.  ``root.destroy`` alone leaves Python references
                # until GC, whose later Variable finalizers would touch a dead
                # interpreter.
                for child in root.winfo_children():
                    child.destroy()
                gc.collect()
                root.destroy()
            except tk.TclError:
                pass
        roots.clear()
        try:
            del root
        except UnboundLocalError:
            pass
        gc.collect()


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "settings"))
    notices = []
    monkeypatch.setattr("brd_spd.gui.messagebox.showinfo", lambda *args: notices.append(("info", args)))
    monkeypatch.setattr("brd_spd.gui.messagebox.showerror", lambda *args: notices.append(("error", args)))
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Tk display unavailable")
    root.withdraw()
    yield root, notices
    try:
        for callback in root.tk.call("after", "info"):
            root.after_cancel(callback)
        for child in root.winfo_children():
            child.destroy()
        gc.collect()
        root.destroy()
    except tk.TclError:
        pass


def pump(root, done):
    deadline = time.monotonic() + 15
    while not done() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.02)
    root.update()
    assert done(), "GUI worker did not finish"


def test_skill_gui_generates_files_via_worker_queue(ui, tmp_path):
    root, notices = ui
    pane = SkillPane(root)
    source = tmp_path / "board.spd"
    source.write_text(SOURCE, encoding="utf-8")
    output = tmp_path / "scripts"
    pane.source_var.set(str(source))
    pane.output_var.set(str(output))
    pane.layers_var.set("TOP")
    pane.nets_var.set("PWR")
    pane._generate()
    pump(root, lambda: not pane._running)
    assert (output / "design.il").is_file()
    assert not [item for item in notices if item[0] == "error"], notices


def test_remote_gui_uploads_polls_and_downloads_simulated_result(ui, tmp_path):
    root, notices = ui
    agent = ProcessSimulator(AgentConfig(tmp_path / "agent", host="127.0.0.1", port=0))
    info = agent.start()
    try:
        pane = RemoteClientPane(root)
        source, base = tmp_path / "board.spd", tmp_path / "base.brd"
        source.write_text(SOURCE, encoding="utf-8")
        base.write_bytes(b"synthetic-base")
        output = tmp_path / "result.zip"
        pane.host_var.set("127.0.0.1")
        pane.port_var.set(str(info["port"]))
        pane.token_var.set(info["token"])
        pane.fingerprint_var.set(info["fingerprint"])
        pane.spd_var.set(str(source))
        pane.brd_var.set(str(base))
        pane.output_var.set(str(output))
        pane.layers_var.set("TOP")
        pane.nets_var.set("PWR")
        pane._run()
        pump(root, lambda: not pane._running)
        assert output.is_file(), notices
        assert pane.job_var.get()
        assert not [item for item in notices if item[0] == "error"], notices
    finally:
        agent.stop()


def test_parse_allegro_pid_is_optional_and_bounded():
    assert parse_allegro_pid("") is None
    assert parse_allegro_pid(" 42 ") == 42
    assert parse_allegro_pid("4294967295") == 4294967295
    with pytest.raises(ValueError):
        parse_allegro_pid("0")
    with pytest.raises(ValueError):
        parse_allegro_pid("42.0")
    with pytest.raises(ValueError):
        parse_allegro_pid("4294967296")


def test_remote_gui_existing_allegro_pid_skips_brd_upload(ui, tmp_path, monkeypatch):
    root, notices = ui
    uploads, options_seen = [], []

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def health(self):
            return {"ok": True}

        def check_pid(self, pid):
            return {"pid": pid, "executable": r"C:\Cadence\allegro.exe", "creation_time": 987654321}

        def create_job(self, options):
            options_seen.append(options)
            return {"id": "job-1", "status": "created"}

        def upload_file(self, _job, slot, _path, progress=None):
            uploads.append(slot)
            if progress:
                progress("ok")

        def submit_job(self, _job):
            return {"status": "queued"}

        def get_job(self, _job):
            return {"status": "succeeded"}

        def get_logs(self, _job, offset=0):
            return {"text": "", "next_offset": offset}

        def download_result(self, _job, destination, progress=None):
            destination.write_bytes(b"zip")

    monkeypatch.setattr("brd_spd.remote.RemoteClient", FakeClient)
    pane = RemoteClientPane(root)
    source, output = tmp_path / "board.spd", tmp_path / "result.zip"
    source.write_text(SOURCE, encoding="utf-8")
    pane.host_var.set("127.0.0.1")
    pane.token_var.set("test-token")
    pane.fingerprint_var.set("fingerprint")
    pane.spd_var.set(str(source))
    pane.output_var.set(str(output))
    pane.pid_var.set("101")
    pane._run()
    pump(root, lambda: not pane._running)
    assert output.read_bytes() == b"zip"
    assert uploads == ["spd"]
    assert options_seen == [{"update_components": False, "allegro_pid": 101, "allegro_creation_time": 987654321}]
    assert not [item for item in notices if item[0] == "error"], notices


def test_remote_gui_checks_allegro_pid_in_worker(ui, monkeypatch):
    root, notices = ui

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def check_pid(self, pid):
            return {"pid": pid, "executable": r"C:\Cadence\allegro.exe", "creation_time": 987654321}

    monkeypatch.setattr("brd_spd.remote.RemoteClient", FakeClient)
    pane = RemoteClientPane(root)
    pane.host_var.set("127.0.0.1")
    pane.token_var.set("test-token")
    pane.fingerprint_var.set("fingerprint")
    pane.pid_var.set("101")
    pane._check_pid()
    pump(root, lambda: not pane._running)
    assert pane.status_var.get() == "PID 확인됨"
    assert "C:\\Cadence\\allegro.exe" in pane.log.get("1.0", "end")
    assert not [item for item in notices if item[0] == "error"], notices


@pytest.mark.parametrize(
    ("changed_host", "changed_pid"),
    [("127.0.0.2", "101"), ("127.0.0.1", "102")],
    ids=["host", "pid"],
)
def test_remote_gui_does_not_reuse_checked_pid_identity_after_endpoint_change(ui, tmp_path, monkeypatch, changed_host, changed_pid):
    root, notices = ui
    checks, options_seen = [], []

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def health(self):
            return {"ok": True}

        def check_pid(self, pid):
            checks.append(pid)
            return {"pid": pid, "executable": r"C:\Cadence\allegro.exe", "creation_time": 123456789}

        def create_job(self, options):
            options_seen.append(options)
            return {"id": "job-2", "status": "created"}

        def upload_file(self, _job, _slot, _path, progress=None):
            if progress:
                progress("ok")

        def submit_job(self, _job):
            return {"status": "queued"}

        def get_job(self, _job):
            return {"status": "succeeded"}

        def get_logs(self, _job, offset=0):
            return {"text": "", "next_offset": offset}

        def download_result(self, _job, destination, progress=None):
            destination.write_bytes(b"zip")

    monkeypatch.setattr("brd_spd.remote.RemoteClient", FakeClient)
    pane = RemoteClientPane(root)
    source = tmp_path / "board.spd"
    source.write_text(SOURCE, encoding="utf-8")
    pane.host_var.set("127.0.0.1")
    pane.port_var.set("8765")
    pane.token_var.set("test-token")
    pane.fingerprint_var.set("fingerprint")
    pane.pid_var.set("101")
    pane._check_pid()
    pump(root, lambda: not pane._running)
    assert pane._checked_pid == (pid_identity_key("127.0.0.1", 8765, "fingerprint", 101), 123456789)

    # A changed host or PID creates a different key, so Run checks again.
    pane.host_var.set(changed_host)
    pane.pid_var.set(changed_pid)
    pane.spd_var.set(str(source))
    pane.output_var.set(str(tmp_path / "result.zip"))
    pane._run()
    pump(root, lambda: not pane._running)
    assert checks == [101, int(changed_pid)]
    assert options_seen[0]["allegro_creation_time"] == 123456789
    assert not [item for item in notices if item[0] == "error"], notices
