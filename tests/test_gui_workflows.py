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
                # Release panes and their Tk variables while the interpreter is
                # still valid.  ``root.destroy`` alone leaves Python references
                # until GC, whose later Variable finalizers would touch a dead
                # interpreter.
                for child in root.winfo_children():
                    child.destroy()
                for callback in root.tk.call("after", "info"):
                    root.after_cancel(callback)
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
    # Confirmations answer "yes" unless a test patches this again.
    monkeypatch.setattr("brd_spd.gui.messagebox.askyesno",
                        lambda *args, **kwargs: notices.append(("askyesno", args)) or True)
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Tk display unavailable")
    root.withdraw()
    yield root, notices
    try:
        for child in root.winfo_children():
            child.destroy()
        for callback in root.tk.call("after", "info"):
            root.after_cancel(callback)
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


def test_help_menu_f1_binding_and_hidden_ipc_tab_smoke(ui, monkeypatch):
    root, _notices = ui
    opened = []
    monkeypatch.setattr("brd_spd.help_ui.open_help", lambda parent, topic="overview": opened.append(topic))
    app = DesktopApp(root)
    # The disabled IPC conversion tab is left out entirely, so SKILL is first.
    tabs = [app.notebook.tab(index, "text") for index in range(app.notebook.index("end"))]
    assert tabs == ["Native SKILL 생성", "원격 Allegro 작업"]
    assert app.notebook.tab(0, "state") == "normal"
    assert root.bind("<F1>")
    assert app._help_menu.entrycget(0, "label") == "시작하기"
    app._help_menu.invoke(0)
    assert opened == ["overview"]


def test_f1_topic_follows_selected_tab_and_falls_back(ui):
    root, _notices = ui
    app = DesktopApp(root)
    assert app._help_topic() == "remote-new"
    app.remote.client.pid_var.set("101")
    assert app._help_topic() == "remote-pid"
    app.notebook.select(app.skill)
    assert app._help_topic() == "native-skill"
    app.notebook.select = lambda: ".unknown-tab"
    assert app._help_topic() == "overview"


def test_remote_roles_show_only_matching_controls_and_restore_choice(ui):
    from brd_spd.gui import load_settings

    root, _notices = ui
    app = DesktopApp(root)
    remote = app.remote
    assert app.notebook.select() == str(remote)
    assert remote.role == "laptop"
    assert remote.agent is None  # Selecting/opening the tab must never start a server.
    assert remote.client.winfo_manager() == "grid"
    remote.client.host_var.set("192.0.2.16")
    remote.client.pid_var.set("123")
    assert remote.help_topic() == "remote-pid"
    menu = root.cget("menu")
    close_handler = root.protocol("WM_DELETE_WINDOW")

    remote.role_buttons[1].invoke()
    assert remote.role == "workstation"
    assert remote.client.winfo_manager() == ""
    assert remote.agent.winfo_manager() == "grid"
    assert remote.agent._agent is None
    assert remote.help_topic() == "workstation"
    assert app.notebook.tab(app.skill, "state") == "disabled"
    assert root.cget("menu") == menu
    assert root.protocol("WM_DELETE_WINDOW") == close_handler
    assert root.title() == "BRD-SPD-IPC2581"
    assert load_settings()["remote_role"] == "workstation"

    remote.role_buttons[0].invoke()
    assert remote.agent.winfo_manager() == ""
    assert remote.client.host_var.get() == "192.0.2.16"
    assert remote.client.pid_var.get() == "123"
    assert app.notebook.tab(app.skill, "state") == "normal"
    assert remote.client.run_button.instate(["disabled"])
    remote.role_buttons[1].invoke()
    app.destroy()
    restored = DesktopApp(root)
    assert restored.remote.role == "workstation"
    assert restored.remote.agent._agent is None
    assert restored.notebook.tab(restored.skill, "state") == "disabled"


