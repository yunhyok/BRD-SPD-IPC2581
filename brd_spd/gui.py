"""Tkinter front end for native SKILL generation and remote Allegro jobs."""

from __future__ import annotations

import json
import csv
import io
import os
import queue
import subprocess
import sys
import threading
import traceback
from concurrent.futures import CancelledError
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import IPC_CONVERSION_ENABLED
from .help_ui import install_help_menu
from .spd_loading import SpdLoading
from .source_snapshot import source_snapshot


APP_NAME = "BRD-SPD-IPC2581"


def parse_target_values(value: str) -> list[str] | None:
    if not value.strip():
        return None
    try:
        items = [item.strip() for item in next(csv.reader([value], skipinitialspace=True, strict=True)) if item.strip()]
    except csv.Error as exc:
        raise ValueError("대상 이름을 확인하세요. 쉼표가 포함된 이름은 따옴표로 묶거나 목록에서 선택하세요.") from exc
    if not items:
        raise ValueError("대상 이름을 선택하세요. 빈칸은 전체 대상을 의미합니다.")
    return items


def _target_row(parent, row, label, variable, command):
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
    entry = ttk.Entry(parent, textvariable=variable)
    entry.grid(row=row, column=1, sticky="ew", padx=10, pady=3)
    button = ttk.Button(parent, text="목록에서 선택…", command=command)
    button.grid(row=row, column=2, sticky="e")
    return [entry, button]


def _choose_targets(pane, source_var, kind):
    if getattr(pane, "_picker_active", False):
        return
    if pane._running or not pane.spd_loading.require_ready():
        return
    from .selection_ui import open_target_picker
    try:
        source = Path(source_var.get().strip()).resolve()
        if not source.is_file():
            raise ValueError("먼저 SPD 입력 파일을 선택하세요.")
        variable = pane.layers_var if kind == "layers" else pane.nets_var
        selected = parse_target_values(variable.get())
        layers = parse_target_values(pane.layers_var.get()) if kind == "nets" else None
    except (OSError, ValueError) as exc:
        messagebox.showerror(APP_NAME, str(exc), parent=pane)
        return

    def apply_values(values):
        if pane._running or Path(source_var.get().strip()).resolve() != source or not pane.spd_loading.require_ready():
            messagebox.showerror(APP_NAME, "작업 상태 또는 SPD 파일이 바뀌었습니다. 목록을 다시 열어 주세요.", parent=pane)
            return
        encoded = io.StringIO()
        csv.writer(encoded, lineterminator="").writerow(values)
        variable.set(encoded.getvalue())
        if kind == "layers" and pane.nets_var.get().strip():
            pane._write_log("레이어 선택이 변경되었습니다. 대상 NET 목록도 확인해 주세요.")

    picker = open_target_picker(pane, source, kind, selected, layers, apply_values)
    pane._picker_active = True
    pane.spd_loading.sync()
    picker.grab_set()

    def closed(event):
        if event.widget == picker:
            pane._picker_active = False
            try:
                pane.spd_loading.sync()
            except tk.TclError:
                pass  # Parent teardown may have already destroyed its controls.
    picker.bind("<Destroy>", closed, add="+")


class _UserCancelled(RuntimeError):
    """Internal worker signal used to stop an in-progress network upload."""


def parse_allegro_pid(value: str) -> int | None:
    """Parse an optional Windows PID without retaining it in user settings."""
    text = value.strip()
    if not text:
        return None
    if not text.isdecimal():
        raise ValueError("Allegro PID는 양의 정수여야 합니다.")
    pid = int(text)
    if not 1 <= pid <= 0xFFFFFFFF:
        raise ValueError("Allegro PID 범위가 올바르지 않습니다.")
    return pid


def pid_identity_key(host: str, port: int, fingerprint: str, pid: int) -> tuple[str, int, str, int]:
    """Key a one-session PID validation; it is intentionally never persisted."""
    return (host.strip().casefold(), int(port), fingerprint.replace(":", "").strip().casefold(), int(pid))


def checked_creation_time(identity: Any, pid: int) -> int:
    """Reject incomplete or mismatched agent PID checks before creating a job."""
    if not isinstance(identity, dict) or identity.get("pid") != pid:
        raise ValueError("Agent PID 확인 결과가 요청한 PID와 일치하지 않습니다.")
    creation = identity.get("creation_time")
    if isinstance(creation, bool) or not isinstance(creation, int) or creation <= 0:
        raise ValueError("Agent PID 확인 결과에 유효한 생성 시간이 없습니다.")
    return creation


def settings_file() -> Path:
    """Return the per-user, non-secret GUI settings location."""
    base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return base / APP_NAME / "settings.json"


