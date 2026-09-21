"""Tkinter front end for converting SPD files to IPC-2581 XML."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_NAME = "BRD-SPD-IPC2581"


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
        self.master.title(APP_NAME)
        self.master.minsize(720, 500)
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


def main() -> None:
    root = tk.Tk()
    try:
        root.option_add("*Font", "Malgun Gothic 10")
    except tk.TclError:
        pass
    ConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