@pytest.mark.parametrize("busy_state", ["remote", "native", "loading", "picker"])
def test_remote_role_cannot_hide_active_work(ui, busy_state):
    root, _notices = ui
    app = DesktopApp(root)
    remote = app.remote
    if busy_state == "remote":
        remote.client._running = True
    elif busy_state == "native":
        app.skill._running = True
    elif busy_state == "loading":
        remote.client.spd_loading.loading = True
    else:
        remote.client._picker_active = True
    remote.role_buttons[1].invoke()  # Guard also works before the periodic UI refresh.
    assert remote.role == remote.role_var.get() == "laptop"
    assert remote.agent is None
    pump(root, lambda: remote.role_buttons[1].instate(["disabled"]))
    remote.client._running = app.skill._running = False
    remote.client.spd_loading.loading = False
    remote.client._picker_active = False
    pump(root, lambda: remote.role_buttons[1].instate(["!disabled"]))
    remote.role_buttons[1].invoke()
    assert remote.role == "workstation"


def test_embedded_workstation_agent_start_health_stop_and_role_unlock(ui, tmp_path):
    import sys
    from brd_spd.remote import RemoteClient

    root, notices = ui
    app = DesktopApp(root)
    remote = app.remote
    remote.role_buttons[1].invoke()
    agent = remote.agent
    agent.host_var.set("127.0.0.1")
    agent.port_var.set("0")
    agent.exe_var.set(sys.executable)  # Health only; no job or process execution.
    agent.workdir_var.set(str(tmp_path / "embedded-agent"))
    try:
        agent.start_button.invoke()
        assert not agent.can_switch_role
        remote.role_buttons[0].invoke()
        assert remote.role == "workstation"
        pump(root, lambda: agent._agent is not None and not agent._busy)
        server = agent._agent
        client = RemoteClient("127.0.0.1", port=server._server.server_address[1],
                              token=agent.token_var.get(), fingerprint=agent.fingerprint_var.get())
        assert client.health()["status"] == "ok"
        assert not agent.can_switch_role
        agent.stop_button.invoke()
        assert not agent.can_switch_role
        pump(root, lambda: agent.can_switch_role)
        assert not agent.token_var.get()
        assert not agent.fingerprint_var.get()
        pump(root, lambda: remote.role_buttons[0].instate(["!disabled"]))
        remote.role_buttons[0].invoke()
        assert remote.role == "laptop"
        assert not [item for item in notices if item[0] == "error"], notices
    finally:
        if agent._agent is not None:
            agent._agent.stop()


def test_close_during_agent_start_waits_for_stop_and_allows_stop_retry(ui, tmp_path, monkeypatch):
    import sys

    root, notices = ui
    release_start = threading.Event()
    stop_calls = []
    destroyed = []

    class SlowAgent:
        def __init__(self, _config):
            pass

        def start(self):
            assert release_start.wait(5)
            return {"host": "127.0.0.1", "port": 0, "token": "test", "fingerprint": "test"}

        def stop(self):
            stop_calls.append(True)
            if len(stop_calls) == 1:
                raise RuntimeError("cancellation pending")

    monkeypatch.setattr("brd_spd.remote.WorkstationAgent", SlowAgent)
    app = DesktopApp(root)
    app.remote.role_buttons[1].invoke()
    agent = app.remote.agent
    agent.exe_var.set(sys.executable)
    agent.workdir_var.set(str(tmp_path / "agent"))
    with monkeypatch.context() as close_patch:
        close_patch.setattr(root, "destroy", lambda: destroyed.append(True))
        try:
            agent.start_button.invoke()
            assert all(widget.instate(["disabled"]) for widget in agent._config_controls)
            app._on_close()
            assert agent._closing
            assert not destroyed
            release_start.set()
            pump(root, lambda: bool(stop_calls) and not agent._busy)
            assert agent._agent is not None
            assert not destroyed
            assert agent.stop_button.instate(["!disabled"])
            assert agent.start_button.instate(["disabled"])
            assert not agent.can_switch_role
            agent.stop_button.invoke()
            pump(root, lambda: bool(destroyed))
            assert len(stop_calls) == 2
            assert agent._agent is None
            assert not agent.token_var.get()
            assert notices and notices[-1][0] == "error"
        finally:
            release_start.set()


