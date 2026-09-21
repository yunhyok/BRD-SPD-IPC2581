import os
import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import brd_spd.process_target as process_target
from brd_spd.process_target import inspect_process, validate_target


@pytest.mark.skipif(os.name != "nt", reason="Windows process identity API")
def test_inspect_and_validate_current_python_process():
    identity = inspect_process(os.getpid())
    assert identity["pid"] == os.getpid()
    assert Path(identity["executable"]).is_absolute()
    assert identity["creation_time"] > 0
    assert validate_target(os.getpid(), Path(sys.executable), identity) == identity


@pytest.mark.parametrize("pid", [True, False, 0, -1, 0x100000000, "123"])
def test_invalid_pid_is_rejected(pid):
    error = TypeError if isinstance(pid, (bool, str)) else ValueError
    with pytest.raises(error):
        inspect_process(pid)


def test_fake_api_detects_dead_process_and_always_closes():
    class FakeAPI:
        closed = False

        def open(self, pid):
            return object()

        def executable(self, handle):
            return r"C:\Cadence\allegro.exe"

        def creation_time(self, handle):
            return 123

        def alive(self, handle):
            return False

        def close(self, handle):
            self.closed = True

    api = FakeAPI()
    with pytest.raises(ProcessLookupError, match="no longer running"):
        process_target._inspect_process_windows(12, api)
    assert api.closed


def test_validate_rejects_executable_mismatch_and_pid_reuse(tmp_path, monkeypatch):
    actual = tmp_path / "allegro.exe"
    other = tmp_path / "other.exe"
    actual.write_bytes(b"a")
    other.write_bytes(b"b")
    current = {"pid": 42, "executable": str(actual), "creation_time": 200}
    monkeypatch.setattr(process_target, "inspect_process", lambda pid: dict(current))

    with pytest.raises(RuntimeError, match="configured executable"):
        validate_target(42, other)
    stale = {"pid": 42, "executable": str(actual), "creation_time": 100}
    with pytest.raises(RuntimeError, match="PID may have been reused"):
        validate_target(42, actual, stale)


@pytest.mark.skipif(os.name != "nt", reason="Windows process identity API")
def test_nonexistent_process_is_rejected():
    with pytest.raises(ProcessLookupError):
        inspect_process(0xFFFFFFFF)


class FakeProcessAPI:
    def __init__(self, executable, creation_time=500):
        self.path = str(executable)
        self.created = creation_time
        self.closed = False

    def open(self, pid):
        return "process-handle"

    def executable(self, handle):
        assert handle == "process-handle"
        return self.path

    def creation_time(self, handle):
        return self.created

    def alive(self, handle):
        return True

    def close(self, handle):
        self.closed = True


class FakeWindowAPI:
    def __init__(self, windows, process_api, result=(True, False), final_pid=None):
        self._windows = windows
        self.process_api = process_api
        self.result = result
        self.final_pid = final_pid
        self.sent = []

    def windows(self):
        return list(self._windows)

    def window_pid(self, hwnd):
        if self.final_pid is not None:
            return self.final_pid
        return next(window["pid"] for window in self._windows if window["hwnd"] == hwnd)

    def send_copydata(self, hwnd, payload, timeout_ms):
        assert not self.process_api.closed, "process identity handle closed before dispatch"
        self.sent.append((hwnd, payload, timeout_ms))
        return self.result


def _identity(pid, executable, created=500):
    return {"pid": pid, "executable": str(executable), "creation_time": created}


def test_dispatch_selects_only_target_pid_and_builds_exact_payload(tmp_path):
    executable = tmp_path / "allegro.exe"
    executable.touch()
    process = FakeProcessAPI(executable)
    windows = FakeWindowAPI([
        {"hwnd": 10, "pid": 77, "visible": True, "owner": 0,
         "title": "Cadence Allegro PCB Editor"},
        {"hwnd": 11, "pid": 88, "visible": True, "owner": 0,
         "title": "Cadence Allegro PCB Editor"},
        {"hwnd": 12, "pid": 77, "visible": False, "owner": 0,
         "title": "Allegro hidden"},
        {"hwnd": 13, "pid": 77, "visible": True, "owner": 99,
         "title": "Allegro dialog"},
    ], process)

    result = process_target._dispatch_command_windows(
        77, executable, _identity(77, executable), "axlTest()",
        process_api=process, window_api=windows,
    )

    assert result == {"accepted": True, "timed_out": False, "hwnd": 10}
    assert windows.sent == [(10, b"axlTest()\n\0", 2000)]
    assert process.closed


