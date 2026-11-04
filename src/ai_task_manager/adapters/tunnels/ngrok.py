"""One pinned ngrok process; no default credentials, config, or existing tunnel adoption."""

import hashlib
import math
import os
import re
import select
import shlex
import signal
import stat
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from ai_task_manager.adapters.process.groups import OwnedProcessGroup, require_group_monitoring
from ai_task_manager.config.environment import CREDENTIAL_ENVIRONMENT_NAME_PATTERN
from ai_task_manager.config.files import MAX_JSON_BYTES, digest, private_path, strict_json

CONFIG = b"""version: 3
agent:
  web_addr: false
  console_ui: false
  remote_management: false
  update_check: false
  inspect_db_size: 0
"""
KEYS = frozenset({"NGROK_AUTH_TOKEN", "NGROK_AUTHTOKEN"})


class NgrokRefused(RuntimeError):
    """Fixed diagnostic codes only; external output and secrets are never diagnostics."""


@contextmanager
def _defer_interruptions():
    """Keep registered raising handlers out of the spawn/ownership handoff.

    Do not block POSIX signals: a child must not inherit a blocked termination mask.
    Restore the caller's handlers and deliver the first deferred interruption.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    pending, handlers = [], {}

    def defer(signum, frame):
        if not pending:
            pending.append((signum, frame))

    try:
        for value in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
            handler = signal.getsignal(value)
            if callable(handler):
                handlers[value] = handler
                signal.signal(value, defer)
        yield
    finally:
        for value, handler in handlers.items():
            signal.signal(value, handler)
        if pending:
            value, frame = pending[0]
            handlers[value](value, frame)


class _EnvironmentPin:
    def __init__(self, name):
        if not isinstance(name, str) or not re.fullmatch(CREDENTIAL_ENVIRONMENT_NAME_PATTERN, name):
            raise NgrokRefused("ngrok_credential_environment_name_refused")
        self.name = name
        self.sha256 = None

    def read(self):
        value = os.environ.get(self.name)
        if not value or len(value) > 4096 or any(not 0x21 <= ord(c) <= 0x7E for c in value):
            raise NgrokRefused("ngrok_credential_absent_or_ambiguous")
        hashed = hashlib.sha256(value.encode()).digest()
        if self.sha256 is not None and self.sha256 != hashed:
            raise NgrokRefused("ngrok_credential_changed")
        self.sha256 = hashed
        return value

    def check(self):
        self.read()


def _metadata(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _no_links(path):
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise NgrokRefused("ngrok_symbolic_link_refused")


class _FilePin:
    def __init__(self, path, *, private=False, maximum=128 * 1024 * 1024):
        self.path = Path(path).absolute()
        _no_links(self.path)
        if private:
            private_path(self.path)
        before = self.path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_mode & 0o022
            or before.st_uid not in {0, os.getuid()}
            or before.st_size > maximum
        ):
            raise NgrokRefused("ngrok_file_refused")
        descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(maximum + 1)
        if (
            _metadata(before) != _metadata(opened)
            or _metadata(before) != _metadata(self.path.lstat())
            or len(raw) > maximum
        ):
            raise NgrokRefused("ngrok_file_changed")
        self.metadata, self.sha256 = _metadata(before), hashlib.sha256(raw).hexdigest()

    def check(self):
        _no_links(self.path)
        if _metadata(self.path.lstat()) != self.metadata:
            raise NgrokRefused("ngrok_file_changed")


def credential(path):
    """Read the two allowed aliases as data from one explicit private source."""
    pin = _FilePin(path, private=True, maximum=MAX_JSON_BYTES)
    descriptor = os.open(pin.path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if _metadata(os.fstat(stream.fileno())) != pin.metadata:
            raise NgrokRefused("ngrok_file_changed")
        raw = stream.read(MAX_JSON_BYTES + 1)
    pin.check()
    try:
        values = {}
        for line in raw.decode("utf-8").splitlines():
            match = re.match(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$", line)
            if match and match[1] in KEYS:
                parts = shlex.split(match[2], comments=True)
                if (
                    match[1] in values
                    or len(parts) != 1
                    or not parts[0]
                    or len(parts[0]) > 4096
                    or any(not 0x21 <= ord(c) <= 0x7E for c in parts[0])
                ):
                    raise ValueError()
                values[match[1]] = parts[0]
        if not values or len(set(values.values())) != 1:
            raise ValueError()
        return next(iter(values.values())), pin
    except (UnicodeError, ValueError):
        raise NgrokRefused("ngrok_credential_absent_or_ambiguous") from None


def _upstream(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.port is None
        or not 1 <= parsed.port <= 65535
        or value != f"http://127.0.0.1:{parsed.port}"
    ):
        raise NgrokRefused("ngrok_loopback_upstream_required")
    return value


def _origin(value):
    if not isinstance(value, str) or len(value) > 253:
        raise NgrokRefused("ngrok_https_origin_required")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", parsed.hostname)
        or "." not in parsed.hostname
        or parsed.hostname.endswith(".localhost")
        or parsed.hostname.replace(".", "").isdigit()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.port is not None
        or value != "https://" + parsed.hostname
    ):
        raise NgrokRefused("ngrok_https_origin_required")
    return value


class NgrokTunnel:
    """Launch is an explicit effect. Observations never accept a caller-provided URL."""

    def __init__(
        self,
        executable,
        executable_sha256,
        credential_file,
        target,
        root,
        *,
        lifetime=105.0,
        credential_environment=None,
    ):
        if (credential_file is None) == (credential_environment is None):
            raise NgrokRefused("ngrok_exact_credential_source_required")
        environment_pin = (
            _EnvironmentPin(credential_environment) if credential_environment is not None else None
        )
        self.target = _upstream(target)
        if (
            type(lifetime) not in {int, float}
            or not math.isfinite(lifetime)
            or not 0 < lifetime <= 86400
            or not re.fullmatch(r"[0-9a-f]{64}", executable_sha256)
        ):
            raise NgrokRefused("ngrok_bound_required")
        self.executable = _FilePin(executable)
        if self.executable.sha256 != executable_sha256 or not os.access(executable, os.X_OK):
            raise NgrokRefused("ngrok_executable_pin_mismatch")
        self.root = Path(root).absolute()
        _no_links(self.root)
        # Exclusive creation refuses occupied run directories; never reuse user configuration.
        self.root.mkdir(mode=0o700)
        self.directory_pins = [(p, p.stat().st_dev, p.stat().st_ino) for p in (self.root,)]
        config = self.root / "ngrok.yml"
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(CONFIG)
        self.config = _FilePin(config, private=True)
        self.credential_file, self.credential_pin = credential_file, environment_pin
        self.argv = (
            str(self.executable.path),
            "http",
            self.target,
            "--config",
            str(config),
            "--inspect=false",
            "--log=stdout",
            "--log-format=json",
            "--log-level=info",
            "--name=owned-receiver",
        )
        self.lifetime, self.group, self.thread = lifetime, None, None
        self.lock, self.stop_lock = threading.RLock(), threading.Lock()
        self.closing = threading.Event()
        self.origin, self.error, self.closed = None, None, None
        self.agent_code = None
        self.stage = "prepared"
        self.started = False
        self.process = None
        self.launch_attempted = False

    def _files(self):
        for path, device, inode in self.directory_pins:
            _no_links(path)
            info = path.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700
                or (info.st_dev, info.st_ino) != (device, inode)
            ):
                raise NgrokRefused("ngrok_directory_changed")
        self.executable.check()
        self.config.check()
        if self.credential_pin is not None:
            self.credential_pin.check()

    def start(self):
        with self.lock:
            if self.started or self.closing.is_set():
                raise NgrokRefused("ngrok_start_once_required")
            self.started = True
            self.stage = "starting"
            self._files()
            require_group_monitoring()
            if isinstance(self.credential_pin, _EnvironmentPin):
                token = self.credential_pin.read()
            else:
                token, self.credential_pin = credential(self.credential_file)
            self._files()
            environment = {
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "LC_ALL": "C",
                "NGROK_AUTHTOKEN": token,
            }
            try:
                with _defer_interruptions():
                    self.launch_attempted = True
                    self.process = subprocess.Popen(
                        self.argv,
                        cwd=self.root,
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    self.group = OwnedProcessGroup(self.process)
                self.deadline = time.monotonic() + self.lifetime
                initial = self.group.capture()
                if not initial.known:
                    self.error = "ngrok_process_unknown"
                    raise NgrokRefused(self.error)
                self.thread = threading.Thread(target=self._read, name="owned-ngrok", daemon=True)
                self.thread.start()
            except BaseException:
                self.stop()
                raise
            finally:
                environment.clear()
                token = None
        return self

    def _record(self, raw):
        row = strict_json(raw)
        if not isinstance(row, dict):
            raise NgrokRefused("ngrok_output_malformed")
        # A warning/error, reconnect, or second endpoint makes the old observation unusable.
        if row.get("lvl") not in {"info", "debug"}:
            # Retain only documented identifiers, never the message around them.
            codes = set(
                re.findall(
                    r"\bERR_NGROK_(?:105|107|108|334|8012)\b",
                    str(row.get("err", "")) + " " + str(row.get("msg", "")),
                )
            )
            if len(codes) == 1:
                self.agent_code = codes.pop()
            raise NgrokRefused("ngrok_agent_unhealthy")
        if row.get("msg") == "started tunnel":
            if row.get("addr") != self.target:
                raise NgrokRefused("ngrok_upstream_changed")
            if (
                self.origin is not None
                or row.get("obj") != "tunnels"
                or row.get("name") != "owned-receiver"
            ):
                raise NgrokRefused("ngrok_endpoint_ambiguous")
            self.origin = _origin(row.get("url"))
            self.stage = "observing"
        elif self.origin is not None:
            if row.get("msg") == "join connections" and row.get("obj") == "join":
                if row.get("l") != self.target.removeprefix("http://"):
                    raise NgrokRefused("ngrok_upstream_changed")
                # Connection diagnostics grant no new authority; discard client identity.
                return
            # Unknown later agent events do not establish continued endpoint health.
            raise NgrokRefused("ngrok_endpoint_observation_changed")

    def _read(self):
        pending, total = b"", 0
        try:
            stream = self.group.process.stdout
            while not self.closing.is_set():
                if time.monotonic() >= self.deadline:
                    raise NgrokRefused("ngrok_deadline")
                if not select.select([stream], [], [], 0.05)[0]:
                    continue
                data = os.read(stream.fileno(), 8192)
                if not data:
                    raise NgrokRefused("ngrok_output_closed")
                total += len(data)
                pending += data
                if total > 65536 or len(pending) > 16384:
                    raise NgrokRefused("ngrok_output_bound")
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    with self.lock:
                        self._record(line)
        except Exception as error:
            with self.lock:
                self.error = (
                    str(error) if isinstance(error, NgrokRefused) else "ngrok_output_invalid"
                )
        finally:
            # No raw stdout/stderr is persisted, including authentication failure text.
            self.stop()

    def observe(self, target):
        with self.lock:
            if (
                self.error
                or self.closing.is_set()
                or not self.started
                or self.group is None
                or self.thread is None
                or not self.thread.is_alive()
                or time.monotonic() >= self.deadline
            ):
                raise NgrokRefused(self.error or "ngrok_not_running")
            self._files()
            leader = self.group.leader()
            if leader.known and leader.exited and self.origin is None:
                # Drain terminal authentication diagnostics already in the owned pipe.
                # Pending never grants endpoint authority; discovery remains bounded.
                raise NgrokRefused("ngrok_endpoint_pending")
            group = self.group.observe()
            if (
                not leader.known
                or leader.exited
                or not group.known
                or not any(m.pid == self.group.process.pid for m in group.live_members)
                or tuple(self.group.process.args) != self.argv
            ):
                raise NgrokRefused("ngrok_process_unknown")
            if target != self.target:
                raise NgrokRefused("ngrok_upstream_changed")
            if self.origin is None:
                raise NgrokRefused("ngrok_endpoint_pending")
            fields = {
                "transport": "ngrok",
                "pid": self.group.process.pid,
                "start_identity": self.group.start_identity,
                "argv_sha256": digest(self.argv),
                "executable_sha256": self.executable.sha256,
                "config_sha256": self.config.sha256,
                "origin": self.origin,
                "upstream": self.target,
            }
            return {**fields, "identity": digest(fields)}

    def stop(self):
        with _defer_interruptions():
            return self._stop()

    def _stop(self):
        self.closing.set()
        with self.stop_lock:
            if self.closed is None:
                if self.group is None and self.process is not None:
                    try:
                        self.group = OwnedProcessGroup(self.process)
                    except Exception:
                        pass
                if self.group is None:
                    self.closed = {
                        "confirmed_stop": not self.launch_attempted,
                        "started": self.process is not None,
                        "leader_pid": self.process.pid if self.process is not None else None,
                        "reason": "ngrok_cleanup_unknown" if self.launch_attempted else None,
                    }
                else:
                    try:
                        stop = self.group.stop()
                        self.closed = self.group.evidence(stop, orphaned=False)
                        self.closed["returncode"] = self.group.reap_exited_leader()
                        self.closed["reaped"] = self.group.process.returncode is not None
                    except Exception:
                        self.closed = {
                            "confirmed_stop": False,
                            "reaped": False,
                            "leader_pid": self.group.process.pid,
                            "leader_start_identity": self.group.start_identity,
                            "reason": "ngrok_cleanup_unknown",
                        }
                    finally:
                        self.group.process.stdout.close()
        if (
            self.thread is not None
            and self.thread is not threading.current_thread()
            and self.thread.is_alive()
        ):
            self.thread.join(timeout=1)
        result = dict(self.closed)
        if self.thread is not threading.current_thread():
            result["reader_stopped"] = self.thread is None or not self.thread.is_alive()
            result["confirmed_stop"] = result["confirmed_stop"] and result["reader_stopped"]
        result["diagnostic"] = self.diagnostic()
        return result

    def diagnostic(self):
        category = {
            "ERR_NGROK_105": "invalid_token",
            "ERR_NGROK_107": "invalid_token",
            "ERR_NGROK_108": "account_session_limit",
            "ERR_NGROK_334": "endpoint_occupied",
            "ERR_NGROK_8012": "upstream_unavailable",
        }.get(self.agent_code)
        if category is None:
            category = {
                "ngrok_deadline": "timeout",
                "ngrok_upstream_changed": "upstream_mismatch",
                "ngrok_endpoint_ambiguous": "ambiguous_endpoint",
            }.get(self.error, "unknown" if self.error else "none")
        return {
            "stage": self.stage,
            "category": category,
            "agent_code": self.agent_code,
            "exit_status": self.closed.get("returncode") if self.closed else None,
        }
