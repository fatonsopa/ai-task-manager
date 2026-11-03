"""Closed installed normal-controller adapter for the inherited native relay."""

import hashlib
import json
import os
import stat
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from ai_task_manager.adapters.hosts.bridge.invocations import InvocationBinding
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied

from .controller import NormalInvocationController
from .store import NativeStore


def package_root():
    return Path(__file__).resolve().parents[3]


def product_digest(root):
    package = root / "ai_task_manager"
    values = {}
    for path in sorted(package.rglob("*")):
        if path.suffix not in {".py", ".json"}:
            continue
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o022
            or info.st_uid not in {0, os.getuid()}
        ):
            raise AccessDenied("Installed normal relay source is unsafe")
        values[str(path.relative_to(package))] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not values:
        raise AccessDenied("Installed product source is unavailable")
    return digest(values)


def command_digest(command):
    return hashlib.sha256(json.dumps(command, sort_keys=True, allow_nan=False).encode()).hexdigest()


def prepare(permit, command):
    c = permit.issuer.controller
    if type(c) is not NormalInvocationController:
        raise AccessDenied("Exact installed normal invocation controller required")
    permit.issuer._validate(permit)
    return {
        "kind": "normal_native_v1",
        "source_root": str(package_root()),
        "source_digest": product_digest(package_root()),
        "store": c.store.pointer,
        "permission_ref": c.reference,
        "permission_digest": digest(c.permission),
        "binding": asdict(permit.binding),
        "command_digest": command_digest(command),
    }


class NormalRelayPolicy:
    def __init__(self, spec, command):
        fields = {
            "kind",
            "source_root",
            "source_digest",
            "store",
            "permission_ref",
            "permission_digest",
            "binding",
            "command_digest",
        }
        if type(spec) is not dict or set(spec) != fields or spec["kind"] != "normal_native_v1":
            raise AccessDenied("Closed normal native inherited binding required")
        if Path(spec["source_root"]) != package_root():
            raise AccessDenied("Inherited normal source differs from the installed relay")
        self.spec, self.command = spec, command
        self.binding = InvocationBinding(**spec["binding"])
        self._sources_current()
        store = NativeStore(spec["store"]["path"], spec["store"])
        p = store.permission(spec["permission_ref"])
        if digest(p) != spec["permission_digest"]:
            raise AccessDenied("Inherited normal permission changed")
        self.control = NormalInvocationController(store, p)
        self.check()

    def _sources_current(self):
        if product_digest(package_root()) != self.spec["source_digest"]:
            raise AccessDenied("Inherited installed product source changed")
        if command_digest(self.command) != self.spec["command_digest"]:
            raise AccessDenied("Inherited native command changed")

    def _observe(self, call_id, statuses):
        self._sources_current()
        c, b = self.control, self.binding
        with c.store.transaction() as conn:
            row = c.store.read(conn, "native_calls", call_id)
            if row is None or row["status"] not in statuses:
                raise AccessDenied("Inherited native invocation is unavailable")
            row = c._owned(conn, call_id, b.worker_identity, row["status"])
            observed = InvocationBinding.observed(
                {"run_id": c.store.binding.store_id, "policy_revision": 1, "call": row}
            )
        return row, observed

    def check(self):
        _, observed = self._observe(self.binding.call_id, {"launching", "running"})
        if observed != self.binding:
            raise AccessDenied("Inherited native invocation owner changed")

    @contextmanager
    def actual_spawn(self):
        self.check()
        with self.control.launch_boundary(
            self.binding.call_id, worker_identity=self.binding.worker_identity
        ) as report:
            yield report

    def _child(self, call_id, statuses):
        row, child = self._observe(call_id, statuses)
        if (
            row["metadata"].get("parent_call_id") != self.binding.call_id
            or row["uses_worker"]
            or child.purpose not in {"neutral", "assignment"}
            or any(
                getattr(child, key) != getattr(self.binding, key)
                for key in [
                    "run_id",
                    "policy_revision",
                    "worker_identity",
                    "attempt_id",
                    "host",
                    "configuration_digest",
                    "model",
                    "effort",
                ]
            )
        ):
            raise AccessDenied("Inherited child invocation belongs to another native host")
        return row, child

    def invocation_deadline(self, call_id):
        return self._child(call_id, {"launching", "running"})[1].deadline

    def completed(self, call_id, *, active_call=None):
        if call_id != active_call or active_call is None:
            return False
        try:
            row, child = self._child(call_id, {"succeeded"})
            return (
                row["outcome"] == "succeeded"
                and row["process_stopped"] is True
                and self.control.clock().timestamp() < child.deadline
            )
        except (AccessDenied, KeyError, ValueError, TypeError):
            return False
