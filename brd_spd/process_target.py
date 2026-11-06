"""Read-only Windows process identity checks for a caller-selected PID."""
from __future__ import annotations

import os
import locale
from pathlib import Path
from typing import Any


MAX_DWORD = 0xFFFFFFFF
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
STILL_ACTIVE = 259
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF
WM_COPYDATA = 0x004A
CADENCE_COPYDATA_ID = 0x26297811
SMTO_BLOCK = 0x0001
SMTO_ABORTIFHUNG = 0x0002


class DispatchNotAttempted(RuntimeError):
    """A pre-dispatch check failed, so no command reached the target window.

    Callers must treat this as a plain failure: nothing was sent, therefore no
    native execution can be pending and no cancellation marker is required.
    """


def _validated_pid(pid: int) -> int:
    if isinstance(pid, bool) or not isinstance(pid, int):
        raise TypeError("pid must be an integer")
    if pid <= 0 or pid > MAX_DWORD:
        raise ValueError("pid must be between 1 and 4294967295")
    return pid


class _WindowsProcessAPI:
    """Small typed Kernel32 surface, kept injectable for deterministic tests."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        ]
        self.kernel32.GetProcessTimes.restype = wintypes.BOOL
        self.kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        self.kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL

    def _winerror(self) -> OSError:
        return self.ctypes.WinError(self.ctypes.get_last_error())

    def open(self, pid: int):
        rights = PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        handle = self.kernel32.OpenProcess(rights, False, pid)
        if handle:
            return handle
        error = self.ctypes.get_last_error()
        if error in {87, 1168}:  # ERROR_INVALID_PARAMETER / ERROR_NOT_FOUND
            raise ProcessLookupError(error, f"process {pid} does not exist")
        if error == 5:  # ERROR_ACCESS_DENIED
            raise PermissionError(error, f"access denied while inspecting process {pid}")
        raise self.ctypes.WinError(error)

    def executable(self, handle) -> str:
        # Windows documents 32,767 as the maximum extended path length.
        capacity = 32768
        buffer = self.ctypes.create_unicode_buffer(capacity)
        size = self.wintypes.DWORD(capacity)
        if not self.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, self.ctypes.byref(size)):
            raise self._winerror()
        return buffer.value

    def creation_time(self, handle) -> int:
        values = [self.wintypes.FILETIME() for _ in range(4)]
        if not self.kernel32.GetProcessTimes(handle, *(self.ctypes.byref(v) for v in values)):
            raise self._winerror()
        created = values[0]
        return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)

    def alive(self, handle) -> bool:
        exit_code = self.wintypes.DWORD()
        if not self.kernel32.GetExitCodeProcess(handle, self.ctypes.byref(exit_code)):
            raise self._winerror()
        wait = int(self.kernel32.WaitForSingleObject(handle, 0))
        if wait == WAIT_FAILED:
            raise self._winerror()
        if wait == WAIT_OBJECT_0:
            return False
        if wait != WAIT_TIMEOUT:
            raise OSError(f"unexpected WaitForSingleObject result: {wait}")
        return int(exit_code.value) == STILL_ACTIVE

    def close(self, handle) -> None:
        self.kernel32.CloseHandle(handle)


class _WindowsMessageAPI:
    """Typed User32 calls required for PID-specific WM_COPYDATA dispatch."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._enum_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        self.user32.EnumWindows.argtypes = [self._enum_type, wintypes.LPARAM]
        self.user32.EnumWindows.restype = wintypes.BOOL
        self.user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
        ]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetWindow.restype = wintypes.HWND
        self.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self.user32.GetWindowTextLengthW.restype = ctypes.c_int
        self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetWindowTextW.restype = ctypes.c_int
        self.user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
            wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
        ]
        self.user32.SendMessageTimeoutW.restype = wintypes.LPARAM

        class COPYDATASTRUCT(ctypes.Structure):
            _fields_ = [
                ("dwData", ctypes.c_size_t),
                ("cbData", wintypes.DWORD),
                ("lpData", wintypes.LPVOID),
            ]

        self.copydata_type = COPYDATASTRUCT

    def windows(self) -> list[dict[str, Any]]:
        windows: list[dict[str, Any]] = []
        failures: list[BaseException] = []

        @self._enum_type
        def callback(hwnd, _lparam):
            # ctypes swallows exceptions raised inside a callback, which would
            # stop enumeration early and return a silently partial window list.
            try:
                process_id = self.wintypes.DWORD()
                self.user32.GetWindowThreadProcessId(hwnd, self.ctypes.byref(process_id))
                title_length = self.user32.GetWindowTextLengthW(hwnd)
                title = ""
                if title_length > 0:
                    buffer = self.ctypes.create_unicode_buffer(title_length + 1)
                    self.user32.GetWindowTextW(hwnd, buffer, len(buffer))
                    title = buffer.value
                windows.append({
                    "hwnd": int(hwnd),
                    "pid": int(process_id.value),
                    "visible": bool(self.user32.IsWindowVisible(hwnd)),
                    "owner": int(self.user32.GetWindow(hwnd, 4) or 0),  # GW_OWNER
                    "title": title,
                })
            except BaseException as exc:
                failures.append(exc)
                return False
            return True

        self.ctypes.set_last_error(0)
        enumerated = self.user32.EnumWindows(callback, 0)
        if failures:
            raise failures[0]
        if not enumerated:
            error = self.ctypes.get_last_error()
            if error:
                raise self.ctypes.WinError(error)
        return windows

    def window_pid(self, hwnd: int) -> int:
        process_id = self.wintypes.DWORD()
        self.ctypes.set_last_error(0)
        thread_id = self.user32.GetWindowThreadProcessId(hwnd, self.ctypes.byref(process_id))
        if not thread_id:
            error = self.ctypes.get_last_error()
            if error:
                raise self.ctypes.WinError(error)
            raise RuntimeError("target window no longer exists")
        return int(process_id.value)

    def send_copydata(self, hwnd: int, payload: bytes, timeout_ms: int) -> tuple[bool, bool]:
        data = self.ctypes.create_string_buffer(payload)
        copydata = self.copydata_type(
            CADENCE_COPYDATA_ID, len(payload), self.ctypes.cast(data, self.wintypes.LPVOID)
        )
        receipt = self.ctypes.c_size_t()
        self.ctypes.set_last_error(0)
        delivered = self.user32.SendMessageTimeoutW(
            hwnd, WM_COPYDATA, 0, self.ctypes.addressof(copydata),
            SMTO_ABORTIFHUNG | SMTO_BLOCK, timeout_ms, self.ctypes.byref(receipt),
        )
        if delivered:
            # A completed dispatch is accepted regardless of the receiver's
            # LRESULT; Allegro's command completion is reported out of band.
            return True, False
        error = self.ctypes.get_last_error()
        if error in {0, 1460}:  # no diagnostic or ERROR_TIMEOUT
            return False, True
        raise self.ctypes.WinError(error)


