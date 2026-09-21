"""Manual workstation-agent console for an Allegro-licensed Windows machine."""

from __future__ import annotations

import queue
import threading
import traceback
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .gui import APP_NAME, load_settings, save_settings


DEFAULT_ALLEGRO = r"C:\Cadence\SPB_24.1\tools\bin\allegro.exe"


class AgentConsoleApp(ttk.Frame):
    """Start and stop the HTTPS agent explicitly; it never changes firewall settings."""

    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=16)
        persisted = load_settings()
        self.host_var = tk.StringVar(value=persisted.get("agent_host", "0.0.0.0"))
        self.port_var = tk.StringVar(value=persisted.get("agent_port", "8765"))
        self.allowed_var = tk.StringVar(value=persisted.get("agent_allowed_clients", ""))
        self.exe_var = tk.StringVar(value=persisted.get("agent_allegro_exe", DEFAULT_ALLEGRO))
        self.workdir_var = tk.StringVar(value=persisted.get("agent_workdir", str(Path.home() / "BRD-SPD-Agent")))
        self.status_var = tk.StringVar(value="중지됨")
        self.token_var = tk.StringVar(value="")
        self.fingerprint_var = tk.StringVar(value="")
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._agent: Any = None
        self._busy = False
        self._closing = False
        self._build()
        self.master.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_events)

    def _build(self) -> None:
        self.master.title(f"{APP_NAME} Workstation Agent")
        self.master.minsize(690, 470)
        self.master.columnconfigure(0, weight=1)
        self.master.rowconfigure(0, weight=1)
        self.grid(sticky="nsew")
        self.columnconfigure(1, weight=1)
        self.rowconfigure(9, weight=1)
        ttk.Label(self, text="Allegro 워크스테이션 Agent", font=("Malgun Gothic", 15, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(self, text="라이선스가 있는 워크스테이션에서만 수동으로 시작합니다. 방화벽·자동 시작은 변경하지 않습니다.", foreground="#8a4b00", wraplength=640).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))
        self._row(2, "바인드 주소", self.host_var)
        self._row(3, "포트", self.port_var)
        self._row(4, "허용 클라이언트 IP (선택)", self.allowed_var)
        self._file_row(5, "Allegro 실행 파일", self.exe_var, self._choose_exe, [("Allegro 실행 파일", "allegro.exe"), ("실행 파일", "*.exe")])
        self._folder_row(6, "작업 폴더", self.workdir_var, self._choose_workdir)
        controls = ttk.Frame(self)
        controls.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(10, 8))
        self.start_button = ttk.Button(controls, text="Agent 시작", command=self._start)
        self.start_button.grid(row=0, column=0, padx=(0, 6))
        self.stop_button = ttk.Button(controls, text="Agent 중지", command=self._stop, state="disabled")
        self.stop_button.grid(row=0, column=1, padx=(0, 12))
        ttk.Label(controls, textvariable=self.status_var).grid(row=0, column=2, sticky="w")
        ttk.Label(self, text="토큰 (클라이언트에 복사, 저장되지 않음)").grid(row=8, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.token_var, state="readonly").grid(row=8, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=3)
        ttk.Label(self, text="인증서 지문").grid(row=9, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.fingerprint_var, state="readonly").grid(row=9, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=3)
        frame = ttk.Frame(self)
        frame.grid(row=10, column=0, columnspan=3, sticky="nsew", pady=(10, 0))
        self.rowconfigure(10, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.log = tk.Text(frame, height=11, wrap="word", state="disabled", font=("Consolas", 10))
        scroll = ttk.Scrollbar(frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

    def _row(self, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=variable).grid(row=row, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=3)

    def _file_row(self, row: int, label: str, variable: tk.StringVar, command: Any, types: list[tuple[str, str]]) -> None:
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=10, pady=3)
        ttk.Button(self, text="찾아보기…", command=command).grid(row=row, column=2)

    def _folder_row(self, row: int, label: str, variable: tk.StringVar, command: Any) -> None:
        self._file_row(row, label, variable, command, [])

    def _choose_exe(self) -> None:
        path = filedialog.askopenfilename(title="Allegro 실행 파일 선택", filetypes=[("Allegro 실행 파일", "allegro.exe"), ("실행 파일", "*.exe")])
        if path:
            self.exe_var.set(path)

    def _choose_workdir(self) -> None:
        path = filedialog.askdirectory(title="Agent 작업 폴더 선택")
        if path:
            self.workdir_var.set(path)

    def _start(self) -> None:
        if self._busy or self._agent is not None:
            return
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showerror(APP_NAME, "포트는 숫자여야 합니다.")
            return
        exe = Path(self.exe_var.get().strip())
        if not exe.is_file():
            messagebox.showerror(APP_NAME, f"Allegro 실행 파일을 찾을 수 없습니다.\n{exe}")
            return
        allowed = [item.strip() for item in self.allowed_var.get().split(",") if item.strip()]
        datadir = Path(self.workdir_var.get().strip())
        host = self.host_var.get().strip()
        self._busy = True
        self.start_button.configure(state="disabled")
        self.status_var.set("시작 중…")
        def worker() -> None:
            try:
                from brd_spd.remote import AgentConfig, WorkstationAgent

                config = AgentConfig(datadir=datadir, host=host, port=port, allegro_exe=exe, allowed_clients=allowed or None)
                agent = WorkstationAgent(config)
                info = agent.start()
                self._events.put(("started", (agent, info)))
            except Exception as exc:
                self._events.put(("error", (str(exc), traceback.format_exc())))
        threading.Thread(target=worker, daemon=True).start()

    def _stop(self, closing: bool = False) -> None:
        self._closing = self._closing or closing
        if self._agent is None or self._busy:
            return
        self._busy = True
        self.stop_button.configure(state="disabled")
        self.status_var.set("중지 중…")
        def worker() -> None:
            try:
                self._agent.stop()
                self._events.put(("stopped", None))
            except Exception as exc:
                self._events.put(("error", (str(exc), traceback.format_exc())))
        threading.Thread(target=worker, daemon=True).start()

    def _on_close(self) -> None:
        """Stop the agent first so closing the GUI cannot orphan an Allegro process."""
        self._closing = True
        self.status_var.set("종료 중: Agent를 안전하게 중지합니다…")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        if self._agent is not None:
            self._stop(closing=True)
        elif not self._busy:
            self.master.destroy()

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "started":
                    self._agent, info = payload
                    self._busy = False
                    self.token_var.set(str(info.get("token", "")))
                    self.fingerprint_var.set(str(info.get("fingerprint", "")))
                    self._write_log("Agent 시작: " + str(info))
                    self.status_var.set(f"실행 중: {info.get('host')}:{info.get('port')}")
                    self.stop_button.configure(state="normal")
                    save_settings({"agent_host": self.host_var.get(), "agent_port": self.port_var.get(), "agent_allowed_clients": self.allowed_var.get(), "agent_allegro_exe": self.exe_var.get(), "agent_workdir": self.workdir_var.get()})
                    if self._closing:
                        self._stop(closing=True)
                elif kind == "stopped":
                    self._agent = None
                    self._busy = False
                    self.token_var.set("")
                    self.fingerprint_var.set("")
                    self._write_log("Agent 중지됨")
                    self.status_var.set("중지됨")
                    self.start_button.configure(state="normal")
                    if self._closing:
                        self.master.destroy()
                elif kind == "error":
                    message, details = payload
                    self._busy = False
                    self._write_log("오류: " + message)
                    self._write_log(details)
                    self.status_var.set("오류")
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    if self._closing:
                        self.master.destroy()
                    else:
                        messagebox.showerror(APP_NAME, f"Agent 작업에 실패했습니다.\n\n{message}")
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _write_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")


def main() -> None:
    root = tk.Tk()
    try:
        root.option_add("*Font", "Malgun Gothic 10")
    except tk.TclError:
        pass
    AgentConsoleApp(root)
    root.mainloop()