def test_dispatch_refuses_ambiguous_target_windows(tmp_path):
    executable = tmp_path / "allegro.exe"
    executable.touch()
    process = FakeProcessAPI(executable)
    windows = FakeWindowAPI([
        {"hwnd": 1, "pid": 77, "visible": True, "owner": 0, "title": "Allegro A"},
        {"hwnd": 2, "pid": 77, "visible": True, "owner": 0, "title": "Allegro B"},
    ], process)
    with pytest.raises(RuntimeError, match="multiple eligible"):
        process_target._dispatch_command_windows(
            77, executable, _identity(77, executable), "axlTest()",
            process_api=process, window_api=windows,
        )
    assert not windows.sent
    assert process.closed


def test_dispatch_rechecks_process_and_window_immediately_before_send(tmp_path):
    executable = tmp_path / "allegro.exe"
    executable.touch()
    eligible = [
        {"hwnd": 1, "pid": 77, "visible": True, "owner": 0, "title": "Allegro"},
    ]

    process = FakeProcessAPI(executable)
    windows = FakeWindowAPI(eligible, process, final_pid=88)
    with pytest.raises(RuntimeError, match="window ownership changed"):
        process_target._dispatch_command_windows(
            77, executable, _identity(77, executable), "axlTest()",
            process_api=process, window_api=windows,
        )
    assert not windows.sent
    assert process.closed

    class ExitingProcess(FakeProcessAPI):
        def __init__(self, path):
            super().__init__(path)
            self.alive_checks = 0

        def alive(self, handle):
            self.alive_checks += 1
            return self.alive_checks == 1

    process = ExitingProcess(executable)
    windows = FakeWindowAPI(eligible, process)
    with pytest.raises(ProcessLookupError, match="exited before command dispatch"):
        process_target._dispatch_command_windows(
            77, executable, _identity(77, executable), "axlTest()",
            process_api=process, window_api=windows,
        )
    assert not windows.sent
    assert process.closed


def test_dispatch_refuses_reused_identity_and_executable_mismatch(tmp_path):
    executable = tmp_path / "allegro.exe"
    other = tmp_path / "other.exe"
    executable.touch()
    other.touch()
    process = FakeProcessAPI(executable, creation_time=501)
    windows = FakeWindowAPI([], process)
    with pytest.raises(RuntimeError, match="PID may have been reused"):
        process_target._dispatch_command_windows(
            77, executable, _identity(77, executable, 500), "axlTest()",
            process_api=process, window_api=windows,
        )

    process = FakeProcessAPI(executable)
    windows = FakeWindowAPI([], process)
    with pytest.raises(RuntimeError, match="configured executable"):
        process_target._dispatch_command_windows(
            77, other, _identity(77, executable), "axlTest()",
            process_api=process, window_api=windows,
        )


def test_dispatch_timeout_is_unknown_and_not_an_exception(tmp_path):
    executable = tmp_path / "allegro.exe"
    executable.touch()
    process = FakeProcessAPI(executable)
    windows = FakeWindowAPI([
        {"hwnd": 3, "pid": 77, "visible": True, "owner": 0, "title": "Allegro"},
    ], process, result=(False, True))
    result = process_target._dispatch_command_windows(
        77, executable, _identity(77, executable), "axlTest()", timeout_ms=25,
        process_api=process, window_api=windows,
    )
    assert result == {"accepted": False, "timed_out": True, "hwnd": 3}
    assert len(windows.sent) == 1


def test_dispatch_requires_prior_identity(tmp_path):
    with pytest.raises(ValueError, match="expected_identity is required"):
        process_target._dispatch_command_windows(
            77, tmp_path / "allegro.exe", None, "axlTest()",
            process_api=object(), window_api=object(),
        )


@pytest.mark.parametrize("command,error", [
    (None, TypeError),
    ("", ValueError),
    ("one\ntwo", ValueError),
    ("one\rtwo", ValueError),
    ("one\0two", ValueError),
    ("x" * 1023, ValueError),
])
def test_command_payload_rejects_unsafe_or_oversized_values(command, error):
    with pytest.raises(error):
        process_target._encode_command(command)


def test_command_payload_accepts_maximum_ansi_line():
    payload = process_target._encode_command("x" * 1022)
    assert len(payload) == 1024
    assert payload[-2:] == b"\n\0"