def _identity_from_handle(pid: int, api, handle) -> dict[str, Any]:
    executable = os.path.abspath(api.executable(handle))
    creation_time = int(api.creation_time(handle))
    if creation_time <= 0:
        raise OSError("process returned an invalid creation time")
    if not api.alive(handle):
        raise ProcessLookupError(f"process {pid} is no longer running")
    return {"pid": pid, "executable": executable, "creation_time": creation_time}


def _inspect_process_windows(pid: int, api=None) -> dict[str, Any]:
    pid = _validated_pid(pid)
    api = api or _WindowsProcessAPI()
    handle = api.open(pid)
    try:
        return _identity_from_handle(pid, api, handle)
    finally:
        api.close(handle)


def inspect_process(pid: int) -> dict[str, Any]:
    """Return a stable identity for a live Windows process.

    The function only requests process query and synchronization rights. It
    does not read process memory, attach a debugger, send commands, or stop the
    target.
    """
    pid = _validated_pid(pid)
    if os.name != "nt":
        raise OSError("process target inspection is supported only on Windows")
    return _inspect_process_windows(pid)


def _normalized_path(path: Path | str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _same_executable(left: Path | str, right: Path | str) -> bool:
    try:
        return os.path.samefile(left, right)
    except (FileNotFoundError, OSError):
        return _normalized_path(left) == _normalized_path(right)


def _validate_identity(pid: int, expected_executable: Path,
                       expected_identity: dict[str, Any] | None,
                       current: dict[str, Any]) -> None:
    expected = Path(expected_executable).expanduser().resolve(strict=False)
    if not _same_executable(current["executable"], expected):
        raise RuntimeError(
            f"process {pid} executable does not match the configured executable"
        )

    if expected_identity is None:
        return
    if not isinstance(expected_identity, dict):
        raise TypeError("expected_identity must be a dictionary")
    required = {"pid", "executable", "creation_time"}
    if not required.issubset(expected_identity):
        raise ValueError("expected_identity must contain pid, executable, and creation_time")
    previous_pid = _validated_pid(expected_identity["pid"])
    try:
        if isinstance(expected_identity["creation_time"], bool):
            raise ValueError
        previous_creation = int(expected_identity["creation_time"])
    except (TypeError, ValueError) as exc:
        raise ValueError("expected_identity creation_time must be an integer") from exc
    if (previous_pid != pid or previous_creation != current["creation_time"] or
            not _same_executable(expected_identity["executable"], current["executable"])):
        raise RuntimeError("process identity changed; the PID may have been reused")


def validate_target(pid: int, expected_executable: Path,
                    expected_identity: dict[str, Any] | None = None) -> dict[str, Any]:
    """Verify a live PID, executable path, and optional prior identity.

    Comparing the creation timestamp as well as the PID prevents a stale GUI
    selection from silently targeting a different process after PID reuse.
    """
    pid = _validated_pid(pid)
    current = inspect_process(pid)
    _validate_identity(pid, expected_executable, expected_identity, current)
    return current


def _encode_command(command: str) -> bytes:
    if not isinstance(command, str):
        raise TypeError("command must be a string")
    if not command:
        raise ValueError("command must not be empty")
    if "\0" in command or "\r" in command or "\n" in command:
        raise ValueError("command must be one line without NUL, CR, or LF")
    encoding = "mbcs" if os.name == "nt" else locale.getpreferredencoding(False)
    encoded = (command + "\n").encode(encoding, errors="strict")
    if len(encoded) > 1023:
        raise ValueError("encoded command plus newline must be at most 1023 bytes")
    return encoded + b"\0"


def _dispatch_command_windows(pid: int, expected_executable: Path,
                              expected_identity: dict[str, Any], command: str,
                              timeout_ms: int = 2000, process_api=None,
                              window_api=None) -> dict[str, Any]:
    handle = None
    try:
        # Every check below runs before a single byte is sent.  Failures here
        # are reported as DispatchNotAttempted so callers can record a plain
        # failure instead of an unobserved native execution.
        try:
            pid = _validated_pid(pid)
            if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
                raise TypeError("timeout_ms must be an integer")
            if timeout_ms <= 0 or timeout_ms > MAX_DWORD:
                raise ValueError("timeout_ms must be between 1 and 4294967295")
            if expected_identity is None:
                raise ValueError("expected_identity is required for PID reuse protection")
            payload = _encode_command(command)
            process_api = process_api or _WindowsProcessAPI()
            window_api = window_api or _WindowsMessageAPI()
            handle = process_api.open(pid)
            current = _identity_from_handle(pid, process_api, handle)
            _validate_identity(pid, expected_executable, expected_identity, current)
            matches = [
                window for window in window_api.windows()
                if window["pid"] == pid and window["visible"] and not window["owner"]
                and "allegro" in str(window["title"]).casefold()
            ]
            if not matches:
                raise DispatchNotAttempted(f"no eligible Allegro window belongs to process {pid}")
            if len(matches) != 1:
                raise DispatchNotAttempted(f"multiple eligible Allegro windows belong to process {pid}")
            hwnd = int(matches[0]["hwnd"])
            if window_api.window_pid(hwnd) != pid:
                raise DispatchNotAttempted("target window ownership changed before dispatch")
            if not process_api.alive(handle):
                raise DispatchNotAttempted(f"process {pid} exited before command dispatch")
        except DispatchNotAttempted:
            raise
        except BaseException as exc:
            raise DispatchNotAttempted(str(exc) or type(exc).__name__) from exc
        accepted, timed_out = window_api.send_copydata(hwnd, payload, timeout_ms)
        return {"accepted": bool(accepted), "timed_out": bool(timed_out), "hwnd": hwnd}
    finally:
        if handle is not None:
            process_api.close(handle)


def dispatch_command(pid: int, expected_executable: Path,
                     expected_identity: dict[str, Any], command: str,
                     timeout_ms: int = 2000) -> dict[str, Any]:
    """Send one bounded Cadence command to exactly one PID-owned Allegro window.

    A timeout means delivery is unknown; callers must poll the native result
    marker and must not retry the command automatically.  Any failure raised
    before the command leaves this process is a :class:`DispatchNotAttempted`.
    """
    if os.name != "nt":
        raise OSError("Cadence command dispatch is supported only on Windows")
    return _dispatch_command_windows(
        pid, expected_executable, expected_identity, command, timeout_ms
    )