def load_settings() -> dict[str, str]:
    try:
        data = json.loads(settings_file().read_text(encoding="utf-8"))
        return {str(key): str(value) for key, value in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(values: dict[str, str]) -> None:
    """Persist only workstation addresses and paths; tokens are deliberately excluded."""
    safe = {key: value for key, value in values.items() if "token" not in key.lower()}
    path = settings_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        merged = {key: value for key, value in load_settings().items() if "token" not in key.lower()}
        merged.update(safe)
        path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


class ConverterApp(ttk.Frame):
    """A small, self-contained GUI which keeps conversion work off the UI thread."""

    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=16)
        self.master = master
        self.source_var = tk.StringVar()
        self.template_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.status_var = tk.StringVar(value="SPD 파일을 선택하세요.")
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._running = False
        self._last_output: Path | None = None
        self._build()
        self.after(100, self._drain_events)

    def _build(self) -> None:
        top = self.winfo_toplevel()
        top.title(APP_NAME)
        top.minsize(720, 500)
        self.master.columnconfigure(0, weight=1)
        self.master.rowconfigure(0, weight=1)
        self.grid(sticky="nsew")
        self.columnconfigure(1, weight=1)
        self.rowconfigure(7, weight=1)

        ttk.Label(self, text="SPD → IPC-2581 / BRD 변환", font=("Malgun Gothic", 15, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 12)
        )
        self._file_row(1, "SPD 입력 파일", self.source_var, self._choose_source, "SPD 파일 (*.spd)")
        self._file_row(2, "템플릿 XML (선택)", self.template_var, self._choose_template, "XML 파일 (*.xml)")
        self._file_row(3, "출력 XML 파일", self.output_var, self._choose_output, "XML 파일 (*.xml)")

        limitation = (
            "참고: 이 결과는 제조·비교용 IPC 형상 교환본입니다. 원본 BRD의 배선·동적 plane은 자동으로 갱신되지 않습니다."
        )
        ttk.Label(self, text=limitation, foreground="#8a4b00", wraplength=660).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(10, 4)
        )

        controls = ttk.Frame(self)
        controls.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(8, 8))
        controls.columnconfigure(2, weight=1)
        self.convert_button = ttk.Button(controls, text="변환 시작", command=self._start_conversion)
        self.convert_button.grid(row=0, column=0, padx=(0, 8))
        self.open_button = ttk.Button(controls, text="출력 폴더 열기", command=self._open_output_folder, state="disabled")
        self.open_button.grid(row=0, column=1)
        ttk.Label(controls, textvariable=self.status_var).grid(row=0, column=2, sticky="e")

        ttk.Label(self, text="작업 로그").grid(row=6, column=0, columnspan=3, sticky="nw")
        log_frame = ttk.Frame(self)
        log_frame.grid(row=7, column=0, columnspan=3, sticky="nsew", pady=(4, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=14, wrap="word", state="disabled", font=("Consolas", 10))
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self._write_log("준비되었습니다. 입력 SPD와 출력 XML 경로를 지정한 뒤 변환을 시작하세요.")

    def _file_row(self, row: int, label: str, variable: tk.StringVar, command: Any, file_type: str) -> None:
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(self, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=10, pady=5)
        ttk.Button(self, text="찾아보기…", command=command).grid(row=row, column=2, sticky="e", pady=5)

    def _choose_source(self) -> None:
        path = filedialog.askopenfilename(title="SPD 파일 선택", filetypes=[("SPD 파일", "*.spd"), ("모든 파일", "*.*")])
        if path:
            source = Path(path)
            self.source_var.set(str(source))
            if not self.output_var.get().strip():
                self.output_var.set(str(source.with_suffix(".xml")))

    def _choose_template(self) -> None:
        path = filedialog.askopenfilename(title="템플릿 XML 선택 (선택 사항)", filetypes=[("XML 파일", "*.xml"), ("모든 파일", "*.*")])
        if path:
            self.template_var.set(path)

    def _choose_output(self) -> None:
        initial = self.output_var.get().strip() or "output.xml"
        path = filedialog.asksaveasfilename(
            title="출력 IPC-2581 XML 저장 위치", initialfile=Path(initial).name,
            defaultextension=".xml", filetypes=[("XML 파일", "*.xml"), ("모든 파일", "*.*")]
        )
        if path:
            self.output_var.set(path)

    def _start_conversion(self) -> None:
        if not IPC_CONVERSION_ENABLED:
            messagebox.showinfo(APP_NAME, "IPC-2581 변환은 현재 비활성화되어 있습니다.")
            return
        if self._running:
            return
        source_text, output_text, template_text = (self.source_var.get().strip(), self.output_var.get().strip(), self.template_var.get().strip())
        if not source_text:
            messagebox.showerror(APP_NAME, "입력 SPD 파일을 선택하세요.")
            return
        source = Path(source_text)
        if not source.is_file():
            messagebox.showerror(APP_NAME, f"입력 파일을 찾을 수 없습니다.\n{source}")
            return
        if not output_text:
            output_text = str(source.with_suffix(".xml"))
            self.output_var.set(output_text)
        output = Path(output_text)
        if output.suffix.lower() != ".xml":
            output = output.with_suffix(".xml")
            self.output_var.set(str(output))
        template = Path(template_text) if template_text else None
        if template is not None and not template.is_file():
            messagebox.showerror(APP_NAME, f"템플릿 파일을 찾을 수 없습니다.\n{template}")
            return
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"출력 폴더를 만들 수 없습니다.\n{exc}")
            return
        self._running = True
        self._last_output = output
        self.convert_button.configure(state="disabled")
        self.open_button.configure(state="disabled")
        self.status_var.set("변환 중…")
        self._write_log(f"변환 시작: {source}")
        thread = threading.Thread(target=self._convert_worker, args=(source, output, template), daemon=True)
        thread.start()

    def _convert_worker(self, source: Path, output: Path, template: Path | None) -> None:
        try:
            from brd_spd.convert import convert

            def progress(message: Any) -> None:
                self._events.put(("log", str(message)))

            report = convert(source, output, template=template, progress=progress)
            self._events.put(("complete", (output, report)))
        except Exception as exc:  # surfaced on the UI thread with context
            self._events.put(("error", (str(exc), traceback.format_exc())))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "log":
                    self._write_log(payload)
                elif kind == "complete":
                    output, report = payload
                    self._finish_success(output, report)
                elif kind == "error":
                    self._finish_error(*payload)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _finish_success(self, output: Path, report: Any) -> None:
        self._running = False
        self.convert_button.configure(state="normal")
        self.open_button.configure(state="normal")
        self.status_var.set("완료")
        self._write_log(f"완료: {output}")
        report_data = report if isinstance(report, dict) else {}
        warnings = report_data.get("warnings") if isinstance(report_data.get("warnings"), dict) else {}
        warning_summary = ", ".join(
            f"{code} ({item.get('count', 0) if isinstance(item, dict) else 0})"
            for code, item in warnings.items()
        )
        report_path = report_data.get("report_path")
        log_path = report_data.get("log_path")
        if warning_summary:
            self.status_var.set("경고와 함께 완료")
            self._write_log("경고 범주: " + warning_summary)
        if report_path:
            self._write_log("상세 보고서: " + str(report_path))
        if log_path:
            self._write_log("변환 로그: " + str(log_path))
        if warning_summary:
            message = f"변환이 경고와 함께 완료되었습니다.\n\n출력: {output}\n경고: {warning_summary}"
        else:
            message = f"변환이 완료되었습니다.\n\n출력: {output}"
        if report_path:
            message += f"\n보고서: {report_path}"
        if log_path:
            message += f"\n로그: {log_path}"
        messagebox.showinfo(APP_NAME, message)

    def _finish_error(self, message: str, details: str) -> None:
        self._running = False
        self.convert_button.configure(state="normal")
        self.status_var.set("오류")
        self._write_log("오류: " + message)
        self._write_log(details)
        messagebox.showerror(APP_NAME, f"변환에 실패했습니다.\n\n{message}\n\n자세한 내용은 작업 로그를 확인하세요.")

    def _open_output_folder(self) -> None:
        target = (self._last_output or Path(self.output_var.get())).parent
        if not target.exists():
            messagebox.showerror(APP_NAME, f"출력 폴더를 찾을 수 없습니다.\n{target}")
            return
        try:
            if sys.platform == "win32":
                os.startfile(target)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", str(target)], check=False)
            else:
                subprocess.run(["xdg-open", str(target)], check=False)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"폴더를 열 수 없습니다.\n{exc}")

    def _write_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")


