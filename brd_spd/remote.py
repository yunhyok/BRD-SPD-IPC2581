"""Small authenticated HTTPS service for running native Allegro conversion jobs.

The protocol deliberately exposes jobs and fixed upload slots rather than a
remote shell.  Inputs always live below the agent data directory and Allegro is
started with a fixed argument list.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import queue
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import uuid
import warnings
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit


PROTOCOL_VERSION = 1
DEFAULT_MAX_UPLOAD = 10 * 1024**3
MAX_METADATA = 64 * 1024
_JOB_STATES = {
    "created", "queued", "running", "succeeded", "failed",
    "cancel_requested", "cancelled", "interrupted",
}
_TERMINAL_STATES = {"succeeded", "failed", "cancelled", "interrupted"}
_SLOTS = {"spd": "input.spd", "brd": "base.brd"}


class _Cancelled(RuntimeError):
    pass


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _job_id(value: str) -> str:
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("invalid job id") from exc
    if value != parsed:
        raise ValueError("invalid job id")
    return value


def _safe_options(options: Any) -> dict[str, Any]:
    if not isinstance(options, dict):
        raise ValueError("options must be an object")
    unknown = set(options) - {"layers", "nets", "update_components", "allegro_pid", "allegro_creation_time"}
    if unknown:
        raise ValueError("unsupported option(s): " + ", ".join(sorted(unknown)))
    result: dict[str, Any] = {}
    for key in ("layers", "nets"):
        if key not in options or options[key] is None:
            continue
        values = options[key]
        if (not isinstance(values, list) or len(values) > 100_000 or
                any(not isinstance(v, str) or not v or len(v) > 1024 for v in values)):
            raise ValueError(f"{key} must be a bounded list of nonempty strings")
        result[key] = values
    if "update_components" in options:
        if not isinstance(options["update_components"], bool):
            raise ValueError("update_components must be boolean")
        result["update_components"] = options["update_components"]
    if "allegro_pid" in options:
        pid = options["allegro_pid"]
        if type(pid) is not int or not 1 <= pid <= 0xFFFFFFFF:
            raise ValueError("allegro_pid must be an integer between 1 and 4294967295")
        result["allegro_pid"] = pid
    if "allegro_creation_time" in options:
        created = options["allegro_creation_time"]
        if "allegro_pid" not in result or type(created) is not int or not 0 < created < 2**64:
            raise ValueError("allegro_creation_time requires allegro_pid and a positive FILETIME integer")
        result["allegro_creation_time"] = created
    return result


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _fingerprint(certificate_der: bytes) -> str:
    return hashlib.sha256(certificate_der).hexdigest()


def _create_certificate(datadir: Path, host: str) -> tuple[Path, Path, str]:
    """Create or load the agent's persistent self-signed TLS identity."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    cert_path, key_path = datadir / "agent-cert.pem", datadir / "agent-key.pem"
    if not cert_path.exists() or not key_path.exists():
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "BRD-SPD workstation agent")])
        sans: list[x509.GeneralName] = [x509.DNSName("localhost")]
        for value in {host, "127.0.0.1", "::1", socket.gethostname()}:
            if value in {"0.0.0.0", "::", ""}:
                continue
            try:
                sans.append(x509.IPAddress(ipaddress.ip_address(value)))
            except ValueError:
                sans.append(x509.DNSName(value))
        now = datetime.now(timezone.utc)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )
        # Create the private key with owner-only permissions from the start so
        # it is never briefly readable by other local accounts.
        descriptor = os.open(key_path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        try:
            key_path.chmod(0o600)
        except OSError:
            pass
    certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return cert_path, key_path, _fingerprint(certificate.public_bytes(serialization.Encoding.DER))


@dataclass
class AgentConfig:
    datadir: Path
    host: str = "0.0.0.0"
    port: int = 8765
    allegro_exe: Path | None = None
    allowed_clients: list[str] | None = None
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD
    session_timeout_seconds: float = 6 * 60 * 60

    def __post_init__(self) -> None:
        self.datadir = Path(self.datadir).resolve()
        if self.allegro_exe is not None:
            self.allegro_exe = Path(self.allegro_exe).resolve()
        if not 0 <= self.port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if self.max_upload_bytes <= 0:
            raise ValueError("max_upload_bytes must be positive")
        if not 0 < self.session_timeout_seconds <= 7 * 24 * 60 * 60:
            raise ValueError("session_timeout_seconds must be positive and at most seven days")


class _AgentServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, agent):
        self.agent = agent
        self.tls_context: ssl.SSLContext | None = None
        super().__init__(address, handler)

    def get_request(self):
        """Leave TLS negotiation to the per-connection handler thread.

        Wrapping the listening socket makes ``accept()`` perform the handshake
        on the server thread.  One idle peer could then prevent every other
        client from reaching the threaded request handler.
        """
        client, address = super().get_request()
        client.settimeout(30)
        try:
            if self.tls_context is None:
                raise RuntimeError("TLS context is not configured")
            wrapped = self.tls_context.wrap_socket(
                client, server_side=True, do_handshake_on_connect=False
            )
            wrapped.settimeout(30)
            return wrapped, address
        except BaseException:
            client.close()
            raise


