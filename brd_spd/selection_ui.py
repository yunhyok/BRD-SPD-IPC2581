"""Non-blocking layer and net selection dialog for large SPD inputs."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from collections import OrderedDict
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, Literal


_CACHE_LIMIT = 2
CatalogSignature = tuple[str, int, int, int, int]
_catalog_cache: OrderedDict[CatalogSignature, dict] = OrderedDict()
_cache_lock = threading.Lock()


def catalog_signature(source: Path) -> CatalogSignature:
    """Identify the file and revision, including replacement at the same path."""
    resolved = source.expanduser().resolve()
    stat = resolved.stat()
    return str(resolved), stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino


_fingerprint = catalog_signature


def _copy_catalog(catalog: dict) -> dict:
    return {
        "layers": list(catalog.get("layers", [])),
        "nets": list(catalog.get("nets", [])),
        "nets_by_layer": {
            str(layer): list(nets)
            for layer, nets in catalog.get("nets_by_layer", {}).items()
        },
    }


def _cached(key: CatalogSignature) -> dict | None:
    with _cache_lock:
        value = _catalog_cache.get(key)
        if value is None:
            return None
        _catalog_cache.move_to_end(key)
        return _copy_catalog(value)


def _store_cache(key: CatalogSignature, catalog: dict) -> None:
    with _cache_lock:
        # A changed file at the same resolved path invalidates its old entry.
        for old_key in list(_catalog_cache):
            if old_key[0] == key[0] and old_key != key:
                del _catalog_cache[old_key]
        _catalog_cache[key] = _copy_catalog(catalog)
        _catalog_cache.move_to_end(key)
        while len(_catalog_cache) > _CACHE_LIMIT:
            _catalog_cache.popitem(last=False)


def _scan_catalog(source: Path, progress, cancelled):
    from .selection_catalog import scan_catalog
    return scan_catalog(source, progress=progress, cancelled=cancelled)


def load_catalog(source: Path, progress=None, cancelled=None) -> dict:
    """Load an SPD catalog, reusing at most two successful file signatures."""
    source = Path(source)
    key = catalog_signature(source)
    cached = _cached(key)
    if cached is not None:
        return cached
    is_cancelled = cancelled or (lambda: False)
    if is_cancelled():
        raise RuntimeError("SPD 카탈로그 읽기가 취소되었습니다.")
    catalog = _scan_catalog(
        Path(key[0]), progress=progress, cancelled=is_cancelled
    )
    if is_cancelled():
        raise RuntimeError("SPD 카탈로그 읽기가 취소되었습니다.")
    if catalog_signature(Path(key[0])) != key:
        raise RuntimeError("스캔하는 동안 SPD 파일이 변경되었습니다.")
    copied = _copy_catalog(catalog)
    _store_cache(key, copied)
    return _copy_catalog(copied)


def _catalog_worker(source: Path, key: CatalogSignature,
                    cancel_event: threading.Event,
                    events: queue.Queue[tuple[str, object]]) -> None:
    """Scan without retaining the dialog or any Tk object."""
    def progress(message) -> None:
        if not cancel_event.is_set():
            events.put(("progress", str(message)))

    try:
        catalog = load_catalog(source, progress=progress, cancelled=cancel_event.is_set)
        if cancel_event.is_set():
            return
        if catalog_signature(source) != key:
            raise RuntimeError("스캔하는 동안 SPD 파일이 변경되었습니다.")
        events.put(("catalog", _copy_catalog(catalog)))
    except Exception as exc:
        if not cancel_event.is_set():
            events.put(("error", exc))


def _unique_strings(values) -> list[str]:
    result = []
    seen = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError("카탈로그 항목은 문자열이어야 합니다.")
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


class TargetPicker(tk.Toplevel):
    """Asynchronous picker returned by :func:`open_target_picker`."""

    def __init__(self, parent, source: Path, kind: Literal["layers", "nets"],
                 selected: list[str] | None, layers: list[str] | None,
                 on_apply: Callable[[list[str]], None]):
        super().__init__(parent)
        if kind not in {"layers", "nets"}:
            self.destroy()
            raise ValueError("kind must be 'layers' or 'nets'")
        self.source = Path(source)
        self.kind = kind
        self.initial_selected = None if selected is None else list(selected)
        self.layers = None if layers is None else list(layers)
        self.on_apply = on_apply
        self.cancel_event = threading.Event()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._after_id = None
        self._closed = False
        self._loaded = False
        self._source_key = None

        self.title("레이어 선택" if kind == "layers" else "네트 선택")
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.minsize(430, 420)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        heading = "반영할 레이어를 선택하세요." if kind == "layers" else "반영할 NET을 선택하세요."
        ttk.Label(self, text=heading, font=("Malgun Gothic", 11, "bold")).grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 3)
        )
        ttk.Label(self, text="Ctrl 또는 Shift 키로 여러 항목을 선택할 수 있습니다.").grid(
            row=1, column=0, sticky="w", padx=12, pady=(0, 8)
        )

        list_frame = ttk.Frame(self)
        list_frame.grid(row=2, column=0, sticky="nsew", padx=12)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(
            list_frame, selectmode=tk.EXTENDED, exportselection=False,
            font=("Malgun Gothic", 10), activestyle="dotbox",
        )
        vertical = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
        horizontal = ttk.Scrollbar(list_frame, orient="horizontal", command=self.listbox.xview)
        self.listbox.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.listbox.bind("<<ListboxSelect>>", self._selection_changed)

        controls = ttk.Frame(self)
        controls.grid(row=3, column=0, sticky="ew", padx=12, pady=(8, 0))
        self.select_all_button = ttk.Button(controls, text="전체 선택", command=self.select_all)
        self.clear_button = ttk.Button(controls, text="선택 해제", command=self.clear_all)
        self.select_all_button.pack(side="left")
        self.clear_button.pack(side="left", padx=(6, 0))

        self.status = tk.StringVar(value="SPD 카탈로그를 읽는 중…")
        ttk.Label(self, textvariable=self.status).grid(
            row=4, column=0, sticky="w", padx=12, pady=(8, 3)
        )
        actions = ttk.Frame(self)
        actions.grid(row=5, column=0, sticky="e", padx=12, pady=(3, 12))
        ttk.Button(actions, text="닫기", command=self.close).pack(side="right")
        self.apply_button = ttk.Button(actions, text="적용", command=self.apply, state="disabled")
        self.apply_button.pack(side="right", padx=(0, 6))
        self.select_all_button.configure(state="disabled")
        self.clear_button.configure(state="disabled")

        try:
            self._source_key = catalog_signature(self.source)
        except OSError as exc:
            message = f"SPD 파일을 읽을 수 없습니다.\n{exc}"
            self._after_id = self.after_idle(
                lambda: self._fail(message)
            )
            return

        cached = _cached(self._source_key)
        if cached is not None:
            self.events.put(("catalog", cached))
        else:
            thread = threading.Thread(
                target=_catalog_worker,
                args=(Path(self._source_key[0]), self._source_key,
                      self.cancel_event, self.events),
                name="spd-selection-catalog", daemon=True,
            )
            thread.start()
        self._schedule_poll(0)

    def _schedule_poll(self, delay=50) -> None:
        if not self._closed:
            self._after_id = self.after(delay, self._poll)

    def _poll(self) -> None:
        self._after_id = None
        if self._closed:
            return
        completed = False
        while True:
            try:
                event, value = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "progress":
                self.status.set(str(value))
            elif event == "catalog":
                self._load_catalog(value)
                completed = True
            elif event == "error":
                self._fail(f"SPD 카탈로그를 읽지 못했습니다.\n{value}")
                completed = True
        if not completed and not self._closed:
            self._schedule_poll()

    def _load_catalog(self, raw_catalog: dict) -> None:
        try:
            if not isinstance(raw_catalog, dict):
                raise ValueError("잘못된 SPD 카탈로그입니다.")
            catalog = _copy_catalog(raw_catalog)
            available_layers = _unique_strings(catalog["layers"])
            if self.kind == "layers":
                items = available_layers
            else:
                all_nets = _unique_strings(catalog["nets"])
                if self.layers is None:
                    items = all_nets
                else:
                    unknown = [layer for layer in self.layers if layer not in set(available_layers)]
                    if unknown:
                        raise ValueError("SPD에 없는 레이어: " + ", ".join(unknown))
                    allowed = set()
                    by_layer = catalog["nets_by_layer"]
                    for layer in self.layers:
                        allowed.update(_unique_strings(by_layer.get(layer, [])))
                    items = [net for net in all_nets if net in allowed]
        except (TypeError, ValueError) as exc:
            self._fail(str(exc))
            return

        self.listbox.delete(0, tk.END)
        for item in items:
            self.listbox.insert(tk.END, item)
        wanted = set(items if self.initial_selected is None else self.initial_selected)
        for index, item in enumerate(items):
            if item in wanted:
                self.listbox.selection_set(index)
        self._loaded = True
        self.select_all_button.configure(state="normal")
        self.clear_button.configure(state="normal")
        self._selection_changed()

    def _selection_changed(self, _event=None) -> None:
        selected_count = len(self.listbox.curselection())
        total = self.listbox.size()
        self.status.set(f"{selected_count}개 선택 / 전체 {total}개")
        self.apply_button.configure(state="normal" if self._loaded and selected_count else "disabled")

    def select_all(self) -> None:
        if self._loaded:
            self.listbox.selection_set(0, tk.END)
            self._selection_changed()

    def clear_all(self) -> None:
        self.listbox.selection_clear(0, tk.END)
        self._selection_changed()

    def apply(self) -> None:
        if self._closed or not self._loaded:
            return
        indices = self.listbox.curselection()
        if not indices:
            messagebox.showerror("선택 필요", "하나 이상의 항목을 선택하세요.", parent=self)
            return
        try:
            if catalog_signature(self.source) != self._source_key:
                self.apply_button.configure(state="disabled")
                raise RuntimeError("SPD 파일이 스캔 후 변경되었습니다. 선택 창을 다시 여세요.")
            values = [self.listbox.get(index) for index in indices]
            self.on_apply(values)
        except Exception as exc:
            messagebox.showerror("선택 적용 실패", str(exc), parent=self)
            return
        self.close()

    def _fail(self, message: str) -> None:
        if self._closed:
            return
        self._loaded = False
        self.status.set("카탈로그를 사용할 수 없습니다.")
        self.apply_button.configure(state="disabled")
        self.select_all_button.configure(state="disabled")
        self.clear_button.configure(state="disabled")
        messagebox.showerror("SPD 선택", message, parent=self)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.cancel_event.set()
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        self.destroy()


def open_target_picker(parent, source: Path, kind: Literal["layers", "nets"],
                       selected: list[str] | None, layers: list[str] | None,
                       on_apply: Callable[[list[str]], None]) -> tk.Toplevel:
    """Open and immediately return a non-blocking SPD target picker."""
    return TargetPicker(parent, source, kind, selected, layers, on_apply)