class RemoteClientPane(ttk.Frame):
    """Client for the separate workstation agent. All network I/O runs on a worker."""

    def __init__(self, master: tk.Misc, *, show_heading: bool = True) -> None:
        super().__init__(master, padding=16)
        self._show_heading = show_heading
        persisted = load_settings()
        self.host_var = tk.StringVar(value=persisted.get("remote_host", "127.0.0.1"))
        self.port_var = tk.StringVar(value=persisted.get("remote_port", "8765"))
        self.token_var = tk.StringVar()  # credentials must not be persisted
        self.fingerprint_var = tk.StringVar(value=persisted.get("remote_fingerprint", ""))
        self.spd_var = tk.StringVar(value=persisted.get("remote_spd", ""))
        self.brd_var = tk.StringVar(value=persisted.get("remote_brd", ""))
        self.output_var = tk.StringVar(value=persisted.get("remote_output", ""))
        self.job_var = tk.StringVar(value=persisted.get("remote_job_id", ""))
        self.layers_var = tk.StringVar(value=persisted.get("remote_layers", ""))
        self.nets_var = tk.StringVar(value=persisted.get("remote_nets", ""))
        self.pid_var = tk.StringVar()  # PID reuse makes persistence unsafe.
        self.components_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="연결 정보를 입력하세요.")
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._running = False
        self._cancel_requested = False
        self._job_id = ""
        self._client: Any = None
        self._checked_pid: tuple[tuple[str, int, str, int], int] | None = None
        self._build()
        self.pid_var.trace_add("write", self._update_pid_note)
        self.after(100, self._drain_events)

    def _build(self) -> None:
        self.columnconfigure(1, weight=1)
        if self._show_heading:
            ttk.Label(self, text="원격 Allegro 24.1 작업", font=("Malgun Gothic", 15, "bold")).grid(
                row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
            )
        self._row(2, "워크스테이션 IP", self.host_var)
        self._row(3, "포트", self.port_var)
        self._row(4, "접속 토큰", self.token_var, show="•")
        self._row(5, "인증서 지문", self.fingerprint_var)
        ttk.Label(self, text="Allegro PID (빈칸: 새 실행)").grid(row=6, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=self.pid_var).grid(row=6, column=1, sticky="ew", padx=10, pady=3)
        self.pid_check_button = ttk.Button(self, text="PID 확인", command=self._check_pid)
        self.pid_check_button.grid(row=6, column=2, sticky="e")
        self.pid_note_var = tk.StringVar(value="빈칸이면 업로드한 원본 BRD로 새 Allegro 작업을 실행합니다.")
        ttk.Label(self, textvariable=self.pid_note_var, foreground="#8a4b00", wraplength=700).grid(row=7, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self.spd_loading = SpdLoading(self, self.spd_var, 8, self._choose_spd)
        self._file_row(9, "원본 BRD", self.brd_var, self._choose_brd, "BRD 파일", "*.brd")
        self._file_row(10, "출력 ZIP", self.output_var, self._choose_output, "ZIP 파일", "*.zip")
        self._row(11, "기존 작업 ID", self.job_var)
        targets = _target_row(self, 12, "대상 레이어 (빈칸: 전체)", self.layers_var, lambda: _choose_targets(self, self.spd_var, "layers"))
        targets += _target_row(self, 13, "대상 NET (빈칸: 전체)", self.nets_var, lambda: _choose_targets(self, self.spd_var, "nets"))
        self.components_check = ttk.Checkbutton(self, text="부품 위치/회전 업데이트 포함", variable=self.components_var)
        self.components_check.grid(row=14, column=1, sticky="w", pady=3)
        controls = ttk.Frame(self)
        controls.grid(row=15, column=0, columnspan=3, sticky="ew", pady=(10, 6))
        self.connect_button = ttk.Button(controls, text="연결 확인", command=self._connect)
        self.connect_button.grid(row=0, column=0, padx=(0, 6))
        self.run_button = ttk.Button(controls, text="전송 및 실행", command=self._run)
        self.run_button.grid(row=0, column=1, padx=(0, 6))
        self.spd_loading.bind_controls(targets + [self.components_check, self.run_button])
        self.resume_button = ttk.Button(controls, text="기존 작업 받기", command=self._resume)
        self.resume_button.grid(row=0, column=2, padx=(0, 6))
        self.cancel_button = ttk.Button(controls, text="취소", command=self._cancel, state="disabled")
        self.cancel_button.grid(row=0, column=3, padx=(0, 10))
        ttk.Label(controls, textvariable=self.status_var).grid(row=0, column=4, sticky="w")
        ttk.Label(self, text="원격 작업 로그").grid(row=16, column=0, columnspan=3, sticky="w")
        frame = ttk.Frame(self)
        frame.grid(row=17, column=0, columnspan=3, sticky="nsew", pady=(4, 0))
        self.rowconfigure(17, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.log = tk.Text(frame, height=7, wrap="word", state="disabled", font=("Consolas", 10))
        scrollbar = ttk.Scrollbar(frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

    def _row(self, row: int, label: str, variable: tk.StringVar, show: str | None = None) -> None:
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=variable, show=show or "").grid(row=row, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=3)

    def _file_row(self, row: int, label: str, variable: tk.StringVar, command: Any, type_name: str, pattern: str) -> None:
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=10, pady=3)
        ttk.Button(self, text="찾아보기…", command=command).grid(row=row, column=2, sticky="e")

    def _choose_spd(self) -> None:
        path = filedialog.askopenfilename(title="SPD 파일 선택", filetypes=[("SPD 파일", "*.spd"), ("모든 파일", "*.*")])
        if path:
            self.spd_var.set(path)
            self.spd_loading.load()

    def _choose_brd(self) -> None:
        path = filedialog.askopenfilename(title="원본 BRD 선택", filetypes=[("Allegro BRD", "*.brd"), ("모든 파일", "*.*")])
        if path:
            self.brd_var.set(path)

    def _choose_output(self) -> None:
        path = filedialog.asksaveasfilename(title="결과 ZIP 저장", defaultextension=".zip", filetypes=[("ZIP 파일", "*.zip")])
        if path:
            self.output_var.set(path)

    def _update_pid_note(self, *_: Any) -> None:
        try:
            pid = parse_allegro_pid(self.pid_var.get())
        except ValueError:
            self.pid_note_var.set("PID 형식을 확인하세요. 1 이상 4,294,967,295 이하의 정수만 사용할 수 있습니다.")
        else:
            self.pid_note_var.set("PID 입력시 해당 Allegro에 열린 BRD를 사용합니다." if pid else "빈칸이면 업로드한 원본 BRD로 새 Allegro 작업을 실행합니다.")

    def _check_pid(self) -> None:
        if self._running:
            return
        try:
            host, port, token, fingerprint = self._config()
            pid = parse_allegro_pid(self.pid_var.get())
            if pid is None:
                raise ValueError("확인할 Allegro PID를 입력하세요.")
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self._start_worker(self._check_pid_worker, host, port, token, fingerprint, pid)

    def _check_pid_worker(self, host: str, port: int, token: str, fingerprint: str, pid: int) -> None:
        from brd_spd.remote import RemoteClient

        client = RemoteClient(host, port=port, token=token, fingerprint=fingerprint, timeout=30)
        result = client.check_pid(pid)
        self._events.put(("pid_validated", (pid_identity_key(host, port, fingerprint, pid), result)))

    def _config(self) -> tuple[str, int, str, str]:
        host = self.host_var.get().strip()
        token = self.token_var.get().strip()
        fingerprint = self.fingerprint_var.get().strip()
        try:
            port = int(self.port_var.get().strip())
        except ValueError as exc:
            raise ValueError("포트는 숫자여야 합니다.") from exc
        if not host or not token or not fingerprint:
            raise ValueError("IP, 토큰, 인증서 지문을 모두 입력하세요.")
        return host, port, token, fingerprint

    def _connect(self) -> None:
        if self._running:
            return
        try:
            host, port, token, fingerprint = self._config()
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self._start_worker(self._connect_worker, host, port, token, fingerprint)

    def _connect_worker(self, host: str, port: int, token: str, fingerprint: str) -> None:
        from brd_spd.remote import RemoteClient

        client = RemoteClient(host, port=port, token=token, fingerprint=fingerprint, timeout=30)
        health = client.health()
        self._events.put(("connected", (client, health)))

    def _run(self) -> None:
        if self._running or getattr(self, "_picker_active", False) or not self.spd_loading.require_ready():
            return
        try:
            host, port, token, fingerprint = self._config()
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        source_text, brd_text, output_text = self.spd_var.get().strip(), self.brd_var.get().strip(), self.output_var.get().strip()
        try:
            pid = parse_allegro_pid(self.pid_var.get())
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        source = Path(source_text)
        base_brd = Path(brd_text) if brd_text and pid is None else None
        if not source.is_file() or (pid is None and (base_brd is None or not base_brd.is_file())):
            messagebox.showerror(APP_NAME, "SPD는 항상 필요하며, PID를 비우면 원본 BRD도 필요합니다.")
            return
        if not output_text:
            messagebox.showerror(APP_NAME, "결과 ZIP 저장 위치를 지정하세요.")
            return
        output = Path(output_text)
        if output.exists():
            messagebox.showerror(APP_NAME, "결과 ZIP은 새 파일 경로여야 합니다. 기존 파일을 다른 이름으로 지정하세요.")
            return
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"출력 폴더를 만들 수 없습니다.\n{exc}")
            return
        self._cancel_requested = False
        try:
            options = {"layers": self._items(self.layers_var.get()), "nets": self._items(self.nets_var.get()), "update_components": self.components_var.get()}
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        if pid is not None:
            options["allegro_pid"] = pid
            key = pid_identity_key(host, port, fingerprint, pid)
            prior = self._checked_pid
            if prior is not None and prior[0] == key:
                options["allegro_creation_time"] = prior[1]
        options = {key: value for key, value in options.items() if value is not None}
        self._start_worker(self._run_worker, host, port, token, fingerprint, source, base_brd, output, options, self.spd_loading.signature)

    @staticmethod
    def _items(value: str) -> list[str] | None:
        return parse_target_values(value)

    def _resume(self) -> None:
        if self._running:
            return
        try:
            host, port, token, fingerprint = self._config()
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        job_id, output_text = self.job_var.get().strip(), self.output_var.get().strip()
        if not job_id or not output_text:
            messagebox.showerror(APP_NAME, "기존 작업 ID와 결과 ZIP 저장 위치를 지정하세요.")
            return
        output = Path(output_text)
        if output.exists():
            messagebox.showerror(APP_NAME, "결과 ZIP은 새 파일 경로여야 합니다. 기존 파일을 다른 이름으로 지정하세요.")
            return
        self._cancel_requested = False
        self._start_worker(self._resume_worker, host, port, token, fingerprint, job_id, output)

    def _resume_worker(self, host: str, port: int, token: str, fingerprint: str, job_id: str, output: Path) -> None:
        from brd_spd.remote import RemoteClient

        client = RemoteClient(host, port=port, token=token, fingerprint=fingerprint, timeout=30)
        self._client, self._job_id = client, job_id
        self._events.put(("log", f"기존 작업 연결: {job_id}"))
        self._watch_job(client, job_id, output)

    def _run_worker(self, host: str, port: int, token: str, fingerprint: str, source: Path, base_brd: Path | None, output: Path, options: dict[str, Any], source_signature) -> None:
        from brd_spd.remote import RemoteClient

        client = RemoteClient(host, port=port, token=token, fingerprint=fingerprint, timeout=30)
        self._client = client
        self._events.put(("log", "워크스테이션 연결 확인"))
        self._events.put(("log", str(client.health())))
        if "allegro_pid" in options and "allegro_creation_time" not in options:
            pid = int(options["allegro_pid"])
            identity = client.check_pid(pid)
            options = dict(options)
            options["allegro_creation_time"] = checked_creation_time(identity, pid)
            self._events.put(("log", f"Allegro PID {pid}를 작업 직전에 확인했습니다."))
        job_id = ""
        try:
            job = client.create_job(options)
            job_id = str(job["id"])
            self._job_id = job_id
            self._events.put(("job", job_id))
            self._events.put(("log", f"작업 생성: {job_id}"))

            def upload_progress(value: Any) -> None:
                if self._cancel_requested:
                    raise _UserCancelled("사용자가 업로드를 취소했습니다.")
                self._events.put(("log", f"업로드: {value}"))

            if self._cancel_requested:
                raise _UserCancelled("사용자가 작업을 취소했습니다.")
            self._events.put(("log", "로딩한 SPD의 작업용 사본 준비"))
            with source_snapshot(source, source_signature, output.parent, lambda: self._cancel_requested) as snapshot:
                client.upload_file(job_id, "spd", snapshot, progress=upload_progress)
            if base_brd is not None:
                client.upload_file(job_id, "brd", base_brd, progress=upload_progress)
            if self._cancel_requested:
                raise _UserCancelled("사용자가 작업을 취소했습니다.")
            client.submit_job(job_id)
        except (_UserCancelled, CancelledError):
            client.cancel_job(job_id)
            self._events.put(("log", "취소 요청을 Agent에 전송했습니다."))
        except Exception:
            if job_id:
                try:
                    client.cancel_job(job_id)
                    self._events.put(("log", "오류 후 Agent 작업 취소를 요청했습니다."))
                except Exception:
                    pass
            raise
        self._watch_job(client, job_id, output)

    def _watch_job(self, client: Any, job_id: str, output: Path) -> None:
        offset = 0
        cancel_sent = False
        while True:
            if self._cancel_requested and not cancel_sent:
                client.cancel_job(job_id)
                self._events.put(("log", "취소 요청을 Agent에 전송했습니다. 최종 상태를 기다립니다."))
                cancel_sent = True
            status = client.get_job(job_id)
            logs = client.get_logs(job_id, offset=offset)
            text = str(logs.get("text", ""))
            offset = int(logs.get("next_offset", offset))
            if text:
                self._events.put(("log", text))
            state = str(status.get("status", ""))
            self._events.put(("status", f"원격 작업: {state}"))
            if state in {"succeeded", "completed", "completed_with_warnings"}:
                def download_progress(value: Any) -> None:
                    self._events.put(("log", f"다운로드: {value}"))
                client.download_result(job_id, output, progress=download_progress)
                self._events.put(("complete", (output, status)))
                return
            if state in {"failed", "cancelled", "interrupted"}:
                try:
                    def diagnostic_progress(value: Any) -> None:
                        self._events.put(("log", f"진단 ZIP 다운로드: {value}"))
                    client.download_result(job_id, output, progress=diagnostic_progress)
                    self._events.put(("failed_result", (output, status)))
                    return
                except Exception as exc:
                    self._events.put(("log", f"진단 ZIP을 받을 수 없습니다: {exc}"))
                raise RuntimeError(status.get("error") or f"원격 작업이 {state} 상태로 끝났습니다.")
            threading.Event().wait(1.0)

    def _cancel(self) -> None:
        if self._running:
            self._cancel_requested = True
            self.status_var.set("취소 요청 중…")
            self.cancel_button.configure(state="disabled")

    def _start_worker(self, target: Any, *args: Any) -> None:
        self._running = True
        self.spd_loading.sync()
        self.connect_button.configure(state="disabled")
        self.run_button.configure(state="disabled")
        self.resume_button.configure(state="disabled")
        self.pid_check_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status_var.set("작업 중…")
        def runner() -> None:
            try:
                target(*args)
            except Exception as exc:
                self._events.put(("error", (str(exc), traceback.format_exc())))
        threading.Thread(target=runner, daemon=True).start()

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "log": self._write_log(str(payload))
                elif kind == "status": self.status_var.set(str(payload))
                elif kind == "job": self.job_var.set(str(payload))
                elif kind == "pid_validated":
                    key, identity = payload
                    creation = checked_creation_time(identity, key[3])
                    self._checked_pid = (key, creation)
                    executable = identity.get("executable", "")
                    pid = identity.get("pid", self.pid_var.get())
                    self._write_log(f"PID 확인됨: {pid} | {executable} | 생성: {creation}")
                    self._finish("PID 확인됨")
                elif kind == "connected":
                    self._client, health = payload
                    self._write_log("연결 확인: " + str(health))
                    self._finish("연결됨")
                elif kind == "complete":
                    output, status = payload
                    self._write_log("완료: " + str(status))
                    self._finish("완료")
                    messagebox.showinfo(APP_NAME, f"원격 작업 결과를 저장했습니다.\n{output}")
                elif kind == "failed_result":
                    output, status = payload
                    self._write_log("실패 진단 ZIP 저장: " + str(status))
                    self._finish("실패 진단 ZIP 저장됨")
                    messagebox.showwarning(APP_NAME, f"원격 작업은 실패했지만 진단 ZIP을 저장했습니다.\n{output}")
                elif kind == "cancelled":
                    self._write_log("작업 취소됨")
                    self._finish("취소됨")
                elif kind == "error":
                    message, details = payload
                    self._write_log("오류: " + message)
                    self._write_log(details)
                    self._finish("오류")
                    messagebox.showerror(APP_NAME, f"원격 작업에 실패했습니다.\n\n{message}")
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _finish(self, status: str) -> None:
        self._running = False
        self.connect_button.configure(state="normal")
        self.spd_loading.sync()
        self.resume_button.configure(state="normal")
        self.pid_check_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.status_var.set(status)
        save_settings({"remote_host": self.host_var.get(), "remote_port": self.port_var.get(), "remote_fingerprint": self.fingerprint_var.get(), "remote_spd": self.spd_var.get(), "remote_brd": self.brd_var.get(), "remote_output": self.output_var.get(), "remote_job_id": self.job_var.get(), "remote_layers": self.layers_var.get(), "remote_nets": self.nets_var.get()})

    def _write_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")


