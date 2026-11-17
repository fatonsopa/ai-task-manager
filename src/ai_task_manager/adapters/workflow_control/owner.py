"""A finite serial local owner retains actual runtime handles across client exits."""

import hmac
import math
import os
import secrets
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from threading import BoundedSemaphore, Lock

from ai_task_manager.application.service.models import SCHEMAS
from ai_task_manager.config.files import FileSeal, read_private, write_private
from ai_task_manager.domains.work.errors import AccessDenied

from .identity import (
    canonical,
    endpoint_binding,
    file_binding,
    process_start,
    receive,
    settings,
    signature,
    source_digest,
)


class WorkflowOwner:
    def __init__(
        self, service, config_file, *, deadline, clock=time.time, installation=None, recovery=None
    ):
        if type(deadline) not in {int, float} or not math.isfinite(deadline) or deadline <= clock():
            raise AccessDenied("The owner requires an original finite future deadline")
        self.installation = installation
        self.service, self.config_file, self.deadline, self.clock = (
            service,
            Path(config_file).absolute(),
            deadline,
            clock,
        )
        self.seal = FileSeal(config_file)
        self.config = read_private(config_file)
        self.settings = settings(self.config.get("workflow_control"))
        self.recovery = recovery
        if recovery is not None:
            from ai_task_manager.installation.workflows.cold import ColdRecovery

            if type(recovery) is not ColdRecovery:
                raise AccessDenied("A current original-owner recovery capability is required")
            recovery.authorize(service)
            recovery.release_claim(service.workflows.driver.store, service.scope)
            self.settings = recovery.successor
        if str(service.core.context.db.path) != self.config["core_database"]:
            raise AccessDenied("Owner Core does not match its sealed claim store")
        from ai_task_manager.application.workflows.service import WorkflowService

        if not isinstance(service.workflows, WorkflowService) or service.claim_store is None:
            raise AccessDenied("A composed local workflow and enrolled claim store are required")
        service.claim_store.check()
        service.workflows.check_owner_deadline(deadline)
        self.scope = asdict(service.scope)
        self.key = secrets.token_hex(32)
        self.source = source_digest()
        self.seen = set()
        self._auth_lock = Lock()

    def serve(self, stop=None, ready=None):
        from ai_task_manager.installation.sessions.recovery import setup_lock

        # All trusted owner callers share this nonblocking admission/lifetime lock.
        # No Core transaction is held while serving or waiting for native work.
        with setup_lock(self.config_file):
            return self._serve(stop, ready)

    def _recovery_witness(self, installation, service):
        from .recovery import physical

        installation.check()
        store = service.workflows.driver.store
        witness = {
            "installation": file_binding(installation.seal.path),
            "workflow": dict(physical(store.path), store_id=store.store_id),
            "owner": {"pid": os.getpid(), "start": process_start(os.getpid())},
        }
        root = Path(installation.data["state_root"])
        candidates = {
            "core": self.config["core_database"],
            "workflow": store.path,
            "runtime": root / "runtime.sqlite",
            "host": root / "host.sqlite",
            "runtime_key": root / "runtime.key",
            "host_key": root / "host.key",
            "review_key": root / "review.key",
            "session": self.config["session_file"],
            "authority": self.config["authority_database"],
            "authority_key": self.config["authority_key"],
        }
        if self.config.get("linear"):
            candidates["provider"] = self.config["linear"]["journal"]
            candidates["dependency"] = self.config["linear"]["journal"] + ".dependencies.sqlite"
            candidates["dependency_key"] = (
                self.config["linear"]["journal"] + ".dependencies.key"
            )
        if self.config.get("writer"):
            candidates["writer"] = self.config["writer"]["journal"]
            candidates["writer_key"] = self.config["writer"]["key"]
        if all(Path(p).exists() for p in candidates.values()):
            witness["files"] = {
                k: (file_binding(p) if k.endswith("_key") or k == "session" else physical(p))
                for k, p in candidates.items()
            }
        return witness

    def _serve(self, stop=None, ready=None):
        path = Path(self.settings["endpoint"])
        record = Path(self.settings["record"])
        if path.exists() or path.is_symlink() or record.exists() or record.is_symlink():
            raise AccessDenied("Existing owner state requires verified recovery; no replacement")
        if path.parent.resolve() != path.parent or path.parent.stat().st_mode & 0o077:
            raise AccessDenied("Owner directory must be private and physical")
        from ai_task_manager.adapters.decisions.observations import bind_record
        from ai_task_manager.installation.workflows.bootstrap import activation_reference

        activation = {
            "config": file_binding(self.config_file),
            "scope": self.scope,
            "instance": self.settings["instance"],
            "source": self.source,
            "deadline": self.deadline,
        }
        if self.installation is not None:
            activation["recovery"] = self._recovery_witness(self.installation, self.service)
            members = getattr(self, "member_installations", ())
            if members:
                activation["recovery"]["members"] = [
                    self._recovery_witness(member.spec, member.service) for member in members
                ]
        bind_record(
            self.service.decisions.journal,
            activation_reference(self.config_file, self.settings),
            activation,
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            path.chmod(0o600)
            endpoint = endpoint_binding(path)
            descriptor = {
                "version": 1,
                "instance": self.settings["instance"],
                "scope": self.scope,
                "config": file_binding(self.config_file),
                "source": self.source,
                "pid": os.getpid(),
                "start": process_start(os.getpid()),
                "endpoint": endpoint,
                "key": self.key,
            }
            write_private(record, descriptor)
            listener.listen(8)
            listener.settimeout(0.1)
            if self.recovery is not None:
                self.recovery.publish(self.service, descriptor)
            if ready:
                ready()
            slots = BoundedSemaphore(4)
            try:
                with ThreadPoolExecutor(max_workers=4, thread_name_prefix="workflow-owner") as pool:
                    while self.clock() < self.deadline and not (stop and stop.is_set()):
                        try:
                            stream, _ = listener.accept()
                        except TimeoutError:
                            continue
                        if not slots.acquire(blocking=False):
                            stream.close()
                            continue
                        pool.submit(self._handle, stream, slots, path, endpoint)
            finally:
                self.shutdown_result = self.service.workflows.close()
                if path.exists() and endpoint_binding(path) == endpoint:
                    path.unlink()

    def _handle(self, stream, slots, path, endpoint):
        try:
            with stream:
                stream.settimeout(max(0.01, min(120, self.deadline - self.clock())))
                try:
                    request = receive(stream)
                    if type(request) is not dict:
                        raise AccessDenied("A local request object is required")
                    signed = {k: v for k, v in request.items() if k != "mac"}
                    if (
                        set(request) != {"nonce", "operation", "data", "scope", "instance", "mac"}
                        or type(request["mac"]) is not str
                        or not hmac.compare_digest(request["mac"], signature(self.key, signed))
                        or request["scope"] != self.scope
                        or request["instance"] != self.settings["instance"]
                        or type(request["nonce"]) is not str
                        or len(request["nonce"]) != 64
                        or request["operation"] not in SCHEMAS
                    ):
                        raise AccessDenied("Local request authentication or scope refused")
                    with self._auth_lock:
                        if request["nonce"] in self.seen or len(self.seen) >= 4096:
                            raise AccessDenied("Replayed request or owner nonce bound reached")
                        self.seen.add(request["nonce"])
                    self.seal.check()
                    if source_digest() != self.source or endpoint_binding(path) != endpoint:
                        raise AccessDenied("Owner source or endpoint changed")
                    envelope = self.service.call(request["operation"], request["data"])
                    body = {"nonce": request["nonce"], "envelope": envelope}
                    stream.sendall(canonical(dict(body, mac=signature(self.key, body))) + b"\n")
                except (AccessDenied, OSError, ValueError, TypeError):
                    # Lost replies do not cancel or repeat physical effects.
                    return
        finally:
            slots.release()
