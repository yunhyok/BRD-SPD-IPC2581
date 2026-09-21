import gc
import threading
import time
import tkinter as tk

import pytest

import brd_spd.selection_ui as selection_ui


CATALOG = {
    "layers": ["TOP", "L2"],
    "nets": ["GND", "NET,WITH,COMMA", "N2"],
    "nets_by_layer": {
        "TOP": ["GND"],
        "L2": ["NET,WITH,COMMA", "N2"],
    },
}


@pytest.fixture
def tk_root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    yield root
    try:
        root.update()
        for callback in root.tk.call("after", "info"):
            root.after_cancel(callback)
        for child in root.winfo_children():
            child.destroy()
        # Collect Tk variables and dialog callback cycles while their Tcl
        # interpreter is still available on the main thread.
        gc.collect()
        root.destroy()
    except tk.TclError:
        pass
    gc.collect()


@pytest.fixture(autouse=True)
def clear_catalog_cache():
    with selection_ui._cache_lock:
        selection_ui._catalog_cache.clear()


def _pump(root, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Tk condition timed out")


def _items(picker):
    return list(picker.listbox.get(0, tk.END))


def test_async_all_clear_empty_guard_and_comma_value(
        tk_root, tmp_path, monkeypatch):
    source = tmp_path / "sample.spd"
    source.write_bytes(b"spd")
    entered = threading.Event()
    release = threading.Event()

    def scan(path, progress, cancelled):
        entered.set()
        progress("reading")
        assert release.wait(3)
        return CATALOG

    monkeypatch.setattr(selection_ui, "_scan_catalog", scan)
    errors = []
    monkeypatch.setattr(selection_ui.messagebox, "showerror", lambda *a, **k: errors.append(a))
    applied = []
    started = time.monotonic()
    picker = selection_ui.open_target_picker(
        tk_root, source, "nets", None, None, applied.append
    )
    assert time.monotonic() - started < 0.2
    assert entered.wait(1)
    assert picker.winfo_exists()
    assert not picker._loaded

    release.set()
    _pump(tk_root, lambda: picker._loaded)
    assert _items(picker) == CATALOG["nets"]
    assert picker.listbox.curselection() == (0, 1, 2)

    picker.clear_all()
    assert picker.listbox.curselection() == ()
    assert str(picker.apply_button["state"]) == "disabled"
    picker.apply()
    assert errors and "하나 이상의 항목" in errors[-1][1]
    assert applied == []

    picker.listbox.selection_set(1)
    picker._selection_changed()
    picker.apply()
    assert applied == [["NET,WITH,COMMA"]]


def test_close_cancels_worker_without_apply(tk_root, tmp_path, monkeypatch):
    source = tmp_path / "slow.spd"
    source.write_bytes(b"spd")
    entered = threading.Event()
    observed_cancel = threading.Event()

    def scan(path, progress, cancelled):
        entered.set()
        deadline = time.monotonic() + 3
        while not cancelled() and time.monotonic() < deadline:
            time.sleep(0.01)
        if cancelled():
            observed_cancel.set()
        return CATALOG

    monkeypatch.setattr(selection_ui, "_scan_catalog", scan)
    applied = []
    picker = selection_ui.open_target_picker(
        tk_root, source, "layers", None, None, applied.append
    )
    assert entered.wait(1)
    picker.close()
    assert observed_cancel.wait(1)
    tk_root.update()
    assert applied == []
    assert picker._closed


def test_net_filter_and_unknown_layer_fail_explicitly(
        tk_root, tmp_path, monkeypatch):
    source = tmp_path / "filter.spd"
    source.write_bytes(b"spd")
    scans = []
    monkeypatch.setattr(
        selection_ui, "_scan_catalog",
        lambda path, progress, cancelled: scans.append(path) or CATALOG,
    )
    errors = []
    monkeypatch.setattr(selection_ui.messagebox, "showerror", lambda *a, **k: errors.append(a))

    applied = []
    picker = selection_ui.open_target_picker(
        tk_root, source, "nets", None, ["L2"], applied.append
    )
    _pump(tk_root, lambda: picker._loaded)
    assert _items(picker) == ["NET,WITH,COMMA", "N2"]
    picker.apply()
    assert applied == [["NET,WITH,COMMA", "N2"]]

    unknown = selection_ui.open_target_picker(
        tk_root, source, "nets", None, ["NO_SUCH_LAYER"], applied.append
    )
    _pump(tk_root, lambda: bool(errors))
    assert not unknown._loaded
    assert "SPD에 없는 레이어" in errors[-1][1]
    assert len(scans) == 1, "the second picker should reuse the catalog cache"
    unknown.close()


def test_cache_reuse_invalidation_and_stale_apply_guard(
        tk_root, tmp_path, monkeypatch):
    source = tmp_path / "cached.spd"
    source.write_bytes(b"one")
    scan_count = 0

    def scan(path, progress, cancelled):
        nonlocal scan_count
        scan_count += 1
        return CATALOG

    monkeypatch.setattr(selection_ui, "_scan_catalog", scan)
    errors = []
    monkeypatch.setattr(selection_ui.messagebox, "showerror", lambda *a, **k: errors.append(a))

    first_signature = selection_ui.catalog_signature(source)
    loaded = selection_ui.load_catalog(source)
    assert loaded == CATALOG
    loaded["layers"].append("CALLER_MUTATION")
    first = selection_ui.open_target_picker(
        tk_root, source, "layers", ["TOP"], None, lambda values: None
    )
    _pump(tk_root, lambda: first._loaded)
    assert "CALLER_MUTATION" not in _items(first)
    assert first.listbox.curselection() == (0,)
    first.close()

    second = selection_ui.open_target_picker(
        tk_root, source, "nets", None, None, lambda values: None
    )
    _pump(tk_root, lambda: second._loaded)
    assert scan_count == 1
    second.close()

    source.write_bytes(b"changed-size")
    assert selection_ui.catalog_signature(source) != first_signature
    applied = []
    changed = selection_ui.open_target_picker(
        tk_root, source, "layers", None, None, applied.append
    )
    _pump(tk_root, lambda: changed._loaded)
    assert scan_count == 2

    source.write_bytes(b"changed-again-with-another-size")
    changed.apply()
    assert applied == []
    assert errors and "스캔 후 변경" in errors[-1][1]
    assert str(changed.apply_button["state"]) == "disabled"
    changed.close()
