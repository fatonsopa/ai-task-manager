"""Installed listener lifetime over existing durable intake and inbound policy."""

import fcntl
import hashlib
import json
import math
import os
import re
import signal
import stat
import threading
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from ai_task_manager.adapters.decisions.observations import SessionObservations, current_enrollment
from ai_task_manager.adapters.linear.listener import LoopbackListener
from ai_task_manager.adapters.linear.models import canonical_id
from ai_task_manager.adapters.linear.webhooks import WebhookReceiver
from ai_task_manager.config.files import FileSeal, private_path, read_private
from ai_task_manager.domains.work.errors import AccessDenied, ValidationError


class Source:
    def __init__(self, path):
        self.seal = FileSeal(path)
        self.metadata = self.seal.metadata()
        self.check()

    def check(self):
        if self.seal.metadata() != self.metadata:
            raise AccessDenied("Receiver source changed")
        self.seal.check()


class Owner:
    """A retained flock is the owner; a stale PID never authorizes process cleanup."""

    def __init__(self, database):
        database = private_path(database)
        info = database.stat()
        if info.st_nlink != 1 or database.parent.stat().st_mode & 0o077:
            raise AccessDenied("Receiver database requires private unaliased storage")
        self.path = database.parent / f".linear-receiver-{info.st_dev}-{info.st_ino}.lock"
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(self.fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise AccessDenied("Receiver ownership file is unsafe")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.identity = (info.st_dev, info.st_ino)
            self.check()
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def check(self):
        info = private_path(self.path).lstat()
        if (info.st_dev, info.st_ino) != self.identity or info.st_nlink != 1:
            raise AccessDenied("Receiver ownership file changed")

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class InstalledReceiver:
    def __init__(self, service, config_file):
        self.service = service
        if service.inbound is None or service.seal is None:
            raise AccessDenied("Configured Linear inbound service is required")
        config = read_private(config_file)
        path = config.get("receiver_file")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValidationError("An absolute private receiver_file is required")
        self.sources = [Source(config_file), Source(path), Source(config["session_file"])]
        values = read_private(path)
        if (
            not isinstance(values, dict)
            or set(values)
            != {"version", "webhook_id", "route", "port", "secret_file", "lifetime_seconds"}
            or type(values["version"]) is not int
            or values["version"] != 1
        ):
            raise ValidationError("Unknown receiver configuration")
        canonical_id(values["webhook_id"])
        if (
            not isinstance(values["route"], str)
            or not re.fullmatch(r"/hooks/[A-Za-z0-9_-]{16,128}", values["route"])
            or type(values["port"]) is not int
            or not 0 <= values["port"] <= 65535
            or type(values["lifetime_seconds"]) not in {int, float}
            or not math.isfinite(values["lifetime_seconds"])
            or not 0 < values["lifetime_seconds"] <= 86400
        ):
            raise ValidationError("Receiver route, loopback port and finite lifetime are required")
        secret_path = values["secret_file"]
        if not isinstance(secret_path, str) or not Path(secret_path).is_absolute():
            raise ValidationError("An absolute private webhook secret file is required")
        self.sources.append(Source(secret_path))
        secret = read_private(secret_path)
        if (
            not isinstance(secret, dict)
            or set(secret) != {"secret"}
            or not isinstance(secret["secret"], str)
            or not 16 <= len(secret["secret"]) <= 4096
            or any(not 0x21 <= ord(c) <= 0x7E for c in secret["secret"])
        ):
            raise ValidationError("Webhook secret must be bounded visible ASCII")
        self.secret = secret["secret"].encode()
        if config.get("linear", {}).get("credential_file"):
            self.sources.append(Source(config["linear"]["credential_file"]))
        self.client = service.inbound.client
        self.observations = service.decisions.observations if service.decisions else None
        if not isinstance(self.observations, SessionObservations) or not service.grant_ref:
            raise AccessDenied("Installed session enrollment is required for the listener")
        grant = self.observations.verify_enrollment(service.grant_ref)
        if grant.scope != service.scope or grant.expires_at <= time.time():
            raise AccessDenied("Receiver enrollment is expired or belongs to another session")
        self.credential_digest = hashlib.sha256(
            self.client.credentials().authorization().encode()
        ).digest()
        self.owner = None
        self.http = None
        self.worker = None
        self.closing, self.wake = threading.Event(), threading.Event()
        self.failed = None
        self.instance = str(uuid4())
        self.values = values
        self.deadline = time.monotonic() + values["lifetime_seconds"]
        self.adapter = WebhookReceiver(
            service.core,
            service.handle,
            service.inbound.journal,
            self.client.binding,
            values["webhook_id"],
            lambda: self.secret,
            clock=time.time,
            wake=self.wake.set,
            max_body_bytes=LoopbackListener.BODY_LIMIT,
        )
        self.check()

    def check(self):
        if self.closing.is_set():
            raise AccessDenied("Receiver is stopping")
        for source in self.sources:
            source.check()
        if self.service.claim_store:
            self.service.claim_store.check()
        # This is a local signed-record/source check. Provider validation ran once
        # during startup, never in the HTTP callback or a Core transaction.
        self.observations._check()
        current_enrollment(
            self.observations.journal, self.service.grant_ref, asdict(self.service.scope)
        )
        if self.service.core.session_binding(self.service.handle) != (
            self.service.session_id,
            self.service.scope,
        ):
            raise AccessDenied("Receiver session changed")
        if (
            hashlib.sha256(self.client.credentials().authorization().encode()).digest()
            != self.credential_digest
        ):
            raise AccessDenied("Receiver credential changed")
        if self.owner:
            self.owner.check()
        if time.monotonic() >= self.deadline:
            raise AccessDenied("Receiver lifetime expired")

    def receive(self, method, headers, body):
        self.check()
        return self.adapter.receive(method, headers, body)

    def health(self):
        self.check()
        return json.dumps({"instance_id": self.instance}).encode()

    def _recovery_delay(self):
        # A crashed holder's durable lease supplies its own recovery wake deadline.
        rows = self.service.inbound.journal.deliveries(self.service.scope)
        leases = [r["lease_until"] for r in rows if r["state"] == "leased" and r["lease_until"]]
        return max(0, min(leases) - time.time()) if leases else None

    def _drain(self):
        delay = None
        try:
            while not self.closing.is_set():
                self.wake.wait(delay)
                self.wake.clear()
                if self.closing.is_set():
                    break
                # Repair a crash between journal acceptance and Core intake using
                # the existing signed ingress receipt and idempotent Core operation.
                for receipt in self.service.inbound.journal.deliveries(self.service.scope):
                    if receipt["state"] in {"received", "leased"}:
                        self.check()
                        self.service.core.sync.ingest(
                            self.service.handle,
                            receipt["delivery_id"],
                            json.loads(receipt["payload"]),
                            receipt["ingress_ref"],
                        )
                for _ in range(32):
                    self.check()
                    if self.closing.is_set():
                        break
                    result = self.service.inbound.run_once(
                        self.service.handle, check_authority=self.check
                    )
                    if result is None or result.get("state") == "pending":
                        break
                else:
                    self.wake.set()
                delay = self._recovery_delay()
        except BaseException:
            self.failed = "receiver_drain_failed"
            self.closing.set()

    def start(self):
        if self.owner is not None or self.closing.is_set():
            raise AccessDenied("Receiver can start only once")
        try:
            self.owner = Owner(self.service.core.context.db.path)
            self.check()
            self.http = LoopbackListener(
                self.receive,
                route=self.values["route"],
                port=self.values["port"],
                health=self.health,
            )
            self.http.start()
            self.worker = threading.Thread(target=self._drain, name="linear-inbound", daemon=True)
            self.worker.start()
            self.wake.set()  # Pending durable work is processed even without a new delivery.
            return {
                "instance_id": self.instance,
                "host": self.http.address[0],
                "port": self.http.address[1],
                "route": self.http.route,
                "health_route": self.http.health_route,
                "lifetime_seconds": self.values["lifetime_seconds"],
            }
        except BaseException:
            self.stop()
            raise

    def stop(self, timeout=12):
        end = time.monotonic() + timeout
        self.closing.set()
        self.wake.set()
        http = self.http.close(max(0, end - time.monotonic())) if self.http else {"stopped": True}
        if self.worker is not None and self.worker.ident is not None:
            self.worker.join(max(0, end - time.monotonic()))
        drain_stopped = self.worker is None or not self.worker.is_alive()
        stopped = http["stopped"] and drain_stopped
        if stopped and self.owner:
            self.owner.close()
        return {
            "stopped": stopped,
            "listener": http,
            "drain_stopped": drain_stopped,
            "ownership_released": stopped,
            "failure": self.failed,
        }


def serve(service, config_file, emit, *, receiver=None):
    """CLI-only trusted operator lifetime; no model-visible authority operation."""
    stop_requested = threading.Event()
    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, lambda *_: stop_requested.set())
    failed = None
    cleanup = {"stopped": True}
    try:
        if receiver is None:
            receiver = InstalledReceiver(service, config_file)
        ready = receiver.start()
        emit({"ok": True, "event": "receiver_ready", "data": ready})
        while not stop_requested.wait(0.25):
            if time.monotonic() >= receiver.deadline:
                break
            receiver.check()
            if receiver.closing.is_set() or receiver.http.failure:
                failed = receiver.failed or "receiver_listener_failed"
                break
    except BaseException:
        failed = "receiver_refused"
    finally:
        if receiver is not None:
            cleanup = receiver.stop()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    ok = failed is None and cleanup["stopped"]
    emit({"ok": ok, "event": "receiver_stopped", "failure": failed, "cleanup": cleanup})
    return 0 if ok else 1


def serve_config(config_file, session_file, emit):
    """Bound provider/configuration startup before any listener threads exist."""
    from ai_task_manager.config.service import load_service

    previous = signal.getsignal(signal.SIGALRM)
    timer = signal.getitimer(signal.ITIMER_REAL)

    def timeout(*_):
        raise TimeoutError("Receiver startup deadline")

    try:
        signal.signal(signal.SIGALRM, timeout)
        signal.setitimer(signal.ITIMER_REAL, 30)
        service = load_service(config_file, session_file)
        receiver = InstalledReceiver(service, config_file)
    except BaseException:
        emit(
            {
                "ok": False,
                "event": "receiver_stopped",
                "failure": "receiver_startup_refused",
                "cleanup": {"stopped": True, "listener_started": False},
            }
        )
        return 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, *timer)
        signal.signal(signal.SIGALRM, previous)
    return serve(service, config_file, emit, receiver=receiver)