def test_dispatch_is_explicitly_windows_only(monkeypatch, tmp_path):
    monkeypatch.setattr(process_target.os, "name", "posix")
    with pytest.raises(OSError, match="only on Windows"):
        process_target.dispatch_command(1, tmp_path / "allegro.exe", {}, "axlTest()")


_WINDOW_RECEIVER = textwrap.dedent(r"""
    import ctypes
    import json
    import os
    import sys
    from ctypes import wintypes
    from pathlib import Path

    ready_path = Path(sys.argv[1])
    received_path = Path(sys.argv[2])
    WM_COPYDATA = 0x004A
    WM_DESTROY = 0x0002
    SW_SHOW = 5

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(
        LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    class COPYDATASTRUCT(ctypes.Structure):
        _fields_ = [
            ("dwData", ctypes.c_size_t),
            ("cbData", wintypes.DWORD),
            ("lpData", wintypes.LPVOID),
        ]

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    user32.DefWindowProcW.restype = LRESULT
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.UpdateWindow.argtypes = [wintypes.HWND]
    user32.UpdateWindow.restype = wintypes.BOOL
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    @WNDPROC
    def window_proc(hwnd, message, wparam, lparam):
        if message == WM_COPYDATA:
            frame = ctypes.cast(lparam, ctypes.POINTER(COPYDATASTRUCT)).contents
            payload = ctypes.string_at(frame.lpData, frame.cbData)
            received_path.write_text(json.dumps({
                "dwData": int(frame.dwData),
                "cbData": int(frame.cbData),
                "payload_hex": payload.hex(),
                "wparam": int(wparam),
            }), encoding="utf-8")
            user32.PostQuitMessage(0)
            return 0
        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    instance = kernel32.GetModuleHandleW(None)
    class_name = "SyntheticAllegroReceiver_" + str(os.getpid())
    window_class = WNDCLASSW()
    window_class.lpfnWndProc = window_proc
    window_class.hInstance = instance
    window_class.lpszClassName = class_name
    if not user32.RegisterClassW(ctypes.byref(window_class)):
        raise ctypes.WinError(ctypes.get_last_error())
    hwnd = user32.CreateWindowExW(
        0, class_name, "Synthetic Allegro Protocol Receiver", 0x00CF0000,
        20, 20, 320, 120, None, None, instance, None,
    )
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())
    user32.ShowWindow(hwnd, SW_SHOW)
    user32.UpdateWindow(hwnd)
    ready_path.write_text(str(int(hwnd)), encoding="ascii")
    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))
""")


def _wait_for_file(path, processes, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        for process in processes:
            if process.poll() is not None:
                stderr = process.stderr.read() if process.stderr else ""
                raise AssertionError(f"synthetic receiver exited early: {stderr}")
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path.name}")


def _stop_owned_process(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


@pytest.mark.skipif(os.name != "nt", reason="Windows WM_COPYDATA integration")
def test_real_windows_copydata_receiver_and_pid_isolation(tmp_path):
    target_ready = tmp_path / "target.ready"
    target_received = tmp_path / "target.json"
    decoy_ready = tmp_path / "decoy.ready"
    decoy_received = tmp_path / "decoy.json"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    target = subprocess.Popen(
        [sys.executable, "-c", _WINDOW_RECEIVER, str(target_ready), str(target_received)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, creationflags=flags,
    )
    decoy = subprocess.Popen(
        [sys.executable, "-c", _WINDOW_RECEIVER, str(decoy_ready), str(decoy_received)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, creationflags=flags,
    )
    try:
        _wait_for_file(target_ready, [target, decoy])
        _wait_for_file(decoy_ready, [target, decoy])
        identity = inspect_process(target.pid)
        result = process_target.dispatch_command(
            target.pid, Path(sys.executable), identity, "axlSyntheticReceiver()",
            timeout_ms=2000,
        )
        assert result == {
            "accepted": True,
            "timed_out": False,
            "hwnd": int(target_ready.read_text(encoding="ascii")),
        }
        _wait_for_file(target_received, [target, decoy])
        frame = json.loads(target_received.read_text(encoding="utf-8"))
        expected_payload = b"axlSyntheticReceiver()\n\0"
        assert frame == {
            "dwData": process_target.CADENCE_COPYDATA_ID,
            "cbData": len(expected_payload),
            "payload_hex": expected_payload.hex(),
            "wparam": 0,
        }
        time.sleep(0.1)
        assert not decoy_received.exists()
    finally:
        _stop_owned_process(target)
        _stop_owned_process(decoy)