class WorkstationAgent:
    """Persistent single-runner HTTPS agent for a licensed workstation."""

    def __init__(self, config: AgentConfig):
        self.config = config
        self.config.datadir.mkdir(parents=True, exist_ok=True)
        self._jobs_dir = self.config.datadir / "jobs"
        self._jobs_dir.mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._cancel: dict[str, threading.Event] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._active_uploads: set[tuple[str, str]] = set()
        self._reconciling: set[str] = set()
        self._server: _AgentServer | None = None
        self._server_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._instance_lock = None
        self._stopping = threading.Event()
        self._started = False
        # Serialize first-run token/certificate creation as well as execution.
        self._acquire_instance_lock()
        try:
            self._token = self._load_token()
            self._cert_path, self._key_path, self._fingerprint = _create_certificate(
                self.config.datadir, self.config.host
            )
        finally:
            self._release_instance_lock()
        self._allowed = self._parse_allowed(self.config.allowed_clients)

    def _load_token(self) -> str:
        path = self.config.datadir / "token.txt"
        if path.exists():
            token = path.read_text(encoding="ascii").strip()
            if len(token) < 32:
                raise ValueError("existing token.txt is too short")
            return token
        token = secrets.token_urlsafe(32)
        # Create with owner-only permissions instead of tightening them after
        # the secret is already on disk.
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(token + "\n")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return token

    @staticmethod
    def _parse_allowed(values: list[str] | None):
        if values is None:
            return None
        networks = []
        for value in values:
            networks.append(ipaddress.ip_network(value, strict=False))
        return networks

    @property
    def info(self) -> dict[str, Any]:
        port = self._server.server_address[1] if self._server else self.config.port
        return {
            "host": self.config.host,
            "port": port,
            "token": self._token,
            "token_file": str((self.config.datadir / "token.txt").resolve()),
            "fingerprint": self._fingerprint,
            "protocol": PROTOCOL_VERSION,
            "tls": True,
        }

    def _acquire_instance_lock(self) -> None:
        if self._instance_lock is not None:
            return
        path = self.config.datadir / "agent.lock"
        stream = path.open("a+b")
        try:
            if path.stat().st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            stream.close()
            raise RuntimeError(
                f"another workstation agent is already using {self.config.datadir}"
            ) from exc
        self._instance_lock = stream

    def _release_instance_lock(self) -> None:
        stream, self._instance_lock = self._instance_lock, None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._started:
                return self.info
            self._acquire_instance_lock()
            server = None
            try:
                self._recover()
                server = _AgentServer((self.config.host, self.config.port), _Handler, self)
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.load_cert_chain(self._cert_path, self._key_path)
                server.tls_context = context
                self._server = server
                self._stopping.clear()
                self._server_thread = threading.Thread(target=server.serve_forever, name="spd-agent-https", daemon=True)
                self._worker_thread = threading.Thread(target=self._worker, name="spd-agent-runner", daemon=True)
                self._server_thread.start()
                self._worker_thread.start()
                self._started = True
                return self.info
            except BaseException:
                if server is not None:
                    server.server_close()
                self._server = None
                self._server_thread = None
                self._worker_thread = None
                self._release_instance_lock()
                raise

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._stopping.set()
            for job_id, event in list(self._cancel.items()):
                event.set()
                try:
                    self._signal_session_cancel(job_id)
                except (OSError, ValueError):
                    # A removed or unreadable job directory must not keep the
                    # agent running.
                    pass
            for process in list(self._processes.values()):
                self._terminate(process)
            server = self._server
            self._queue.put(None)
        if server:
            server.shutdown()
            server.server_close()
        if self._server_thread:
            self._server_thread.join(timeout=5)
        if self._worker_thread:
            self._worker_thread.join(timeout=15)
        with self._lock:
            if self._worker_thread and self._worker_thread.is_alive():
                # Never leave the agent half-stopped: the daemon runner keeps
                # awaiting cooperative cancellation while the server, the state
                # flag and the data directory lock are released regardless.
                warnings.warn(
                    "agent runner is still awaiting cooperative cancellation; "
                    "an existing Allegro session will not be terminated",
                    RuntimeWarning, stacklevel=2,
                )
            self._server = None
            self._server_thread = None
            self._worker_thread = None
            self._queue = queue.Queue()
            self._cancel.clear()
            self._processes.clear()
            self._active_uploads.clear()
            self._started = False
            self._release_instance_lock()

    def _recover(self) -> None:
        for directory in self._jobs_dir.iterdir():
            state_path = directory / "job.json"
            if not directory.is_dir() or not state_path.is_file():
                continue
            try:
                job_id = _job_id(directory.name)
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (ValueError, OSError, json.JSONDecodeError):
                continue
            if state.get("status") in {"running", "cancel_requested"}:
                if state.get("session_dispatched"):
                    # Resume observation, never send the command a second time.
                    event = self._cancel.setdefault(job_id, threading.Event())
                    if state["status"] == "cancel_requested":
                        event.set()
                    self._update(job_id, status="queued")
                    self._queue.put(job_id)
                    continue
                self._finish(job_id, directory / "bundle", "interrupted", include_board=False,
                             error="agent restarted during execution")
            elif state.get("status") == "queued":
                self._queue.put(job_id)

    def _job_dir(self, job_id: str) -> Path:
        return self._jobs_dir / _job_id(job_id)

    def _state(self, job_id: str) -> dict[str, Any]:
        path = self._job_dir(job_id) / "job.json"
        # Windows can reject reads while another thread replaces this file.
        # Use the same lock as state writers for the short filesystem access.
        with self._lock:
            if not path.is_file():
                raise FileNotFoundError("job not found")
            state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("status") not in _JOB_STATES:
            raise ValueError("invalid persisted job state")
        return state

    def _update(self, job_id: str, **updates) -> dict[str, Any]:
        with self._lock:
            state = self._state(job_id)
            state.update(updates, updated_utc=_utcnow())
            _atomic_json(self._job_dir(job_id) / "job.json", state)
            return state

    @staticmethod
    def _public(state: dict[str, Any]) -> dict[str, Any]:
        """Return a copy so callers never mutate a persisted state dictionary."""
        return dict(state)

    @property
    def running_job_ids(self) -> list[str]:
        """Ids of persisted jobs that are queued, running, or cancel_requested.

        Read-only and cheap: it reads the persisted state under the same lock as
        the state writers and never reconciles, archives, or dispatches.
        """
        active: list[str] = []
        with self._lock:
            for directory in sorted(self._jobs_dir.iterdir()):
                try:
                    job_id = _job_id(directory.name)
                    state = json.loads((directory / "job.json").read_text(encoding="utf-8"))
                except (ValueError, OSError, json.JSONDecodeError):
                    continue
                if isinstance(state, dict) and state.get("status") in {
                        "queued", "running", "cancel_requested"}:
                    active.append(job_id)
        return active

    def create_job(self, options: Any) -> dict[str, Any]:
        options = _safe_options(options)
        target = self.check_pid(options["allegro_pid"]) if "allegro_pid" in options else None
        if target is not None and options.get("allegro_creation_time", target["creation_time"]) != target["creation_time"]:
            raise ValueError("Allegro PID was reused since validation; check the intended session again")
        job_id = str(uuid.uuid4())
        directory = self._jobs_dir / job_id
        directory.mkdir()
        now = _utcnow()
        state = {
            "id": job_id, "status": "created", "options": options,
            "uploads": {"spd": False, "brd": False},
            "created_utc": now, "updated_utc": now,
        }
        if target is not None:
            state["target_process"] = target
        _atomic_json(directory / "job.json", state)
        (directory / "runner.log").touch()
        return self._public(state)

    def _allegro_executable(self) -> Path:
        return self.config.allegro_exe or Path(r"C:\Cadence\SPB_24.1\tools\bin\allegro.exe")

    def check_pid(self, pid: int, expected_identity: dict | None = None) -> dict:
        from .process_target import validate_target
        executable = self._allegro_executable()
        if executable.name.lower() != "allegro.exe":
            raise ValueError("PID mode requires the configured allegro.exe executable")
        return validate_target(pid, executable, expected_identity)

    def upload(self, job_id: str, slot: str, source, size: int) -> dict[str, Any]:
        if slot not in _SLOTS:
            raise ValueError("invalid upload slot")
        if size <= 0 or size > self.config.max_upload_bytes:
            raise ValueError("upload exceeds configured size limit")
        active = (job_id, slot)
        with self._lock:
            state = self._state(job_id)
            if state["status"] != "created":
                raise RuntimeError("uploads are accepted only while a job is created")
            if slot == "brd" and "allegro_pid" in state["options"]:
                raise ValueError("PID mode uses the BRD already open in Allegro; do not upload a base BRD")
            if active in self._active_uploads:
                raise RuntimeError("an upload for this slot is already active")
            self._active_uploads.add(active)
        directory = self._job_dir(job_id)
        destination = directory / _SLOTS[slot]
        temporary = destination.with_suffix(destination.suffix + ".upload")
        received = 0
        try:
            with temporary.open("wb") as output:
                while received < size:
                    block = source.read(min(1024 * 1024, size - received))
                    if not block:
                        raise ConnectionError("upload ended before Content-Length")
                    output.write(block)
                    received += len(block)
            with self._lock:
                state = self._state(job_id)
                if state["status"] != "created":
                    raise RuntimeError("job changed state during upload")
                os.replace(temporary, destination)
                uploads = dict(state["uploads"])
                uploads[slot] = True
                return self._public(self._update(job_id, uploads=uploads))
        finally:
            temporary.unlink(missing_ok=True)
            with self._lock:
                self._active_uploads.discard(active)

    def submit(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._state(job_id)
            if state["status"] != "created":
                raise RuntimeError("job is not in created state")
            if any(active_job == job_id for active_job, _ in self._active_uploads):
                raise RuntimeError("job has an active upload")
            required = ("spd",) if "allegro_pid" in state["options"] else _SLOTS
            if not all(state["uploads"].get(slot) for slot in required):
                raise RuntimeError("required uploads are missing: " + ", ".join(required))
            if "allegro_pid" in state["options"]:
                self.check_pid(state["options"]["allegro_pid"], state["target_process"])
            state = self._update(job_id, status="queued")
            self._cancel[job_id] = threading.Event()
            self._queue.put(job_id)
            return self._public(state)

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._state(job_id)
            if state["status"] in _TERMINAL_STATES:
                return self._public(state)
            event = self._cancel.setdefault(job_id, threading.Event())
            event.set()
            self._signal_session_cancel(job_id)
            if state["status"] in {"created", "queued"} and not state.get("session_dispatched"):
                self._update(job_id, status="cancel_requested")
                self._log(job_id, "job cancelled before native execution")
                return self._finish(job_id, self._job_dir(job_id) / "bundle", "cancelled",
                                    include_board=False)
            state = self._update(job_id, status="cancel_requested")
            process = self._processes.get(job_id)
        if process is not None:
            self._terminate(process)
        return self._public(state)

    def _signal_session_cancel(self, job_id: str) -> None:
        bundle = self._job_dir(job_id) / "bundle"
        if "allegro_pid" in self._state(job_id)["options"] and bundle.is_dir():
            (bundle / "cancel.flag").touch()

    def _log(self, job_id: str, message: str) -> None:
        line = f"{_utcnow()} {message.rstrip()}\n"
        with (self._job_dir(job_id) / "runner.log").open("a", encoding="utf-8", errors="replace") as stream:
            stream.write(line)

    def _worker(self) -> None:
        while not self._stopping.is_set():
            job_id = self._queue.get()
            if job_id is None:
                return
            try:
                state = self._state(job_id)
                if state["status"] != "queued" and not (
                        state["status"] == "cancel_requested" and state.get("session_dispatched")):
                    continue
                self._run(job_id)
            except _Cancelled:
                self._log(job_id, "job cancelled before native execution")
                directory = self._job_dir(job_id)
                self._finish(job_id, directory / "bundle", "cancelled", include_board=False)
            except BaseException as exc:
                try:
                    self._log(job_id, f"agent failure: {exc}")
                    directory = self._job_dir(job_id)
                    dispatched = self._state(job_id).get("session_dispatched", False)
                    if dispatched:
                        self._signal_session_cancel(job_id)
                    self._finish(job_id, directory / "bundle", "interrupted" if dispatched else "failed",
                                 include_board=False, honor_cancel=not dispatched, error=str(exc),
                                 native_pending=dispatched, design_modified=None)
                except BaseException:
                    pass

    def _generate_bundle(self, source: Path, bundle: Path, options: dict[str, Any]) -> Any:
        from .skill import generate_bundle
        job_id = bundle.parent.name
        event = self._cancel.setdefault(job_id, threading.Event())

        def progress(message: str) -> None:
            self._log(job_id, str(message))
            if event.is_set() or self._stopping.is_set():
                raise _Cancelled("bundle generation cancelled")

        return generate_bundle(
            source, bundle,
            layers=options.get("layers"),
            nets=options.get("nets"),
            update_components=options.get("update_components", False),
            session_mode="allegro_pid" in options,
            progress=progress,
        )

    def _allegro_command(self, bundle: Path) -> list[str]:
        executable = self._allegro_executable()
        executable = Path(executable).resolve()
        if not executable.is_file():
            raise FileNotFoundError(f"Allegro executable not found: {executable}")
        if not (bundle / "run.scr").is_file():
            raise FileNotFoundError("native bundle did not create run.scr")
        return [str(executable), "-nograph", "-s", "run.scr", "base.brd"]

    def _start_process(self, command: list[str], bundle: Path, log_stream):
        environment = dict(os.environ)
        environment["PATH"] = str(Path(command[0]).parent) + os.pathsep + environment.get("PATH", "")
        return subprocess.Popen(
            command, cwd=bundle, env=environment,
            stdout=log_stream, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=15, check=False, creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except subprocess.TimeoutExpired:
                pass
            try:
                process.wait(timeout=5)
                return
            except subprocess.TimeoutExpired:
                pass
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _run(self, job_id: str) -> None:
        directory = self._job_dir(job_id)
        bundle = directory / "bundle"
        event = self._cancel.setdefault(job_id, threading.Event())
        with self._lock:
            # A cancel may have published a terminal state after the worker
            # dequeued this job; a terminal job is never resurrected.
            if self._state(job_id)["status"] in _TERMINAL_STATES:
                return
            state = self._update(job_id, status="running", started_utc=_utcnow())
        if state.get("session_dispatched"):
            self._log(job_id, "resuming existing-session observation without re-dispatch")
            self._monitor_session(job_id, bundle, state)
            return
        self._log(job_id, "generating constrained native bundle")
        if bundle.exists():
            raise RuntimeError("job bundle already exists; jobs cannot be retried in place")
        state = self._state(job_id)
        generation = self._generate_bundle(directory / "input.spd", bundle, state["options"])
        if isinstance(generation, dict) and (
                generation.get("status") == "failed" or generation.get("success") is False):
            raise RuntimeError(f"native bundle generation failed: {generation.get('error', 'unknown error')}")
        if event.is_set() or self._stopping.is_set():
            self._log(job_id, "job cancelled before native execution")
            self._finish(job_id, bundle, "cancelled", include_board=False)
            return
        if "allegro_pid" in state["options"]:
            self._run_session(job_id, bundle, state)
            return
        shutil.copy2(directory / "base.brd", bundle / "base.brd")
        command = self._allegro_command(bundle)
        self._log(job_id, "starting Allegro: " + " ".join(Path(x).name if i == 0 else x for i, x in enumerate(command)))
        log_path = directory / "runner.log"
        with log_path.open("ab", buffering=0) as log_stream:
            with self._lock:
                if event.is_set() or self._stopping.is_set():
                    raise _Cancelled("cancelled before Allegro launch")
                process = self._start_process(command, bundle, log_stream)
                self._processes[job_id] = process
            native_offset = 0
            while (return_code := process.poll()) is None:
                native_offset = self._copy_native_log(bundle / "execution.log", log_stream, native_offset)
                if event.is_set() or self._stopping.is_set():
                    self._terminate(process)
                time.sleep(0.2)
            self._copy_native_log(bundle / "execution.log", log_stream, native_offset)
            with self._lock:
                self._processes.pop(job_id, None)
        self._update(job_id, return_code=return_code)
        if event.is_set():
            self._log(job_id, "native execution cancelled")
            self._finish(job_id, bundle, "cancelled", include_board=False,
                         return_code=return_code)
            return
        result_path, board_path = bundle / "result.json", bundle / "result.brd"
        if not result_path.is_file():
            raise RuntimeError("native runner did not create result.json")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        native_success = result.get("status") == "success" or result.get("success") is True
        if return_code != 0 or not native_success or not board_path.is_file() or board_path.stat().st_size == 0:
            raise RuntimeError(
                f"native run failed: return_code={return_code}, status={result.get('status')!r}, "
                f"result_brd={board_path.is_file() and board_path.stat().st_size > 0}"
            )
        self._log(job_id, "native conversion succeeded")
        self._finish(job_id, bundle, "succeeded", include_board=True,
                     return_code=return_code, result_bytes=board_path.stat().st_size)

    def _dispatch_session(self, state: dict, command: str) -> dict:
        from .process_target import dispatch_command
        return dispatch_command(state["options"]["allegro_pid"], self._allegro_executable(),
                                state["target_process"], command)

    def _run_session(self, job_id: str, bundle: Path, state: dict) -> None:
        from .process_target import DispatchNotAttempted
        from .skill import _skill_string
        pid = state["options"]["allegro_pid"]
        self.check_pid(pid, state["target_process"])
        if not (bundle / "design.il").is_file():
            raise FileNotFoundError("native bundle did not create design.il")
        command = "skill load(" + _skill_string((bundle / "design.il").as_posix()) + ")"
        with self._lock:
            if self._cancel[job_id].is_set() or self._stopping.is_set():
                raise _Cancelled("cancelled before dispatch")
            self._update(job_id, session_dispatched=True)
        self._log(job_id, f"dispatching to existing Allegro PID {pid}; using its currently open BRD")
        try:
            delivery = self._dispatch_session(state, command)
        except DispatchNotAttempted:
            # No command left this process, so the job is a plain failure
            # rather than an unobserved native execution.
            self._update(job_id, session_dispatched=False)
            raise
        self._update(job_id, delivery=delivery)
        if delivery["timed_out"]:
            self._log(job_id, "message delivery timed out; awaiting native markers without resending")
        self._monitor_session(job_id, bundle, state)

    def _monitor_session(self, job_id: str, bundle: Path, state: dict) -> None:
        event = self._cancel.setdefault(job_id, threading.Event())
        deadline = time.monotonic() + 60
        completion_deadline = time.monotonic() + self.config.session_timeout_seconds
        cancellation_deadline = None
        next_identity_check = 0.0
        native_offset = 0
        with (self._job_dir(job_id) / "runner.log").open("ab", buffering=0) as log_stream:
            while not (bundle / "finished.flag").is_file():
                native_offset = self._copy_native_log(bundle / "execution.log", log_stream, native_offset)
                if event.is_set() or self._stopping.is_set():
                    self._signal_session_cancel(job_id)
                    if cancellation_deadline is None:
                        cancellation_deadline = time.monotonic() + 10
                if time.monotonic() >= next_identity_check:
                    try:
                        self.check_pid(state["options"]["allegro_pid"], state["target_process"])
                    except (OSError, RuntimeError):
                        # The user may close Allegro just after the native
                        # script writes its completion marker.
                        if (bundle / "finished.flag").is_file():
                            break
                        raise
                    next_identity_check = time.monotonic() + 2
                if not (bundle / "execution.started").is_file() and (
                        time.monotonic() >= deadline or self._stopping.is_set()):
                    self._signal_session_cancel(job_id)
                    raise RuntimeError("Allegro did not confirm script entry; cancellation marker left for any late execution")
                if time.monotonic() >= completion_deadline or (
                        cancellation_deadline is not None and time.monotonic() >= cancellation_deadline):
                    self._signal_session_cancel(job_id)
                    raise RuntimeError("Native completion is unconfirmed; Allegro remains open. Check this job again to collect any later native result")
                time.sleep(0.2)
            self._copy_native_log(bundle / "execution.log", log_stream, native_offset)
        self._complete_session(job_id, bundle)

    def _complete_session(self, job_id: str, bundle: Path, reconcile: bool = False) -> None:
        try:
            result = json.loads((bundle / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result = {"status": "invalid_result"}
        if not isinstance(result, dict):
            result = {"status": "invalid_result"}
        status = result.get("status")
        board = bundle / "result.brd"
        backup = bundle / "session-before.brd"
        updates = {"reconcile": reconcile, "native_pending": False,
                   "design_modified": result.get("design_modified"), "error": None}
        if status == "cancelled":
            self._log(job_id, "existing-session job cancelled cooperatively; Allegro remains open")
            self._finish(job_id, bundle, "cancelled", include_board=False,
                         **updates)
        elif (status == "success" and board.is_file() and board.stat().st_size
              and backup.is_file() and backup.stat().st_size
              and result.get("design_modified") is True
              and result.get("recovery_brd") == backup.as_posix()):
            self._log(job_id, "existing-session conversion succeeded; Allegro remains open")
            self._finish(job_id, bundle, "succeeded", include_board=True, honor_cancel=False,
                         result_bytes=board.stat().st_size,
                         cancellation_too_late=(bundle / "cancel.flag").is_file(), **updates)
        else:
            message = f"existing-session native run failed or recovery contract incomplete: status={status!r}; see execution.log"
            self._log(job_id, message)
            self._finish(job_id, bundle, "failed", include_board=False, honor_cancel=False,
                         **dict(updates, error=message))

    @staticmethod
    def _copy_native_log(source: Path, destination, offset: int) -> int:
        if not source.is_file():
            return offset
        size = source.stat().st_size
        if size < offset:
            offset = 0
        with source.open("rb") as stream:
            stream.seek(offset)
            while block := stream.read(256 * 1024):
                destination.write(block)
            return stream.tell()

    def _finish(self, job_id: str, bundle: Path, status: str, include_board: bool,
                honor_cancel: bool = True, reconcile: bool = False, **updates) -> dict[str, Any]:
        """Publish the archive before making a terminal state observable."""
        if status not in _TERMINAL_STATES:
            raise ValueError("finish status must be terminal")
        directory = self._job_dir(job_id)
        while True:
            state = self._state(job_id)
            chosen_status, chosen_board = status, include_board
            if honor_cancel and state["status"] == "cancel_requested" and status not in {"cancelled", "interrupted"}:
                chosen_status, chosen_board = "cancelled", False
            final = dict(state)
            final.update(updates)
            final.update(status=chosen_status, finished_utc=_utcnow(), updated_utc=_utcnow())
            self._make_result_archive(directory, bundle, chosen_board, job_state=final)
            with self._lock:
                current = self._state(job_id)
                if (honor_cancel and current["status"] == "cancel_requested" and
                        chosen_status not in {"cancelled", "interrupted"}):
                    continue
                if current["status"] in _TERMINAL_STATES and not (
                        reconcile and current["status"] == "interrupted" and current.get("native_pending")):
                    return self._public(current)
                _atomic_json(directory / "job.json", final)
                return self._public(final)

    def _make_result_archive(self, directory: Path, bundle: Path, include_board: bool,
                             job_state: dict[str, Any] | None = None) -> Path:
        destination = directory / "result.zip"
        temporary = directory / f"result.zip.{threading.get_ident()}.tmp"
        try:
            self._write_result_archive(temporary, directory, bundle, include_board, job_state)
            # Windows refuses to replace a file a download still holds open, so
            # publishing shares the lock with the handler's stat and open.
            with self._lock:
                os.replace(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return destination

    @staticmethod
    def _write_result_archive(temporary: Path, directory: Path, bundle: Path, include_board: bool,
                              job_state: dict[str, Any] | None = None) -> None:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            candidates = [
                (directory / "runner.log", "agent-runner.log"),
                (bundle / "execution.log", "execution.log"),
                (bundle / "generation.log", "generation.log"),
                (bundle / "generation.report.json", "generation.report.json"),
                (bundle / "manifest.json", "manifest.json"),
                (bundle / "result.json", "result.json"),
            ]
            # A snapshot may still be saving while native execution is unknown.
            # Include it only after a native terminal marker, when it is stable.
            if not (job_state or {}).get("native_pending"):
                candidates.append((bundle / "session-before.brd", "session-before.brd"))
            if include_board:
                candidates.insert(0, (bundle / "result.brd", "result.brd"))
            for source, name in candidates:
                if source.is_file():
                    archive.write(source, name)
            if job_state is None:
                archive.write(directory / "job.json", "job.json")
            else:
                archive.writestr("job.json", json.dumps(
                    job_state, ensure_ascii=False, indent=2
                ).encode("utf-8"))

    def get_job(self, job_id: str) -> dict[str, Any]:
        state = self._state(job_id)
        bundle = self._job_dir(job_id) / "bundle"
        if state["status"] == "interrupted" and state.get("native_pending") and (bundle / "finished.flag").is_file():
            with self._lock:
                reconcile = job_id not in self._reconciling
                if reconcile:
                    self._reconciling.add(job_id)
            if reconcile:
                try:
                    self._complete_session(job_id, bundle, reconcile=True)
                finally:
                    with self._lock:
                        self._reconciling.discard(job_id)
        return self._public(self._state(job_id))

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        states = []
        for directory in self._jobs_dir.iterdir():
            if not directory.is_dir():
                continue
            try:
                states.append(self.get_job(directory.name))
            except (ValueError, OSError, FileNotFoundError, json.JSONDecodeError):
                continue
        states.sort(key=lambda item: item.get("updated_utc", ""), reverse=True)
        return states[:limit]

    def logs(self, job_id: str, offset: int) -> dict[str, Any]:
        if offset < 0:
            raise ValueError("offset must be nonnegative")
        self._state(job_id)
        path = self._job_dir(job_id) / "runner.log"
        size = path.stat().st_size
        offset = min(offset, size)
        with path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(1024 * 1024)
            next_offset = stream.tell()
        return {"text": data.decode("utf-8", "replace"), "next_offset": next_offset}

    def result_path(self, job_id: str) -> Path:
        state = self.get_job(job_id)
        with self._lock:
            if job_id in self._reconciling:
                raise RuntimeError("result archive refresh is in progress; retry after checking job status")
        if state["status"] not in _TERMINAL_STATES:
            raise RuntimeError("result archive is available only for a terminal job")
        path = self._job_dir(job_id) / "result.zip"
        if not path.is_file():
            self._make_result_archive(self._job_dir(job_id), self._job_dir(job_id) / "bundle",
                                      include_board=state["status"] == "succeeded", job_state=state)
        return path

    def open_result(self, job_id: str) -> tuple[int, Any]:
        """Open the published archive and report its size under the state lock.

        The lock is held only for the stat and the open so a concurrent publish
        cannot replace the file mid-measurement; the transfer itself runs after
        the lock is released.
        """
        path = self.result_path(job_id)
        with self._lock:
            return path.stat().st_size, path.open("rb")

    def client_allowed(self, address: str) -> bool:
        if self._allowed is None:
            return True
        try:
            ip = ipaddress.ip_address(str(address).split("%", 1)[0])
        except ValueError:
            return False
        return any(ip in network for network in self._allowed)

    def authorized(self, header: str | None) -> bool:
        if not header:
            return False
        try:
            # Headers are decoded as latin-1, so a non-ASCII value would make
            # compare_digest raise instead of simply failing authentication.
            provided = header.encode("ascii")
        except (AttributeError, UnicodeEncodeError):
            return False
        return hmac.compare_digest(provided, ("Bearer " + self._token).encode("ascii"))


class _Handler(BaseHTTPRequestHandler):
    server_version = "BRD-SPD-Agent/1"
    sys_version = ""

    @property
    def agent(self) -> WorkstationAgent:
        return self.server.agent

    def log_message(self, _format, *_args):
        return

    def _json(self, status: int, value: dict[str, Any]) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _guard(self) -> bool:
        if not self.agent.client_allowed(self.client_address[0]):
            self._json(403, {"error": "client address is not allowed"})
            return False
        if not self.agent.authorized(self.headers.get("Authorization")):
            self._json(401, {"error": "authentication required"})
            return False
        return True

    def _body_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Content-Length is required")
        length = int(raw_length)
        if length < 0 or length > MAX_METADATA:
            raise ValueError("metadata request is too large")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _dispatch(self, method: str) -> None:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        parts = [part for part in path.split("/") if part]
        try:
            # The guard runs inside the try so a hostile header can never leave
            # the connection without a response.
            if not self._guard():
                return
            if method == "GET" and parts == ["v1", "health"]:
                self._json(200, {"status": "ok", "protocol": PROTOCOL_VERSION, "runner_slots": 1,
                                 "max_upload_bytes": self.agent.config.max_upload_bytes,
                                 "capabilities": ["allegro_pid"]})
                return
            if method == "GET" and len(parts) == 3 and parts[:2] == ["v1", "allegro"]:
                self._json(200, self.agent.check_pid(int(parts[2])))
                return
            if method == "GET" and parts == ["v1", "jobs"]:
                query = parse_qs(parsed.query)
                jobs = self.agent.list_jobs(int(query.get("limit", ["100"])[0]))
                self._json(200, {"jobs": jobs}); return
            if method == "POST" and parts == ["v1", "jobs"]:
                body = self._body_json()
                if not isinstance(body, dict):
                    raise ValueError("request body must be an object")
                self._json(201, self.agent.create_job(body.get("options", {})))
                return
            if len(parts) >= 3 and parts[:2] == ["v1", "jobs"]:
                job_id = _job_id(parts[2])
                if method == "GET" and len(parts) == 3:
                    self._json(200, self.agent.get_job(job_id)); return
                if method == "POST" and parts[3:] == ["submit"]:
                    self._json(200, self.agent.submit(job_id)); return
                if method == "POST" and parts[3:] == ["cancel"]:
                    self._json(200, self.agent.cancel(job_id)); return
                if method == "GET" and parts[3:] == ["logs"]:
                    query = parse_qs(parsed.query)
                    self._json(200, self.agent.logs(job_id, int(query.get("offset", ["0"])[0]))); return
                if method == "PUT" and len(parts) == 5 and parts[3] == "files":
                    raw_length = self.headers.get("Content-Length")
                    if raw_length is None:
                        raise ValueError("Content-Length is required")
                    self._json(200, self.agent.upload(job_id, parts[4], self.rfile, int(raw_length))); return
                if method == "GET" and parts[3:] == ["result"]:
                    size, stream = self.agent.open_result(job_id)
                    with stream:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/zip")
                        self.send_header("Content-Length", str(size))
                        self.send_header("Content-Disposition", 'attachment; filename="result.zip"')
                        self.end_headers()
                        shutil.copyfileobj(stream, self.wfile, 1024 * 1024)
                    return
            self._json(404, {"error": "route not found"})
        except FileNotFoundError as exc:
            self._json(404, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except RuntimeError as exc:
            self._json(409, {"error": str(exc)})
        except OSError as exc:
            self._json(409, {"error": str(exc)})
        except Exception:
            self._json(500, {"error": "internal agent error"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")


class RemoteClient:
    """Pinned-certificate client for :class:`WorkstationAgent`."""

    def __init__(self, host, port=8765, token="", fingerprint="", timeout=30):
        self.host, self.port = str(host), int(port)
        self.token = str(token)
        # Normalize once so pinning cannot be disabled by a value such as ":"
        # or by surrounding whitespace that only looks like a fingerprint.
        given = str(fingerprint)
        self.fingerprint = given.replace(":", "").strip().lower()
        if given.strip() and (len(self.fingerprint) != 64 or
                              self.fingerprint.strip("0123456789abcdef")):
            raise ValueError("fingerprint must be 64 hex characters")
        self.timeout = timeout
        if not self.fingerprint and not _is_loopback(self.host):
            raise ValueError("TLS certificate fingerprint is required for a non-loopback agent")

    def _connect(self) -> http.client.HTTPSConnection:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=context)
        connection.connect()
        actual = _fingerprint(connection.sock.getpeercert(binary_form=True))
        expected = self.fingerprint
        if expected and not hmac.compare_digest(actual, expected):
            connection.close()
            raise ssl.SSLError("agent TLS certificate fingerprint mismatch")
        return connection

    def _request(self, method: str, path: str, body: bytes | None = None,
                 content_type="application/json") -> tuple[http.client.HTTPResponse, http.client.HTTPSConnection]:
        connection = self._connect()
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        if body is not None:
            headers.update({"Content-Type": content_type, "Content-Length": str(len(body))})
        try:
            connection.request(method, path, body=body, headers=headers)
            return connection.getresponse(), connection
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _json_response(response, connection) -> dict[str, Any]:
        try:
            data = response.read()
        finally:
            connection.close()
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"agent returned invalid response (HTTP {response.status})") from exc
        if response.status >= 400:
            raise RuntimeError(f"agent HTTP {response.status}: {value.get('error', value)}")
        return value

    def _json_call(self, method: str, path: str, value: dict[str, Any] | None = None):
        body = json.dumps(value).encode("utf-8") if value is not None else None
        response, connection = self._request(method, path, body)
        return self._json_response(response, connection)

    def health(self):
        return self._json_call("GET", "/v1/health")

    def check_pid(self, pid: int) -> dict:
        pid = _safe_options({"allegro_pid": pid})["allegro_pid"]
        return self._json_call("GET", f"/v1/allegro/{pid}")

    def create_job(self, options: dict) -> dict:
        result = self._json_call("POST", "/v1/jobs", {"options": options})
        return {"id": result["id"], "status": result["status"]}

    def list_jobs(self) -> list[dict]:
        return self._json_call("GET", "/v1/jobs?limit=100")["jobs"]

    def upload_file(self, job_id, slot, path, progress=None):
        job_id = _job_id(job_id)
        if slot not in _SLOTS:
            raise ValueError("slot must be 'spd' or 'brd'")
        if slot == "brd" and "allegro_pid" in self.get_job(job_id)["options"]:
            raise ValueError("PID mode uses the BRD already open in Allegro; do not upload a base BRD")
        path = Path(path)
        size = path.stat().st_size
        maximum = int(self.health()["max_upload_bytes"])
        if size <= 0 or size > maximum:
            raise ValueError(f"upload size must be between 1 and {maximum} bytes")
        connection = self._connect()
        try:
            connection.putrequest("PUT", f"/v1/jobs/{job_id}/files/{slot}")
            connection.putheader("Authorization", "Bearer " + self.token)
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("Content-Length", str(size))
            connection.endheaders()
            sent = 0
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    connection.send(block)
                    sent += len(block)
                    if progress:
                        progress(f"Uploading {slot}: {sent}/{size} bytes")
            return self._json_response(connection.getresponse(), connection)
        except BaseException:
            connection.close()
            raise

    def submit_job(self, job_id):
        return self._json_call("POST", f"/v1/jobs/{_job_id(job_id)}/submit", {})

    def get_job(self, job_id):
        return self._json_call("GET", f"/v1/jobs/{_job_id(job_id)}")

    def get_logs(self, job_id, offset=0) -> dict:
        if int(offset) < 0:
            raise ValueError("offset must be nonnegative")
        return self._json_call("GET", f"/v1/jobs/{_job_id(job_id)}/logs?offset={int(offset)}")

    def cancel_job(self, job_id):
        return self._json_call("POST", f"/v1/jobs/{_job_id(job_id)}/cancel", {})

    def download_result(self, job_id, destination: Path, progress=None):
        job_id = _job_id(job_id)
        destination = Path(destination).resolve()
        if destination.exists():
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        response, connection = self._request("GET", f"/v1/jobs/{job_id}/result")
        if response.status >= 400:
            return self._json_response(response, connection)
        total = int(response.getheader("Content-Length", "0"))
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("wb", delete=False, dir=destination.parent,
                                             prefix=destination.name + ".", suffix=".part") as output:
                temporary = Path(output.name)
                received = 0
                while block := response.read(1024 * 1024):
                    output.write(block)
                    received += len(block)
                    if progress:
                        progress(f"Downloading result: {received}/{total} bytes")
            if total and received != total:
                raise ConnectionError("result download ended before Content-Length")
            os.replace(temporary, destination)
            temporary = None
            return destination
        finally:
            connection.close()
            if temporary is not None:
                temporary.unlink(missing_ok=True)
