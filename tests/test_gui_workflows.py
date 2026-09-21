"""Exercise Tk workers/events against local generation and a simulated runner."""
import gc
import threading
import time
import tkinter as tk

import pytest

from brd_spd.gui import (
    DesktopApp, RemoteClientPane, SkillPane, parse_allegro_pid, pid_identity_key,
)
from brd_spd.remote import AgentConfig
from test_integration import ProcessSimulator, SOURCE


CATALOG = {
    "layers": ["TOP"],
    "nets": ["PWR"],
    "nets_by_layer": {"TOP": ["PWR"]},
}


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
    monkeypatch.setattr("brd_spd.gui.messagebox.showinfo", lambda *args, **kwargs: notices.append(("info", args)))
    monkeypatch.setattr("brd_spd.gui.messagebox.showerror", lambda *args, **kwargs: notices.append(("error", args)))
    monkeypatch.setattr("brd_spd.gui.messagebox.showwarning", lambda *args, **kwargs: notices.append(("warning", args)))
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


def load_spd(root, pane, source):
    pane.spd_loading.variable.set(str(source))
    pane.spd_loading.load()
    pump(root, lambda: not pane.spd_loading.loading)
    assert pane.spd_loading.catalog is not None


def test_skill_gui_generates_files_via_worker_queue(ui, tmp_path):
    root, notices = ui
    pane = SkillPane(root)
    source = tmp_path / "board.spd"
    source.write_text(SOURCE, encoding="utf-8")
    output = tmp_path / "scripts"
    load_spd(root, pane, source)
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
        load_spd(root, pane, source)
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
    load_spd(root, pane, source)
    # A BRD path saved from an earlier new-process run must be ignored when
    # this job explicitly targets an existing Allegro PID.
    pane.brd_var.set(str(tmp_path / "stale-missing-base.brd"))
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
    load_spd(root, pane, source)
    pane.output_var.set(str(tmp_path / "result.zip"))
    pane._run()
    pump(root, lambda: not pane._running)
    assert checks == [101, int(changed_pid)]
    assert options_seen[0]["allegro_creation_time"] == 123456789
    assert not [item for item in notices if item[0] == "error"], notices


@pytest.mark.parametrize("pane_type", [SkillPane, RemoteClientPane])
def test_spd_dependent_controls_disabled_until_background_load_succeeds(
        ui, tmp_path, monkeypatch, pane_type):
    root, _notices = ui
    entered, release = threading.Event(), threading.Event()

    def delayed_catalog(source, progress=None, cancelled=None):
        entered.set()
        assert release.wait(3)
        return CATALOG

    monkeypatch.setattr("brd_spd.spd_loading.load_catalog", delayed_catalog)
    pane = pane_type(root)
    assert pane.spd_loading.catalog is None
    assert all(widget.instate(["disabled"]) for widget in pane.spd_loading.controls)

    source = tmp_path / "loading.spd"
    source.write_bytes(b"synthetic")
    pane.spd_loading.variable.set(str(source))
    pane.spd_loading.load()
    assert entered.wait(1)
    assert pane.spd_loading.loading
    assert all(widget.instate(["disabled"]) for widget in pane.spd_loading.controls)

    release.set()
    pump(root, lambda: not pane.spd_loading.loading)
    assert pane.spd_loading.catalog == CATALOG
    assert all(widget.instate(["!disabled"]) for widget in pane.spd_loading.controls)


@pytest.mark.parametrize("contents", [b"", b"\xff\xfe\xfd"], ids=["empty", "invalid"])
def test_invalid_or_empty_spd_keeps_actions_disabled(ui, tmp_path, contents):
    root, notices = ui
    pane = SkillPane(root)
    source = tmp_path / "invalid.spd"
    source.write_bytes(contents)
    pane.source_var.set(str(source))
    pane.spd_loading.load()
    pump(root, lambda: not pane.spd_loading.loading)
    assert pane.spd_loading.catalog is None
    assert all(widget.instate(["disabled"]) for widget in pane.spd_loading.controls)
    assert [item for item in notices if item[0] == "error"]


def test_source_change_resets_catalog_targets_and_components(ui, tmp_path):
    root, _notices = ui
    pane = SkillPane(root)
    first = tmp_path / "first.spd"
    second = tmp_path / "second.spd"
    first.write_text(SOURCE, encoding="utf-8")
    second.write_text(SOURCE, encoding="utf-8")
    load_spd(root, pane, first)
    pane.layers_var.set("TOP")
    pane.nets_var.set("PWR")
    pane.components_var.set(True)

    pane.source_var.set(str(second))
    assert pane.spd_loading.catalog is None
    assert pane.spd_loading.signature is None
    assert pane.layers_var.get() == ""
    assert pane.nets_var.get() == ""
    assert pane.components_var.get() is False
    assert all(widget.instate(["disabled"]) for widget in pane.spd_loading.controls)


