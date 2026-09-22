"""Manual workstation-agent console for an Allegro-licensed Windows machine."""

from __future__ import annotations

import queue
import threading
import traceback
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .gui import APP_NAME, append_log, attach_log_menu, load_settings, save_settings, settings_file
from .help_ui import install_help_menu


DEFAULT_ALLEGRO = r"C:\Cadence\SPB_24.1\tools\bin\allegro.exe"


class AgentConsoleApp(ttk.Frame):
    """Start and stop the HTTPS agent explicitly; it never changes firewall settings."""

    def __init__(self, master: tk.Misc, *, standalone: bool = True) -> None:
        super().__init__(master, padding=16)
        self.standalone = standalone
        persisted = load_settings()
        self.host_var = tk.StringVar(value=persisted.get("agent_host", "0.0.0.0"))
        self.port_var = tk.StringVar(value=persisted.get("agent_port", "8765"))
        self.allowed_var = tk.StringVar(value=persisted.get("agent_allowed_clients", ""))
        self.exe_var = tk.StringVar(value=persisted.get("agent_allegro_exe", DEFAULT_ALLEGRO))
        self.workdir_var = tk.StringVar(value=persisted.get("agent_workdir", str(Path.home() / "BRD-SPD-Agent")))
        self.status_var = tk.StringVar(value="중지됨")
        self.token_var = tk.StringVar(value="")
        self.fingerprint_var = tk.StringVar(value="")
        self._running_label = ""
        self._status_ticks = 0
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._agent: Any = None
        self._busy = False
        self._closing = False
        self._destroyed = False
        self._after_id: str | None = None
        self._config_controls: list[tk.Widget] = []
        self._build()
        if standalone:
            self._configure_standalone(master)
        self.bind("<Destroy>", self._destroyed_event, add="+")
        self._sync_controls()
        self._after_id = self.after(100, self._drain_events)

    @property
    def can_switch_role(self) -> bool:
        """Whether an embedding container may safely discard this idle pane."""
        return not self._busy and self._agent is None and not self._closing

    @property
    def running_jobs(self) -> int:
        """Count the agent's unfinished jobs; an older agent may not report them."""
        try:
            return len(list(getattr(self._agent, "running_job_ids", None) or []))
        except Exception:  # a status read must never break the console
            return 0

    def confirm_interrupt(self, question: str) -> bool:
        """Ask before an action which would abort jobs the workstation is running."""
        count = self.running_jobs
        if not count:
            return True
        return bool(messagebox.askyesno(
            APP_NAME, f"실행 중인 작업 {count}개가 중단됩니다. {question}", parent=self))

    def _configure_standalone(self, master: tk.Misc) -> None:
        master.title(f"{APP_NAME} Workstation Agent")
        master.minsize(690, 470)
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.grid(sticky="nsew")
        self._menu, self._help_menu = install_help_menu(master, lambda: "workstation")
        master.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self) -> None:
        self.columnconfigure(1, weight=1)
        if self.standalone:
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
        self.stop_button = ttk.Button(controls, text="Agent 중지", command=self._stop_clicked, state="disabled")
        self.stop_button.grid(row=0, column=1, padx=(0, 12))
        ttk.Label(controls, textvariable=self.status_var).grid(row=0, column=2, sticky="w")
        ttk.Label(self, text="토큰 (클라이언트에 복사, 저장되지 않음)").grid(row=8, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.token_var, state="readonly").grid(row=8, column=1, sticky="ew", padx=(10, 0), pady=3)
        self.token_copy_button = ttk.Button(self, text="복사", command=lambda: self._copy(self.token_var, "토큰"))
        self.token_copy_button.grid(row=8, column=2, sticky="e", padx=(6, 0))
        ttk.Label(self, text="인증서 지문").grid(row=9, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.fingerprint_var, state="readonly").grid(row=9, column=1, sticky="ew", padx=(10, 0), pady=3)
        self.fingerprint_copy_button = ttk.Button(self, text="복사", command=lambda: self._copy(self.fingerprint_var, "인증서 지문"))
        self.fingerprint_copy_button.grid(row=9, column=2, sticky="e", padx=(6, 0))
        frame = ttk.Frame(self)
        frame.grid(row=10, column=0, columnspan=3, sticky="nsew", pady=(10, 0))
        self.rowconfigure(10, weight=1, minsize=120)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.log = tk.Text(frame, height=11, wrap="word", state="disabled", font=("Consolas", 10))
        scroll = ttk.Scrollbar(frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        attach_log_menu(self.log)

    def _copy(self, variable: tk.StringVar, label: str) -> None:
        value = variable.get().strip()
        if not value:
            messagebox.showinfo(APP_NAME, f"복사할 {label}이 없습니다. 먼저 Agent를 시작하세요.", parent=self)
            return
        self.clipboard_clear()
        self.clipboard_append(value)
        self._write_log(f"{label}을 클립보드에 복사했습니다.")

    def _row(self, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=3)
        entry = ttk.Entry(self, textvariable=variable)
        entry.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=3)
        self._config_controls.append(entry)

    def _file_row(self, row: int, label: str, variable: tk.StringVar, command: Any, types: list[tuple[str, str]]) -> None:
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=3)
        entry = ttk.Entry(self, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", padx=10, pady=3)
        browse = ttk.Button(self, text="찾아보기…", command=command)
        browse.grid(row=row, column=2)
        self._config_controls.extend((entry, browse))

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

    def _sync_controls(self) -> None:
        editable = not self._busy and self._agent is None and not self._closing
        for widget in self._config_controls:
            widget.configure(state="normal" if editable else "disabled")
        self.start_button.configure(state="normal" if editable else "disabled")
        can_stop = self._agent is not None and not self._busy
        self.stop_button.configure(state="normal" if can_stop else "disabled")

    def _start(self) -> None:
        if self._closing or self._busy or self._agent is not None:
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
        self.status_var.set("시작 중…")
        self._sync_controls()
        def worker() -> None:
            try:
                from brd_spd.remote import AgentConfig, WorkstationAgent

                config = AgentConfig(datadir=datadir, host=host, port=port, allegro_exe=exe, allowed_clients=allowed or None)
                agent = WorkstationAgent(config)
                info = agent.start()
                self._events.put(("started", (agent, info)))
            except Exception as exc:
                self._events.put(("error", ("start", str(exc), traceback.format_exc())))
        threading.Thread(target=worker, daemon=True).start()

    def _stop_clicked(self) -> None:
        if self._agent is None or self._busy:
            return
        if not self.confirm_interrupt("Agent를 중지할까요?"):
            return
        self._stop()

    def _stop(self, closing: bool = False) -> None:
        self._closing = self._closing or closing
        if self._agent is None or self._busy:
            return
        self._busy = True
        self.status_var.set("중지 중…")
        self._sync_controls()
        agent = self._agent
        def worker() -> None:
            try:
                agent.stop()
                self._events.put(("stopped", None))
            except Exception as exc:
                self._events.put(("error", ("stop", str(exc), traceback.format_exc())))
        threading.Thread(target=worker, daemon=True).start()

    def _on_close(self) -> None:
        """Stop the agent first so closing the GUI cannot orphan an Allegro process."""
        if not self._closing and not self.confirm_interrupt("프로그램을 닫을까요?"):
            return
        self._closing = True
        self.status_var.set("종료 중: Agent를 안전하게 중지합니다…")
        self._sync_controls()
        if self._agent is not None:
            self._stop(closing=True)
        elif not self._busy:
            self._destroy_toplevel()

    def _destroy_toplevel(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        try:
            self.winfo_toplevel().destroy()
        except tk.TclError:
            pass

    def _destroyed_event(self, event: tk.Event) -> None:
        if event.widget == self:
            self._destroyed = True

    def _drain_events(self) -> None:
        self._after_id = None
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "started":
                    self._agent, info = payload
                    self._busy = False
                    self.token_var.set(str(info.get("token", "")))
                    self.fingerprint_var.set(str(info.get("fingerprint", "")))
                    self._write_log("Agent 시작: " + str(info))
                    self._running_label = f"실행 중: {info.get('host')}:{info.get('port')}"
                    self._show_status()
                    if not save_settings({"agent_host": self.host_var.get(), "agent_port": self.port_var.get(), "agent_allowed_clients": self.allowed_var.get(), "agent_allegro_exe": self.exe_var.get(), "agent_workdir": self.workdir_var.get()}):
                        self._write_log(f"설정을 저장하지 못했습니다: {settings_file()}")
                    self._sync_controls()
                    if self._closing:
                        self._stop(closing=True)
                elif kind == "stopped":
                    self._agent = None
                    self._busy = False
                    self.token_var.set("")
                    self.fingerprint_var.set("")
                    self._write_log("Agent 중지됨")
                    self._running_label = ""
                    self.status_var.set("중지됨")
                    self._sync_controls()
                    if self._closing:
                        self._destroy_toplevel()
                elif kind == "error":
                    operation, message, details = payload
                    self._busy = False
                    self._write_log("오류: " + message)
                    self._write_log(details)
                    if operation == "stop" and self._agent is not None:
                        self.status_var.set("중지 실패 · Agent 실행 상태 유지")
                        self._sync_controls()
                        messagebox.showerror(
                            APP_NAME,
                            "Agent 중지에 실패했습니다. 창을 닫지 않았습니다. "
                            f"상태를 확인한 뒤 다시 중지하세요.\n\n{message}",
                            parent=self,
                        )
                    else:
                        self._running_label = ""
                        self.status_var.set("시작 실패")
                        self._sync_controls()
                        if self._closing:
                            self._destroy_toplevel()
                        else:
                            messagebox.showerror(
                                APP_NAME, f"Agent 시작에 실패했습니다.\n\n{message}",
                                parent=self,
                            )
        except queue.Empty:
            pass
        except Exception as exc:  # a UI-side failure must never stop the pump
            self._busy = False
            self._write_log("오류: " + str(exc))
            self._write_log(traceback.format_exc())
            self.status_var.set("오류")
            self._sync_controls()
            messagebox.showerror(APP_NAME, f"Agent 화면에서 오류가 발생했습니다.\n\n{exc}", parent=self)
        finally:
            if not self._destroyed:
                self._after_id = self.after(100, self._drain_events)
                self._status_ticks = (self._status_ticks + 1) % 10
                if self._status_ticks == 0:  # about once a second, not every pump
                    self._show_status()

    def _show_status(self) -> None:
        """Keep the running job count visible so nobody stops the agent blindly."""
        if not self._running_label or self._busy or self._closing:
            return
        count = self.running_jobs
        self.status_var.set(self._running_label + (f" · 실행 중 작업 {count}개" if count else ""))

    def _write_log(self, message: str) -> None:
        append_log(self.log, message)


def main() -> None:
    root = tk.Tk()
    try:
        root.option_add("*Font", "{Malgun Gothic} 10")
    except tk.TclError:
        pass
    AgentConsoleApp(root, standalone=True)
    root.mainloop()