def test_agent_start_failure_keeps_configuration_editable_and_role_switchable(ui, tmp_path, monkeypatch):
    import sys

    root, notices = ui
    class BrokenAgent:
        def __init__(self, _config):
            raise OSError("port unavailable")
    monkeypatch.setattr("brd_spd.remote.WorkstationAgent", BrokenAgent)
    app = DesktopApp(root)
    app.remote.role_buttons[1].invoke()
    agent = app.remote.agent
    agent.exe_var.set(sys.executable)
    agent.workdir_var.set(str(tmp_path / "agent"))
    agent.start_button.invoke()
    pump(root, lambda: agent.can_switch_role)
    assert all(widget.instate(["!disabled"]) for widget in agent._config_controls)
    assert agent.start_button.instate(["!disabled"])
    assert notices and notices[-1][0] == "error"
    pump(root, lambda: app.remote.role_buttons[0].instate(["!disabled"]))
    app.remote.role_buttons[0].invoke()
    assert app.remote.role == "laptop"


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


@pytest.mark.parametrize("pane_type", [SkillPane, RemoteClientPane])
def test_event_pump_survives_a_broken_event_and_keeps_pumping(ui, pane_type):
    root, notices = ui
    pane = pane_type(root)
    pane._running = True
    pane._events.put(("complete", None))  # unpacking this payload raises in the handler
    pump(root, lambda: not pane._running)
    assert pane.status_var.get() == "오류"
    assert [item for item in notices if item[0] == "error"], notices
    assert "Traceback" in pane.log.get("1.0", "end")

    pane._events.put(("log", "펌프 계속"))
    pump(root, lambda: "펌프 계속" in pane.log.get("1.0", "end"))


def test_remote_pid_check_reports_a_mismatched_agent_reply_as_worker_error(ui, monkeypatch):
    root, notices = ui

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def check_pid(self, pid):
            return {"pid": pid + 1, "executable": r"C:\Cadence\allegro.exe", "creation_time": 5}

    monkeypatch.setattr("brd_spd.remote.RemoteClient", FakeClient)
    pane = RemoteClientPane(root)
    pane.host_var.set("127.0.0.1")
    pane.token_var.set("token")
    pane.fingerprint_var.set("fingerprint")
    pane.pid_var.set("101")
    pane._check_pid()
    pump(root, lambda: not pane._running)
    assert pane.status_var.get() == "오류"
    assert pane._checked_pid is None
    assert any("일치하지 않습니다" in str(args) for kind, args in notices if kind == "error"), notices


def test_remote_cancel_saves_log_zip_and_is_not_reported_as_failure(ui, tmp_path, monkeypatch):
    root, notices = ui
    state = {"status": "running"}

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def health(self):
            return {"status": "ok", "protocol": 1, "max_upload_bytes": 1024}

        def create_job(self, _options):
            return {"id": "job-cancel", "status": "created"}

        def upload_file(self, _job, slot, _path, progress=None):
            if progress:
                progress(f"Uploading {slot}: 1048576/5242880 bytes")

        def submit_job(self, _job):
            return {"status": "queued"}

        def get_job(self, _job):
            return {"id": "job-cancel", "status": state["status"]}

        def get_logs(self, _job, offset=0):
            return {"text": "", "next_offset": offset}

        def cancel_job(self, _job):
            state["status"] = "cancelled"

        def download_result(self, _job, destination, progress=None):
            if progress:
                progress("Downloading result: 1024/2048 bytes")
            destination.write_bytes(b"logs")

    monkeypatch.setattr("brd_spd.remote.RemoteClient", FakeClient)
    pane = RemoteClientPane(root)
    source, base = tmp_path / "board.spd", tmp_path / "base.brd"
    source.write_text(SOURCE, encoding="utf-8")
    base.write_bytes(b"synthetic-base")
    output = tmp_path / "result.zip"
    pane.host_var.set("127.0.0.1")
    pane.token_var.set("token")
    pane.fingerprint_var.set("fingerprint")
    load_spd(root, pane, source)
    pane.brd_var.set(str(base))
    pane.output_var.set(str(output))
    pane._run()
    pump(root, lambda: pane.status_var.get() == "원격 작업: running")
    assert pane.cancel_button.instate(["!disabled"])
    pane.cancel_button.invoke()
    # A pending cancel may need repeating, so the button stays usable.
    assert pane.cancel_button.instate(["!disabled"])
    assert pane.cancel_button.cget("text") == "취소 재요청"
    pump(root, lambda: not pane._running)

    assert output.read_bytes() == b"logs"
    assert pane.status_var.get() == "취소됨 (로그 ZIP 저장됨)"
    assert pane.open_button.instate(["!disabled"])
    assert any("작업을 취소했습니다." in str(args) for kind, args in notices if kind == "info"), notices
    assert not [item for item in notices if item[0] in ("error", "warning")], notices
    log = pane.log.get("1.0", "end")
    assert "업로드 spd: 1.0 MB / 5.0 MB" in log
    assert "상태: ok" in log and "{" not in log