@pytest.mark.parametrize(
    ("pane_type", "action_name"),
    [(SkillPane, "_generate"), (RemoteClientPane, "_run")],
    ids=["generate", "remote-run"],
)
def test_file_change_before_action_invalidates_ready_state(
        ui, tmp_path, pane_type, action_name):
    root, notices = ui
    pane = pane_type(root)
    source = tmp_path / "board.spd"
    source.write_text(SOURCE, encoding="utf-8")
    output = tmp_path / "bundle"
    load_spd(root, pane, source)
    if isinstance(pane, SkillPane):
        pane.output_var.set(str(output))

    source.write_text(SOURCE + "\n# changed after load\n", encoding="utf-8")
    getattr(pane, action_name)()
    assert not pane._running
    assert pane.spd_loading.catalog is None
    assert not output.exists()
    assert all(widget.instate(["disabled"]) for widget in pane.spd_loading.controls)
    assert any("먼저 SPD 불러오기" in str(args) for kind, args in notices if kind == "error")


def test_stale_loading_worker_result_is_ignored_after_source_change(
        ui, tmp_path, monkeypatch):
    root, _notices = ui
    pane = SkillPane(root)
    first = tmp_path / "slow.spd"
    second = tmp_path / "current.spd"
    first.write_bytes(b"slow")
    second.write_bytes(b"current")
    first_started, release_first = threading.Event(), threading.Event()
    current_catalog = {
        "layers": ["CURRENT"], "nets": ["CURRENT_NET"],
        "nets_by_layer": {"CURRENT": ["CURRENT_NET"]},
    }

    def catalog(source, progress=None, cancelled=None):
        if source.name == first.name:
            first_started.set()
            assert release_first.wait(3)
            return CATALOG
        return current_catalog

    monkeypatch.setattr("brd_spd.spd_loading.load_catalog", catalog)
    pane.source_var.set(str(first))
    pane.spd_loading.load()
    assert first_started.wait(1)
    pane.source_var.set(str(second))
    pane.spd_loading.load()
    pump(root, lambda: pane.spd_loading.catalog == current_catalog)

    release_first.set()
    deadline = time.monotonic() + 0.3
    while time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)
    assert pane.spd_loading.catalog == current_catalog
    assert pane.spd_loading.status.get().endswith("레이어 1개 · NET 1개")


def test_remote_connect_and_pid_finish_do_not_enable_unloaded_run(
        ui, monkeypatch):
    root, notices = ui

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def health(self):
            return {"status": "ok"}

        def check_pid(self, pid):
            return {
                "pid": pid,
                "executable": r"C:\Cadence\allegro.exe",
                "creation_time": 123,
            }

    monkeypatch.setattr("brd_spd.remote.RemoteClient", FakeClient)
    pane = RemoteClientPane(root)
    pane.host_var.set("127.0.0.1")
    pane.token_var.set("token")
    pane.fingerprint_var.set("fingerprint")
    assert pane.run_button.instate(["disabled"])

    pane._connect()
    pump(root, lambda: not pane._running)
    assert pane.status_var.get() == "연결됨"
    assert pane.run_button.instate(["disabled"])

    pane.pid_var.set("101")
    pane._check_pid()
    pump(root, lambda: not pane._running)
    assert pane.status_var.get() == "PID 확인됨"
    assert pane.run_button.instate(["disabled"])
    assert not [item for item in notices if item[0] == "error"], notices


def test_help_menu_f1_binding_and_disabled_ipc_tab_smoke(ui, monkeypatch):
    root, _notices = ui
    opened = []
    monkeypatch.setattr("brd_spd.help_ui.open_help", lambda parent, topic="overview": opened.append(topic))
    app = DesktopApp(root)
    assert app.notebook.tab(0, "state") == "disabled"
    assert root.bind("<F1>")
    assert app._help_menu.entrycget(0, "label") == "사용 안내"
    app._help_menu.invoke(0)
    assert opened == ["overview"]


@pytest.mark.parametrize(
    ("pane_type", "action_name"),
    [(SkillPane, "_generate"), (RemoteClientPane, "_run")],
    ids=["generate", "remote-run"],
)
def test_open_target_picker_blocks_action_until_closed(
        ui, tmp_path, pane_type, action_name):
    root, notices = ui
    pane = pane_type(root)
    source = tmp_path / "board.spd"
    source.write_text(SOURCE, encoding="utf-8")
    load_spd(root, pane, source)
    if isinstance(pane, SkillPane):
        pane.output_var.set(str(tmp_path / "bundle"))

    layer_picker_button = pane.spd_loading.controls[1]
    layer_picker_button.invoke()
    root.update()
    assert getattr(pane, "_picker_active", False) is True
    assert all(widget.instate(["disabled"]) for widget in pane.spd_loading.controls)

    getattr(pane, action_name)()
    assert not pane._running
    assert not (tmp_path / "bundle").exists()
    assert not [item for item in notices if item[0] == "error"], notices

    from brd_spd.selection_ui import TargetPicker
    picker = next(
        child for child in pane.winfo_children()
        if isinstance(child, TargetPicker)
    )
    picker.close()
    root.update()
    assert getattr(pane, "_picker_active", False) is False
    assert all(widget.instate(["!disabled"]) for widget in pane.spd_loading.controls)


def test_parent_destroy_with_open_picker_has_no_tk_callback_error(
        ui, tmp_path):
    root, _notices = ui
    callback_errors = []
    root.report_callback_exception = lambda *args: callback_errors.append(args)
    pane = SkillPane(root)
    source = tmp_path / "board.spd"
    source.write_text(SOURCE, encoding="utf-8")
    load_spd(root, pane, source)
    pane.spd_loading.controls[1].invoke()
    root.update()
    assert getattr(pane, "_picker_active", False)

    pane.destroy()
    root.update()
    assert callback_errors == []