class SkillPane(ttk.Frame):
    """Create a local, reviewable native-import SKILL bundle without Cadence execution."""

    def __init__(self, master: ttk.Notebook) -> None:
        super().__init__(master, padding=16)
        persisted = load_settings()
        self.source_var = tk.StringVar(value=persisted.get("skill_spd", ""))
        self.output_var = tk.StringVar(value=persisted.get("skill_output", ""))
        self.layers_var = tk.StringVar(value=persisted.get("skill_layers", ""))
        self.nets_var = tk.StringVar(value=persisted.get("skill_nets", ""))
        self.components_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="SPD 파일과 출력 폴더를 지정하세요.")
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._running = False
        self._build()
        self.after(100, self._drain_events)

    def _build(self) -> None:
        self.columnconfigure(1, weight=1)
        self.rowconfigure(7, weight=1)
        ttk.Label(self, text="Native import SKILL 생성", font=("Malgun Gothic", 15, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(self, text="SKILL은 라이선스가 있는 Allegro 워크스테이션에서 검토 후 실행합니다. 원본 BRD의 규칙·배선·동적 plane 자동 복원은 범위에 포함되지 않습니다.", foreground="#8a4b00", wraplength=720).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))
        self.spd_loading = SpdLoading(self, self.source_var, 2, self._choose_source)
        self._folder_row(3, "새 번들 출력 폴더", self.output_var, self._choose_output)
        targets = _target_row(self, 4, "대상 레이어 (빈칸: 전체)", self.layers_var, lambda: _choose_targets(self, self.source_var, "layers"))
        targets += _target_row(self, 5, "대상 NET (빈칸: 전체)", self.nets_var, lambda: _choose_targets(self, self.source_var, "nets"))
        self.components_check = ttk.Checkbutton(self, text="기존 부품 위치/회전 데이터 포함", variable=self.components_var)
        self.components_check.grid(row=6, column=1, sticky="w", pady=4)
        controls = ttk.Frame(self)
        controls.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(8, 6))
        self.generate_button = ttk.Button(controls, text="SKILL 번들 생성", command=self._generate)
        self.generate_button.grid(row=0, column=0, padx=(0, 8))
        self.spd_loading.bind_controls(targets + [self.components_check, self.generate_button])
        self.open_button = ttk.Button(controls, text="출력 폴더 열기", command=self._open_folder, state="disabled")
        self.open_button.grid(row=0, column=1)
        ttk.Label(controls, textvariable=self.status_var).grid(row=0, column=2, sticky="w", padx=12)
        frame = ttk.Frame(self)
        frame.grid(row=8, column=0, columnspan=3, sticky="nsew", pady=(4, 0))
        self.rowconfigure(8, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.log = tk.Text(frame, height=13, wrap="word", state="disabled", font=("Consolas", 10))
        scroll = ttk.Scrollbar(frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

    def _entry_row(self, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=variable).grid(row=row, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=3)

    def _file_row(self, row: int, label: str, variable: tk.StringVar, command: Any, _type: str, _pattern: str) -> None:
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=10, pady=3)
        ttk.Button(self, text="찾아보기…", command=command).grid(row=row, column=2)

    def _folder_row(self, row: int, label: str, variable: tk.StringVar, command: Any) -> None:
        self._file_row(row, label, variable, command, "", "")

    def _choose_source(self) -> None:
        path = filedialog.askopenfilename(title="SPD 파일 선택", filetypes=[("SPD 파일", "*.spd"), ("모든 파일", "*.*")])
        if path:
            self.source_var.set(path)
            self.spd_loading.load()

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(title="새 SKILL 번들을 만들 상위 폴더 선택")
        if path:
            source_name = Path(self.source_var.get().strip() or "spd").stem
            parent = Path(path)
            candidate = parent / f"{source_name}-skill-bundle"
            suffix = 2
            while candidate.exists():
                candidate = parent / f"{source_name}-skill-bundle-{suffix}"
                suffix += 1
            self.output_var.set(str(candidate))

    @staticmethod
    def _items(value: str) -> list[str] | None:
        return parse_target_values(value)

    def _generate(self) -> None:
        if self._running or getattr(self, "_picker_active", False) or not self.spd_loading.require_ready():
            return
        source_text, output_text = self.source_var.get().strip(), self.output_var.get().strip()
        source = Path(source_text)
        if not source.is_file() or not output_text:
            messagebox.showerror(APP_NAME, "SPD 파일과 번들 출력 폴더가 필요합니다.")
            return
        output = Path(output_text)
        if output.exists():
            messagebox.showerror(APP_NAME, "새 번들 출력 폴더를 지정하세요. 이미 존재하는 폴더에는 생성할 수 없습니다.")
            return
        try:
            layers, nets = self._items(self.layers_var.get()), self._items(self.nets_var.get())
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        update_components = self.components_var.get()
        source_signature = self.spd_loading.signature
        self._running = True
        self.spd_loading.sync()
        self.open_button.configure(state="disabled")
        self.status_var.set("SKILL 생성 중…")
        def worker() -> None:
            try:
                from brd_spd.skill import generate_bundle

                def progress(message: Any) -> None:
                    self._events.put(("log", str(message)))
                output.parent.mkdir(parents=True, exist_ok=True)
                progress("로딩한 SPD의 작업용 사본 준비")
                with source_snapshot(source, source_signature, output.parent) as snapshot:
                    report = generate_bundle(snapshot, output, layers=layers, nets=nets, update_components=update_components, progress=progress)
                self._events.put(("complete", (output, report)))
            except Exception as exc:
                self._events.put(("error", (str(exc), traceback.format_exc())))
        threading.Thread(target=worker, daemon=True).start()

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "log":
                    self._write_log(str(payload))
                elif kind == "complete":
                    output, report = payload
                    self._write_log("완료: " + str(report))
                    self._finish("완료")
                    messagebox.showinfo(APP_NAME, f"SKILL 번들이 생성되었습니다.\n{output}")
                elif kind == "error":
                    message, details = payload
                    self._write_log("오류: " + message)
                    self._write_log(details)
                    self._finish("오류")
                    messagebox.showerror(APP_NAME, f"SKILL 생성에 실패했습니다.\n\n{message}")
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _finish(self, status: str) -> None:
        self._running = False
        self.spd_loading.sync()
        self.open_button.configure(state="normal")
        self.status_var.set(status)
        save_settings({"skill_spd": self.source_var.get(), "skill_output": self.output_var.get(), "skill_layers": self.layers_var.get(), "skill_nets": self.nets_var.get()})

    def _open_folder(self) -> None:
        path = Path(self.output_var.get().strip())
        if not path.is_dir():
            messagebox.showerror(APP_NAME, "출력 폴더를 찾을 수 없습니다.")
            return
        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.run(["xdg-open", str(path)], check=False)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"폴더를 열 수 없습니다.\n{exc}")

    def _write_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")


