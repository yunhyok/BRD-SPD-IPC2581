from html.parser import HTMLParser
from pathlib import Path

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