def test_cancel_button_stays_disabled_for_workers_without_a_cancel_path(ui, monkeypatch):
    root, notices = ui
    release = threading.Event()

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def health(self):
            assert release.wait(5)
            return {"status": "ok", "protocol": 1}

    monkeypatch.setattr("brd_spd.remote.RemoteClient", FakeClient)
    pane = RemoteClientPane(root)
    pane.host_var.set("127.0.0.1")
    pane.token_var.set("token")
    pane.fingerprint_var.set("fingerprint")
    pane._connect()
    root.update()
    assert pane._running and pane.cancel_button.instate(["disabled"])
    release.set()
    pump(root, lambda: not pane._running)
    assert pane.status_var.get() == "연결됨"
    assert not [item for item in notices if item[0] == "error"], notices


def test_close_with_busy_pane_confirms_then_cancels_and_destroys(ui, monkeypatch):
    root, _notices = ui
    app = DesktopApp(root)
    app.remote.client._running = True
    destroyed = []
    with monkeypatch.context() as close_patch:
        close_patch.setattr(root, "destroy", lambda: destroyed.append(True))
        close_patch.setattr("brd_spd.gui.messagebox.askyesno", lambda *args, **kwargs: False)
        app._on_close()
        assert not destroyed
        assert not app.remote.client._cancel_requested

        close_patch.setattr("brd_spd.gui.messagebox.askyesno", lambda *args, **kwargs: True)
        app._on_close()
        assert destroyed
        assert app.remote.client._cancel_requested
    app.remote.client._running = False


def test_skill_generation_can_be_cancelled_and_reports_copy_progress(ui, tmp_path, monkeypatch):
    from contextlib import contextmanager
    from concurrent.futures import CancelledError

    root, notices = ui
    entered = threading.Event()

    @contextmanager
    def fake_snapshot(_source, _expected, _directory, cancelled=lambda: False, progress=None):
        progress(2 * 1024 * 1024, 8 * 1024 * 1024)
        entered.set()
        while not cancelled():
            time.sleep(0.01)
        raise CancelledError("SPD 입력 준비가 취소되었습니다.")
        yield  # pragma: no cover - the cancelled copy never yields a snapshot

    monkeypatch.setattr("brd_spd.gui.source_snapshot", fake_snapshot)
    pane = SkillPane(root)
    source = tmp_path / "board.spd"
    source.write_text(SOURCE, encoding="utf-8")
    load_spd(root, pane, source)
    pane.output_var.set(str(tmp_path / "bundle"))
    pane._generate()
    pump(root, lambda: entered.is_set())
    assert pane.cancel_button.instate(["!disabled"])
    pane.cancel_button.invoke()
    assert pane.cancel_button.cget("text") == "취소 재요청"
    pump(root, lambda: not pane._running)

    assert pane.status_var.get() == "취소됨"
    assert pane.open_button.instate(["disabled"])
    assert pane.cancel_button.instate(["disabled"])
    assert "작업용 사본 복사 중: 2.0 MB / 8.0 MB" in pane.log.get("1.0", "end")
    assert not (tmp_path / "bundle").exists()
    assert not [item for item in notices if item[0] == "error"], notices