class RemoteWorkPane(ttk.Frame):
    """Show one computer's role while reusing the existing client and agent."""

    def __init__(self, master: ttk.Notebook, skill: SkillPane) -> None:
        super().__init__(master)
        self.skill = skill
        saved_role = load_settings().get("remote_role", "laptop")
        self.role = saved_role if saved_role in ("laptop", "workstation") else "laptop"
        self.role_var = tk.StringVar(value=self.role)
        self.hint_var = tk.StringVar()
        self.lock_var = tk.StringVar()
        self.agent = None
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        header = ttk.Frame(self, padding=(16, 12, 16, 0))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="이 컴퓨터의 역할", font=("Malgun Gothic", 12, "bold")).grid(sticky="w")
        choices = ttk.Frame(header)
        choices.grid(sticky="w", pady=(6, 4))
        self.role_buttons = []
        for column, (value, label) in enumerate((
            ("laptop", "노트북 (작업 요청)"),
            ("workstation", "워크스테이션 (Allegro 실행)"),
        )):
            button = ttk.Radiobutton(choices, text=label, value=value, variable=self.role_var,
                                     command=self._select_role)
            button.grid(row=0, column=column, padx=(0, 24))
            self.role_buttons.append(button)
        ttk.Label(header, textvariable=self.hint_var, wraplength=740).grid(sticky="w")
        ttk.Label(header, textvariable=self.lock_var, foreground="#8a4b00", wraplength=740).grid(sticky="w")
        self.client = RemoteClientPane(self, show_heading=False)
        self._show_role()
        self._timer = self.after(100, self._refresh_roles)
        self.bind("<Destroy>", self._destroy, add="+")

    def _busy_reason(self) -> str:
        if self.agent is not None and not self.agent.can_switch_role:
            return "역할을 바꾸려면 Agent 중지를 완료하세요."
        for pane in (self.client, self.skill):
            if pane._running or pane.spd_loading.loading or getattr(pane, "_picker_active", False):
                return "작업·SPD 로딩·목록 선택이 끝나면 역할을 바꿀 수 있습니다."
        return ""

    def _refresh_roles(self) -> None:
        reason = self._busy_reason()
        self.lock_var.set(reason)
        for button in self.role_buttons:
            button.configure(state="disabled" if reason else "normal")
        self._timer = self.after(100, self._refresh_roles)

    def _select_role(self) -> None:
        selected = self.role_var.get()
        reason = self._busy_reason()
        if reason or selected not in ("laptop", "workstation"):
            self.role_var.set(self.role)
            self.lock_var.set(reason)
            return
        self.role = selected
        self._show_role()
        save_settings({"remote_role": self.role})
        self.event_generate("<<RemoteRoleChanged>>")

    def _show_role(self) -> None:
        if self.role == "workstation":
            if self.agent is None:
                from .agent_gui import AgentConsoleApp
                self.agent = AgentConsoleApp(self, standalone=False)
            self.client.grid_remove()
            self.agent.grid(row=1, column=0, sticky="nsew")
            self.hint_var.set("Allegro가 설치된 컴퓨터입니다. 아래 설정으로 Agent를 시작하고, 표시된 토큰과 인증서 지문을 노트북에 입력하세요.")
        else:
            if self.agent is not None:
                self.agent.grid_remove()
            self.client.grid(row=1, column=0, sticky="nsew")
            self.hint_var.set("SPD를 보내고 결과·로그를 받는 컴퓨터입니다. 워크스테이션에서 Agent를 시작한 뒤 아래 연결 정보를 입력하세요.")

    def help_topic(self) -> str:
        if self.role == "workstation":
            return "workstation"
        return "remote-pid" if self.client.pid_var.get().strip() else "remote-new"

    def _destroy(self, event: tk.Event) -> None:
        if event.widget == self and self._timer is not None:
            self.after_cancel(self._timer)
            self._timer = None


