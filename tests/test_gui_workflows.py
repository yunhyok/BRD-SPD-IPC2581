"""Exercise Tk workers/events against local generation and a simulated runner."""
import time
import tkinter as tk

import pytest

from brd_spd.gui import SkillPane, RemoteClientPane
from brd_spd.remote import AgentConfig
from test_integration import ProcessSimulator, SOURCE


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
    root.destroy()


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
