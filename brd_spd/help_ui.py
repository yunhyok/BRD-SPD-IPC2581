"""Open the bundled, offline user guide from desktop Help menus."""
from pathlib import Path
import sys
import tkinter as tk
from tkinter import messagebox
import webbrowser


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
    try:
        url = help_url(topic)
        if not webbrowser.open(url, new=2):
            raise RuntimeError(f"기본 브라우저를 열지 못했습니다. 다음 파일을 직접 열어 주세요.\n{help_file()}")
    except (OSError, ValueError, RuntimeError, webbrowser.Error) as exc:
        messagebox.showerror("Help / 도움말", str(exc), parent=parent)


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