def test_skill_error_keeps_output_folder_button_disabled(ui, tmp_path, monkeypatch):
    root, notices = ui

    def boom(*_args, **_kwargs):
        raise RuntimeError("생성 실패")

    monkeypatch.setattr("brd_spd.skill.generate_bundle", boom)
    pane = SkillPane(root)
    source = tmp_path / "board.spd"
    source.write_text(SOURCE, encoding="utf-8")
    load_spd(root, pane, source)
    pane.output_var.set(str(tmp_path / "bundle"))
    pane._generate()
    pump(root, lambda: not pane._running)
    assert pane.status_var.get() == "오류"
    assert pane.open_button.instate(["disabled"])
    assert [item for item in notices if item[0] == "error"]


def test_reload_restores_known_targets_and_drops_missing_names(ui, tmp_path):
    root, _notices = ui
    pane = SkillPane(root)
    source = tmp_path / "board.spd"
    source.write_text(SOURCE, encoding="utf-8")
    load_spd(root, pane, source)
    pane.layers_var.set("TOP,MISSING")
    pane.nets_var.set("PWR")
    pane.components_var.set(True)

    pane.spd_loading.load()  # same file: the reviewed selection must survive
    pump(root, lambda: not pane.spd_loading.loading)
    assert pane.layers_var.get() == "TOP"
    assert pane.nets_var.get() == "PWR"
    assert pane.components_var.get() is True
    assert "MISSING" in pane.log.get("1.0", "end")


def test_persisted_targets_are_restored_after_a_restart(ui, tmp_path):
    root, _notices = ui
    pane = SkillPane(root)
    source = tmp_path / "board.spd"
    source.write_text(SOURCE, encoding="utf-8")
    load_spd(root, pane, source)
    pane.layers_var.set("TOP")
    pane.nets_var.set("PWR")
    pane._finish("완료", success=True)  # persists the SPD path and the selection

    restored = SkillPane(root)
    assert restored.source_var.get() == str(source)
    assert restored.layers_var.get() == "TOP"
    restored.spd_loading.load()
    pump(root, lambda: not restored.spd_loading.loading)
    assert restored.layers_var.get() == "TOP"
    assert restored.nets_var.get() == "PWR"


def test_restored_output_paths_step_past_existing_ones(ui, tmp_path):
    from brd_spd.gui import save_settings

    root, _notices = ui
    (tmp_path / "result.zip").write_bytes(b"old")
    (tmp_path / "result-2.zip").write_bytes(b"old")
    (tmp_path / "board-skill-bundle").mkdir()
    save_settings({"remote_output": str(tmp_path / "result.zip"),
                   "skill_output": str(tmp_path / "board-skill-bundle")})
    assert RemoteClientPane(root).output_var.get() == str(tmp_path / "result-3.zip")
    assert SkillPane(root).output_var.get() == str(tmp_path / "board-skill-bundle-2")


def test_progress_and_status_are_formatted_for_korean_readers():
    from brd_spd.gui import format_bytes, format_health, format_job_status, format_transfer

    assert format_bytes(1048576) == "1.0 MB"
    assert format_transfer("업로드", "Uploading spd: 1048576/5242880 bytes") == "업로드 spd: 1.0 MB / 5.0 MB"
    assert format_transfer("다운로드", "Downloading result: 1048576/2097152 bytes") == "다운로드: 1.0 MB / 2.0 MB"
    assert format_transfer("업로드", "ok") == "업로드: ok"
    assert format_health({"status": "ok", "protocol": 1, "runner_slots": 1,
                          "max_upload_bytes": 1048576, "capabilities": ["allegro_pid"]}) == (
        "상태: ok · 프로토콜 버전: 1 · 실행 슬롯: 1 · 최대 업로드: 1.0 MB · 기능: allegro_pid")
    assert format_job_status({"id": "job-1", "status": "failed", "error": "boom", "options": {}}) == (
        "작업 ID: job-1 · 상태: failed · 오류: boom")