class DesktopApp(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master)
        master.title(APP_NAME)
        master.minsize(760, 660)
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.grid(sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        notebook = self.notebook = ttk.Notebook(self)
        notebook.grid(sticky="nsew")
        ipc = ttk.Frame(notebook)
        ipc.columnconfigure(0, weight=1)
        ipc.rowconfigure(0, weight=1)
        if IPC_CONVERSION_ENABLED:
            ConverterApp(ipc).grid(row=0, column=0, sticky="nsew")
        else:
            ttk.Label(ipc, text="IPC-2581 변환은 현재 비활성화되어 있습니다.").grid(padx=24, pady=24)
        skill = self.skill = SkillPane(notebook)
        remote = self.remote = RemoteWorkPane(notebook, skill)
        notebook.add(ipc, text="IPC-2581 변환" if IPC_CONVERSION_ENABLED else "IPC-2581 변환 (비활성)",
                     state="normal" if IPC_CONVERSION_ENABLED else "disabled")
        notebook.add(skill, text="Native SKILL 생성")
        notebook.add(remote, text="원격 Allegro 작업")
        notebook.select(remote)
        remote.bind("<<RemoteRoleChanged>>", self._role_changed)
        self._role_changed()
        self._menu, self._help_menu = install_help_menu(
            master, lambda: remote.help_topic()
            if notebook.select() == str(remote) else "native-skill"
        )
        master.protocol("WM_DELETE_WINDOW", self._on_close)

    def _role_changed(self, _event: Any = None) -> None:
        self.notebook.tab(self.skill, state="disabled" if self.remote.role == "workstation" else "normal")

    def _on_close(self) -> None:
        if self.remote.agent is not None and not self.remote.agent.can_switch_role:
            self.remote.agent._on_close()
        elif self.remote._busy_reason():
            messagebox.showinfo(APP_NAME, "진행 중인 작업을 완료하거나 취소한 뒤 프로그램을 닫으세요.", parent=self)
        else:
            self.master.destroy()


def main() -> None:
    root = tk.Tk()
    try:
        root.option_add("*Font", "{Malgun Gothic} 10")
    except tk.TclError:
        pass
    DesktopApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
