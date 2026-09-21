from html.parser import HTMLParser
from pathlib import Path
import gc
import tkinter as tk

import pytest

from brd_spd import help_ui
from brd_spd.cli import main


class HelpLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.targets, self.resources = set(), [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if tag == "a" and attrs.get("href", "").startswith("#"):
            self.targets.append(attrs["href"][1:])
        if "src" in attrs or tag == "link":
            self.resources.append(attrs.get("src", attrs.get("href", "")))


def test_offline_help_has_all_menu_and_internal_targets():
    document = HelpLinks()
    document.feed(help_ui.help_file().read_text(encoding="utf-8"))
    assert all(anchor in document.ids for _, anchor in help_ui.HELP_TOPICS)
    assert all(target in document.ids for target in document.targets)
    assert not any(url.startswith(("http:", "https:", "//")) for url in document.resources)
    assert help_ui.help_url("target-selection").endswith("#target-selection")


def test_frozen_help_prefers_installed_copy_and_falls_back_to_bundle(tmp_path, monkeypatch):
    installed, bundled = tmp_path / "installed space", tmp_path / "bundle"
    for directory in (installed, bundled):
        file = directory / "docs/help/index.html"
        file.parent.mkdir(parents=True)
        file.write_text("help", encoding="utf-8")
    monkeypatch.setattr(help_ui.sys, "frozen", True, raising=False)
    monkeypatch.setattr(help_ui.sys, "executable", str(installed / "app.exe"))
    monkeypatch.setattr(help_ui.sys, "_MEIPASS", str(bundled), raising=False)
    assert help_ui.help_file() == installed / "docs/help/index.html"
    assert "installed%20space" in help_ui.help_url()
    (installed / "docs/help/index.html").unlink()
    assert help_ui.help_file() == bundled / "docs/help/index.html"


@pytest.mark.parametrize("command", ["convert", "import-brd"])
def test_disabled_ipc_commands_do_not_create_outputs(tmp_path, capsys, command):
    destination = tmp_path / "result"
    args = [command, str(tmp_path / "source"), "-o", str(destination)]
    if command == "import-brd":
        args += ["--base-brd", str(tmp_path / "base.brd")]
    assert main(args) == 1
    assert "currently disabled" in capsys.readouterr().err
    assert not destination.exists()


@pytest.fixture
def help_root(monkeypatch):
    # Any browser launch or network request is a regression, even if rendering succeeds.
    def forbidden(*args, **kwargs):
        raise AssertionError("Help must stay inside the application and offline")
    monkeypatch.setattr("webbrowser.open", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Tk display unavailable")
    root.withdraw()
    yield root
    for callback in root.tk.call("after", "info"):
        root.after_cancel(callback)
    for child in root.winfo_children():
        child.destroy()
    if hasattr(root, "_help_viewer"):
        del root._help_viewer
    gc.collect()
    root.destroy()


def test_embedded_renderer_navigates_reuses_window_and_reopens(help_root):
    help_ui.open_help(help_root, "remote-pid")
    viewer = help_root._help_viewer
    assert isinstance(viewer, help_ui.HelpViewer)
    assert viewer.topic == "remote-pid"
    text = viewer.html.html.text("text")
    assert "Allegro" in text and "SPD 불러오기" in text and "결과와 다시 받기" in text
    for label, anchor in help_ui.HELP_TOPICS:
        help_ui.open_help(help_root, anchor)
        assert help_root._help_viewer is viewer
        assert viewer.contents.get() == label
        assert viewer.topic == anchor
    viewer.follow_link(viewer._base_url + "#target-selection")
    assert viewer.topic == "target-selection"
    viewer.destroy()
    help_ui.open_help(help_root, "native-skill")
    assert help_root._help_viewer is not viewer
    assert help_root._help_viewer.topic == "native-skill"


def test_embedded_viewer_font_size_and_external_link_blocking(help_root, monkeypatch):
    help_ui.open_help(help_root)
    viewer = help_root._help_viewer
    notices = []
    monkeypatch.setattr(help_ui.messagebox, "showinfo", lambda *a, **k: notices.append(a))
    viewer.follow_link("https://example.invalid/#remote-pid")
    viewer.follow_link("file:///not-help.html#remote-pid")
    assert len(notices) == 2
    assert viewer.topic == "overview"
    with pytest.raises(OSError, match="네트워크"):
        viewer._deny_resource("https://example.invalid/image.png")
    for _ in range(10):
        viewer.change_scale(.1)
    assert viewer.html.cget("fontscale") == 1.6
    assert viewer.larger.instate(["disabled"])
    for _ in range(10):
        viewer.change_scale(-.1)
    assert viewer.html.cget("fontscale") == .8
    assert viewer.smaller.instate(["disabled"])


def test_failed_renderer_initialization_removes_partial_window(help_root, monkeypatch):
    import tkinterweb
    def fail(*args, **kwargs):
        raise tk.TclError("test missing native HTML library")
    monkeypatch.setattr(tkinterweb, "HtmlFrame", fail)
    errors = []
    monkeypatch.setattr(help_ui.messagebox, "showerror", lambda *a, **k: errors.append(a))
    help_ui.open_help(help_root)
    assert len(errors) == 1
    assert help_root.winfo_children() == []
