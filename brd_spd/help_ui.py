"""Open the bundled, offline user guide from desktop Help menus."""
from pathlib import Path
import sys
import tkinter as tk
from tkinter import messagebox, ttk
from urllib.parse import urldefrag


HELP_TOPICS = (
    ("사용 안내", "overview"),
    ("작업 방식 선택", "choose-workflow"),
    ("Native SKILL 생성", "native-skill"),
    ("대상 레이어·NET 선택", "target-selection"),
    ("워크스테이션 Agent 설정", "workstation"),
    ("원격 작업: 새 Allegro 실행", "remote-new"),
    ("원격 작업: 기존 PID 지정", "remote-pid"),
    ("결과·로그 확인", "results"),
    ("문제 해결", "troubleshooting"),
    ("지원 범위", "limitations"),
)


def help_file() -> Path:
    relative = Path("docs") / "help" / "index.html"
    if getattr(sys, "frozen", False):
        roots = [Path(sys.executable).resolve().parent]
        if getattr(sys, "_MEIPASS", None):
            roots.append(Path(sys._MEIPASS))
    else:
        roots = [Path(__file__).resolve().parent.parent]
    for root in roots:
        path = root / relative
        if path.is_file():
            return path
    raise FileNotFoundError("도움말 파일을 찾을 수 없습니다. 프로그램을 다시 설치해 주세요.")


def help_url(topic: str = "overview") -> str:
    if topic not in {anchor for _label, anchor in HELP_TOPICS}:
        raise ValueError("알 수 없는 도움말 항목입니다.")
    return help_file().resolve().as_uri() + "#" + topic


def open_help(parent, topic: str = "overview") -> None:
    root = parent.winfo_toplevel()
    viewer = getattr(root, "_help_viewer", None)
    try:
        help_url(topic)  # Validate the topic and bundled file before creating a window.
        if viewer is None or not viewer.winfo_exists():
            viewer = HelpViewer(root)
            root._help_viewer = viewer
        viewer.show_topic(topic)
        viewer.deiconify()
        viewer.lift()
        viewer.contents.focus_set()
    except (OSError, ValueError, RuntimeError, ImportError, tk.TclError) as exc:
        messagebox.showerror("Help / 도움말", str(exc), parent=parent)


class HelpViewer(tk.Toplevel):
    """Render the bundled HTML locally with Tkhtml, without a browser runtime."""

    def __init__(self, parent):
        # Import before creating the window so a missing engine leaves no blank window.
        from tkinterweb import HtmlFrame

        document_path = help_file()
        source = document_path.read_text(encoding="utf-8")
        super().__init__(parent)
        self.title("BRD-SPD-IPC2581 도움말")
        self.geometry("980x760")
        self.minsize(640, 420)
        self.transient(parent)
        self.topic = "overview"
        self.scale = 1.0
        self._base_url = document_path.resolve().as_uri()
        try:
            toolbar = ttk.Frame(self, padding=8)
            toolbar.pack(fill="x")
            ttk.Label(toolbar, text="목차").pack(side="left", padx=(0, 6))
            self.contents = ttk.Combobox(toolbar, values=[label for label, _ in HELP_TOPICS], state="readonly", width=29)
            self.contents.pack(side="left", fill="x", expand=True, padx=(0, 10))
            self.contents.bind("<<ComboboxSelected>>", lambda _event: self.show_topic(HELP_TOPICS[self.contents.current()][1]))
            self.smaller = ttk.Button(toolbar, text="글자 축소", command=lambda: self.change_scale(-0.1))
            self.smaller.pack(side="left", padx=3)
            self.larger = ttk.Button(toolbar, text="글자 확대", command=lambda: self.change_scale(0.1))
            self.larger.pack(side="left", padx=3)
            ttk.Button(toolbar, text="닫기", command=self.destroy).pack(side="right", padx=(8, 0))
            self.html = HtmlFrame(
                self, messages_enabled=False, javascript_enabled=False,
                horizontal_scrollbar="auto",
                objects_enabled=False, forms_enabled=False, images_enabled=False,
                threading_enabled=False, on_link_click=self.follow_link,
                request_func=self._deny_resource,
            )
            self.html.pack(fill="both", expand=True)
            self.html.load_html(source, base_url=self._base_url)
            self.bind("<Escape>", lambda _event: self.destroy())
        except Exception:
            self.destroy()
            raise

    @staticmethod
    def _deny_resource(*_args, **_kwargs):
        raise OSError("내장 도움말은 외부 문서나 네트워크 리소스를 불러오지 않습니다.")

    def show_topic(self, topic):
        topics = [anchor for _, anchor in HELP_TOPICS]
        if topic not in topics:
            raise ValueError("알 수 없는 도움말 항목입니다.")
        element = self.html.document.getElementById(topic)
        if element is None:
            raise ValueError("도움말 항목을 찾을 수 없습니다.")
        self.topic = topic
        self.contents.current(topics.index(topic))
        self.update_idletasks()
        element.scrollIntoView()

    def follow_link(self, url):
        base, fragment = urldefrag(url)
        if base not in ("", self._base_url) or fragment not in {anchor for _, anchor in HELP_TOPICS}:
            messagebox.showinfo("Help / 도움말", "내장 도움말의 목차 링크만 열 수 있습니다.", parent=self)
            return
        self.show_topic(fragment)

    def change_scale(self, delta):
        self.scale = round(min(1.6, max(0.8, self.scale + delta)), 1)
        self.html.configure(fontscale=self.scale)
        self.smaller.configure(state="disabled" if self.scale <= 0.8 else "normal")
        self.larger.configure(state="disabled" if self.scale >= 1.6 else "normal")
        self.show_topic(self.topic)


def install_help_menu(root, context_topic=None):
    bar = tk.Menu(root)
    help_menu = tk.Menu(bar, tearoff=False)
    for label, topic in HELP_TOPICS:
        help_menu.add_command(label=label, command=lambda topic=topic: open_help(root, topic))
    bar.add_cascade(label="Help / 도움말", menu=help_menu)
    root.configure(menu=bar)

    def context_help(_event):
        open_help(root, context_topic() if context_topic else "overview")
        return "break"

    root.bind("<F1>", context_help)
    return bar, help_menu
