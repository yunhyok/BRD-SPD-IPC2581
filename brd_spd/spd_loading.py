"""SPD readiness gate shared by the native and remote Tk panes."""
from __future__ import annotations

from concurrent.futures import CancelledError
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .selection_ui import catalog_signature, load_catalog


class SpdLoading:
    def __init__(self, pane, variable, row, browse):
        self.pane, self.variable = pane, variable
        self.catalog = self.signature = None
        self.loading = False
        self.generation = 0
        self.cancel = threading.Event()
        self.events = queue.Queue()
        self.timer = None
        self.controls = []
        self.restore = ("", "", False)
        ttk.Label(pane, text="SPD 입력").grid(row=row, column=0, sticky="w")
        field = ttk.Frame(pane)
        field.grid(row=row, column=1, sticky="ew", padx=10, pady=3)
        field.columnconfigure(0, weight=1)
        self.entry = ttk.Entry(field, textvariable=variable)
        self.entry.grid(sticky="ew")
        self.entry.bind("<Return>", self._load_event, add="+")
        self.status = tk.StringVar(value="SPD를 불러오면 대상과 작업 옵션이 활성화됩니다.")
        ttk.Label(field, textvariable=self.status, wraplength=430).grid(sticky="w")
        buttons = ttk.Frame(pane)
        buttons.grid(row=row, column=2, sticky="e")
        self.browse = ttk.Button(buttons, text="찾아보기…", command=browse)
        self.browse.grid(row=0, column=0, padx=(0, 4))
        self.button = ttk.Button(buttons, text="SPD 불러오기", command=self.load)
        self.button.grid(row=0, column=1)
        self.trace = variable.trace_add("write", self.invalidate)
        pane.bind("<Destroy>", self._destroy, add="+")

    def bind_controls(self, controls):
        self.controls = controls
        self.sync()

    def sync(self):
        busy = self.pane._running or getattr(self.pane, "_picker_active", False)
        for widget in self.controls:
            widget.configure(state="normal" if self.catalog is not None and not busy else "disabled")
        for widget in (self.entry, self.browse):
            widget.configure(state="disabled" if busy else "normal")
        self.button.configure(state="disabled" if busy or self.loading else "normal")

    def invalidate(self, *_):
        self.cancel.set()
        self.generation += 1
        self.catalog = self.signature = None
        self.loading = False
        self.pane.layers_var.set("")
        self.pane.nets_var.set("")
        self.pane.components_var.set(False)
        self.status.set("SPD 불러오기가 필요합니다. 이전 대상 선택은 초기화되었습니다.")
        self.sync()

    def _load_event(self, _event):
        self.load()
        return "break"

    def load(self):
        if self.pane._running or self.loading:
            return
        # Keep the current selection so a reload of the same board can restore it.
        self.restore = (self.pane.layers_var.get(), self.pane.nets_var.get(), self.pane.components_var.get())
        self.invalidate()
        try:
            if not self.variable.get().strip():
                raise ValueError("SPD 입력 파일을 선택하세요.")
            source = Path(self.variable.get().strip()).resolve()
            signature = catalog_signature(source)
        except (OSError, ValueError) as exc:
            self._failed(str(exc))
            return
        self.loading = True
        self.cancel = threading.Event()
        self.status.set("SPD 목록 읽는 중…")
        self.sync()
        events, cancel, generation = self.events, self.cancel, self.generation

        def worker():
            try:
                catalog = load_catalog(source, lambda text: events.put((generation, "progress", text)), cancel.is_set)
                if not catalog["layers"] or not catalog["nets"]:
                    raise ValueError("반영 가능한 plane 레이어/NET을 찾지 못했습니다. SPD 파일 내용을 확인하세요.")
                events.put((generation, "loaded", (signature, catalog)))
            except CancelledError:
                pass
            except Exception as exc:
                events.put((generation, "error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()
        if self.timer is None:
            self.timer = self.pane.after(75, self._drain)

    def _drain(self):
        self.timer = None
        try:
            while True:
                generation, kind, value = self.events.get_nowait()
                if generation != self.generation:
                    continue
                if kind == "progress":
                    self.status.set(str(value).replace("SPD catalog", "SPD 목록 읽는 중"))
                elif kind == "error":
                    self._failed(value)
                else:
                    signature, catalog = value
                    try:
                        if catalog_signature(Path(self.variable.get().strip())) != signature:
                            raise ValueError("SPD 파일이 변경되었습니다. 다시 불러오세요.")
                    except (OSError, ValueError) as exc:
                        self._failed(str(exc))
                        continue
                    self.signature, self.catalog = signature, catalog
                    self.loading = False
                    text = f"SPD 로딩 완료 · 레이어 {len(catalog['layers'])}개 · NET {len(catalog['nets'])}개"
                    self.status.set(text)
                    self.pane._write_log(text)
                    self._restore_targets(catalog)
                    self.sync()
        except queue.Empty:
            pass
        if self.loading:
            self.timer = self.pane.after(75, self._drain)

    def _restore_targets(self, catalog):
        """Bring back the selection cleared by ``invalidate`` for names this SPD still has."""
        from .gui import encode_target_values, parse_target_values

        layers_text, nets_text, components = self.restore
        self.restore = ("", "", False)
        for variable, text, known in ((self.pane.layers_var, layers_text, catalog["layers"]),
                                      (self.pane.nets_var, nets_text, catalog["nets"])):
            try:
                names = parse_target_values(text)
            except ValueError:
                names = None
            if not names:
                continue
            available = set(known)
            kept = [name for name in names if name in available]
            dropped = [name for name in names if name not in available]
            if kept:
                variable.set(encode_target_values(kept))
            if dropped:
                self.pane._write_log("이번 SPD에 없는 대상은 제외했습니다: " + ", ".join(dropped))
        self.pane.components_var.set(bool(components))

    def _failed(self, text):
        self.loading = False
        self.catalog = self.signature = None
        self.status.set("SPD 로딩 실패 · 파일을 확인하고 다시 불러오세요.")
        self.pane._write_log("SPD 로딩 오류: " + text)
        self.sync()
        messagebox.showerror("SPD 불러오기", text, parent=self.pane)

    def require_ready(self):
        if self.catalog is not None:
            try:
                if catalog_signature(Path(self.variable.get().strip())) == self.signature:
                    return True
            except (OSError, ValueError):
                pass
            self.invalidate()
        messagebox.showerror("SPD 불러오기", "먼저 SPD 불러오기를 완료하세요. 파일이 변경되었다면 다시 불러와야 합니다.", parent=self.pane)
        return False

    def _destroy(self, event):
        if event.widget != self.pane:
            return
        self.cancel.set()
        self.variable.trace_remove("write", self.trace)
        if self.timer is not None:
            self.pane.after_cancel(self.timer)
            self.timer = None
