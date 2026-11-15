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
from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.linear.listener import ListenerStopping, LoopbackListener
from ai_task_manager.adapters.linear.models import canonical_id
from ai_task_manager.adapters.linear.webhooks import WebhookReceiver
from ai_task_manager.application.linear_sync.reconcile import Reconciliation
from ai_task_manager.config.files import FileSeal, private_path, read_private
from ai_task_manager.domains.work.errors import AccessDenied, StaleVersion, ValidationError
from ai_task_manager.observability.failures import FailureStage, capture_failure


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
            not in (
                {"version", "webhook_id", "route", "port", "secret_file", "lifetime_seconds"},
                {
                    "version",
                    "webhook_id",
                    "route",
                    "port",
                    "secret_file",
                    "lifetime_seconds",
                    "reconciliation_seconds",
                },
            )
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
        self.reconciliation_seconds = values.get("reconciliation_seconds", 300)
        if (
            type(self.reconciliation_seconds) is not int
            or not 120 <= self.reconciliation_seconds <= 900
        ):
            raise ValidationError("Reconciliation interval must be120 to900 seconds")
        self.reconciliation = Reconciliation(
            service.core, service.inbound.client, service.inbound.journal
        )
        self.next_reconciliation = 0.0
        self.reconciliation_after = None
        self.reconciliation_pending = {}
        self.provider_not_before = 0.0
        self.reconciliation_failures = 0
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
        # Enrollment rechecks provider identity and replaces the immutable binding.
        # Refresh the selected app before the HTTP adapter captures that binding.
        self.client.check_connection(service.scope, refresh=True)
        self.credential_digest = hashlib.sha256(
            self.client.credentials().authorization().encode()
        ).digest()
        self.owner = None
        self.http = None
        self.worker = None
        self.recovery_worker = None
        self.reconciliation_wake = threading.Event()
        self.draining = threading.Event()
        self.closing, self.wake = threading.Event(), threading.Event()
        self.failed = None
        self._diagnostics = []
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
        # Check real revocation/source failures even when shutdown was requested.
        # Only this explicit lifecycle signal is an expected drain cancellation.
        if self.closing.is_set():
            raise ListenerStopping()
        if time.monotonic() >= self.deadline:
            raise AccessDenied("Receiver lifetime expired")

    def receive(self, method, headers, body):
        self.check()
        return self.adapter.receive(method, headers, body)

    def health(self):
        self.check()
        return json.dumps({"instance_id": self.instance}).encode()

    # Only parked receipts need this local recovery check. Core is the durable
    # cross-process signal, including a crash before an optional notification.
    CONFIRMATION_RECOVERY_SECONDS = 0.25

    def _recovery_delay(self):
        # A crashed holder's durable lease supplies its own recovery wake deadline.
        rows = self.service.inbound.journal.deliveries(self.service.scope)
        delays = []
        now = time.time()
        for row in rows:
            waiting = json.loads(row["checkpoint"]).get("awaiting_outbound")
            if row["state"] == "leased" and row["lease_until"] > now:
                delays.append(row["lease_until"] - now)
            elif row["state"] in {"received", "leased"} and waiting:
                # A crash after checkpoint but before release may retain an
                # expired lease. It must not turn this recovery wait into a spin.
                delays.append(self.CONFIRMATION_RECOVERY_SECONDS)
            elif row["state"] in {"received", "leased"}:
                # Work may have arrived or changed claimant after selection.
                delays.append(0)
        return min(delays) if delays else None

    def _reconcile(self):
        """One bounded recovery slice using the same Core observation policy."""
        self.check()
        try:
            due = sorted(
                task_id
                for task_id, deadline in self.reconciliation_pending.items()
                if deadline <= time.monotonic()
            )[:8]
            rows = self.reconciliation.inspect_registered(
                self.service.handle,
                check_authority=self.check,
                max_tasks=8,
                after_task_id=None if due else self.reconciliation_after,
                task_ids=due if due else None,
                should_yield=lambda: self.wake.is_set() or self.draining.is_set(),
            )
            if rows and not due:
                self.reconciliation_after = rows[-1]["task_id"]
            for row in rows:
                self.reconciliation_pending.pop(row["task_id"], None)
                if "retry_at" in row:
                    self.reconciliation_pending[row["task_id"]] = row["retry_at"]
            if due and not rows:
                for task_id in due:
                    self.reconciliation_pending.pop(task_id, None)
            self.reconciliation_failures = 0
            self.next_reconciliation = min(
                [time.monotonic() + self.reconciliation_seconds]
                + list(self.reconciliation_pending.values())
            )
        except StaleVersion:
            # A current webhook/local operation won. Never apply the older read.
            self.next_reconciliation = time.monotonic() + self.reconciliation_seconds
        except LinearError as error:
            self.check()
            if error.category not in {
                "rate_limited",
                "http_failure",
                "transport_failure",
                "transport_deadline",
            }:
                raise
            self.reconciliation_failures = min(self.reconciliation_failures + 1, 4)
            backoff = min(
                3600, self.reconciliation_seconds * 2 ** (self.reconciliation_failures - 1)
            )
            if error.retry_after is not None:
                if (
                    type(error.retry_after) not in (int, float)
                    or not math.isfinite(error.retry_after)
                    or error.retry_after < 0
                ):
                    raise
                backoff = max(backoff, error.retry_after)
            self.provider_not_before = time.monotonic() + backoff
            self.next_reconciliation = self.provider_not_before

    def _reconcile_loop(self):
        try:
            while not self.closing.is_set():
                due = max(self.next_reconciliation, self.provider_not_before)
                self.reconciliation_wake.wait(max(0, due - time.monotonic()))
                self.reconciliation_wake.clear()
                if self.closing.is_set():
                    break
                if time.monotonic() < max(self.next_reconciliation, self.provider_not_before):
                    continue
                self._reconcile()
        except ListenerStopping:
            pass
        except BaseException as error:
            try:
                self._diagnostics.append(capture_failure(error, FailureStage.RECEIVER_MONITOR))
            finally:
                self.failed = "receiver_reconciliation_failed"
                self.closing.set()
                self.wake.set()

    def _drain(self):
        delay = None
        try:
            while not self.closing.is_set():
                self.wake.wait(delay)
                self.wake.clear()
                if self.closing.is_set():
                    break
                self.check()
                if time.monotonic() < self.provider_not_before:
                    delay = self.provider_not_before - time.monotonic()
                    continue
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
                    if time.monotonic() < self.provider_not_before:
                        break
                    self.draining.set()
                    try:
                        result = self.service.inbound.run_once(
                            self.service.handle, check_authority=self.check
                        )
                    finally:
                        self.draining.clear()
                    if result is None:
                        break
                else:
                    self.wake.set()
                delay = self._recovery_delay()
                if time.monotonic() < self.provider_not_before:
                    delay = self.provider_not_before - time.monotonic()
        except ListenerStopping:
            pass
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
            self.recovery_worker = threading.Thread(
                target=self._reconcile_loop, name="linear-reconciliation", daemon=True
            )
            self.recovery_worker.start()
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
        self.reconciliation_wake.set()
        http = self.http.close(max(0, end - time.monotonic())) if self.http else {"stopped": True}
        if self.worker is not None and self.worker.ident is not None:
            self.worker.join(max(0, end - time.monotonic()))
        drain_stopped = self.worker is None or not self.worker.is_alive()
        if self.recovery_worker is not None and self.recovery_worker.ident is not None:
            self.recovery_worker.join(max(0, end - time.monotonic()))
        recovery_stopped = self.recovery_worker is None or not self.recovery_worker.is_alive()
        stopped = http["stopped"] and drain_stopped and recovery_stopped
        if self.http and self.http.failure:
            self.failed = self.failed or "receiver_listener_failed"
        if stopped and self.owner:
            self.owner.close()
        return {
            "stopped": stopped,
            "listener": http,
            "drain_stopped": drain_stopped,
            "reconciliation_stopped": recovery_stopped,
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
    diagnostics = []
    stage = FailureStage.RECEIVER_CONFIGURE
    cleanup = {"stopped": True}
    try:
        if receiver is None:
            receiver = InstalledReceiver(service, config_file)
        stage = FailureStage.RECEIVER_START
        ready = receiver.start()
        emit({"ok": True, "event": "receiver_ready", "data": ready})
        stage = FailureStage.RECEIVER_MONITOR
        while not stop_requested.wait(0.25):
            if time.monotonic() >= receiver.deadline:
                break
            receiver.check()
            if receiver.closing.is_set() or receiver.http.failure:
                failed = receiver.failed or "receiver_listener_failed"
                break
    except BaseException as error:
        failed = "receiver_refused"
        diagnostics.append(capture_failure(error, stage))
    finally:
        if receiver is not None:
            try:
                cleanup = receiver.stop()
            except BaseException as error:
                failed = failed or "receiver_cleanup_failed"
                cleanup = {"stopped": False}
                diagnostics.append(capture_failure(error, FailureStage.RECEIVER_STOP))
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    failed = failed or cleanup.get("failure")
    if not cleanup["stopped"]:
        failed = failed or "receiver_cleanup_failed"
    ok = failed is None
    result = {"ok": ok, "event": "receiver_stopped", "failure": failed, "cleanup": cleanup}
    if receiver is not None:
        diagnostics = [*getattr(receiver, "_diagnostics", ()), *diagnostics]
    if diagnostics:
        result["diagnostics"] = diagnostics
    emit(result)
    return 0 if ok else 1


def serve_config(config_file, session_file, emit):
    """Bound provider/configuration startup before any listener threads exist."""
    from ai_task_manager.config.service import load_service

    previous = signal.getsignal(signal.SIGALRM)
    timer = signal.getitimer(signal.ITIMER_REAL)

    def timeout(*_):
        raise TimeoutError("Receiver startup deadline")

    stage = FailureStage.RECEIVER_LOAD
    try:
        signal.signal(signal.SIGALRM, timeout)
        signal.setitimer(signal.ITIMER_REAL, 30)
        service = load_service(config_file, session_file)
        stage = FailureStage.RECEIVER_CONFIGURE
        receiver = InstalledReceiver(service, config_file)
    except BaseException as error:
        emit(
            {
                "ok": False,
                "event": "receiver_stopped",
                "failure": "receiver_startup_refused",
                "diagnostics": [capture_failure(error, stage)],
                "cleanup": {"stopped": True, "listener_started": False},
            }
        )
        return 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, *timer)
        signal.signal(signal.SIGALRM, previous)
    return serve(service, config_file, emit, receiver=receiver)