def test_log_keeps_the_reader_position_and_offers_a_copy_menu(ui):
    root, _notices = ui
    pane = SkillPane(root)
    pane.log.configure(height=4)
    root.update()
    for index in range(80):
        pane._write_log(f"줄 {index}")
    root.update()
    pane.log.yview_moveto(0.0)
    top_before = pane.log.yview()[0]
    pane._write_log("읽는 중에 도착한 줄")
    assert pane.log.yview()[0] == top_before  # no jump while scrolled back

    pane.log.see("end")
    root.update()
    pane._write_log("마지막 줄")
    assert pane.log.yview()[1] >= 0.999
    assert "마지막 줄" in pane.log.get("end-2l", "end")

    menu = pane.log.log_menu
    assert [menu.entrycget(index, "label") for index in range(menu.index("end") + 1)] == ["복사", "로그 저장…"]
    menu.invoke(0)
    assert "마지막 줄" in root.clipboard_get()


def test_client_token_can_be_shown_and_agent_values_can_be_copied(ui):
    root, _notices = ui
    app = DesktopApp(root)
    client = app.remote.client
    assert client.token_entry.cget("show") == "•"
    client.show_token_var.set(True)
    client._toggle_token()
    assert client.token_entry.cget("show") == ""

    app.remote.role_buttons[1].invoke()
    agent = app.remote.agent
    agent.token_var.set("copied-token")
    agent.fingerprint_var.set("copied-fingerprint")
    agent.token_copy_button.invoke()
    assert root.clipboard_get() == "copied-token"
    agent.fingerprint_copy_button.invoke()
    assert root.clipboard_get() == "copied-fingerprint"
    assert "클립보드에 복사했습니다" in agent.log.get("1.0", "end")


def test_agent_shows_running_job_count_and_confirms_before_stopping(ui, monkeypatch):
    root, notices = ui
    app = DesktopApp(root)
    app.remote.role_buttons[1].invoke()
    agent = app.remote.agent
    stopped = []

    class FakeAgent:
        running_job_ids = ["job-a", "job-b"]

        def stop(self):
            stopped.append(True)

    agent._agent = FakeAgent()
    agent._running_label = "실행 중: 127.0.0.1:8765"
    agent._show_status()
    assert agent.status_var.get() == "실행 중: 127.0.0.1:8765 · 실행 중 작업 2개"

    with monkeypatch.context() as answer:
        answer.setattr("brd_spd.gui.messagebox.askyesno", lambda *args, **kwargs: False)
        agent._sync_controls()
        agent.stop_button.invoke()
        assert not stopped and agent._agent is not None
    agent.stop_button.invoke()
    pump(root, lambda: agent._agent is None)
    assert stopped
    assert agent.status_var.get() == "중지됨"
    assert not [item for item in notices if item[0] == "error"], notices


def test_settings_save_failure_is_written_to_the_pane_log(ui, monkeypatch):
    root, _notices = ui
    pane = SkillPane(root)
    monkeypatch.setattr("brd_spd.gui.save_settings", lambda _values: False)
    pane._finish("완료", success=True)
    assert "설정을 저장하지 못했습니다" in pane.log.get("1.0", "end")


def test_client_focus_starts_on_host_and_return_loads_the_spd(ui, tmp_path):
    root, _notices = ui
    pane = RemoteClientPane(root)
    # A headless Tk never reports focus, so assert the pane's declared start point.
    assert pane.initial_focus is pane.host_entry
    source = tmp_path / "board.spd"
    source.write_text(SOURCE, encoding="utf-8")
    pane.spd_var.set(str(source))
    # Xvfb delivers no key events to unmapped widgets, so drive the binding itself.
    assert pane.spd_loading.entry.bind("<Return>")
    assert pane.spd_loading._load_event(None) == "break"
    pump(root, lambda: not pane.spd_loading.loading)
    assert pane.spd_loading.catalog is not None
