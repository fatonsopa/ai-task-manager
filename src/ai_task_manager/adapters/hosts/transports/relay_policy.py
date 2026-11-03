"""One fixed, source-sealed build-control adapter for a trusted inherited binding.

This initial adapter serves local build/evaluation accounting. Unsupported policy
adapters refuse; model messages never select a Python module or source location.
"""

import hashlib
import importlib
import inspect
import json
import os
import stat
import sys
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

MAX_BINDING = 65536


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def command_record(command):
    return {"argv": list(command.argv), "cwd": str(command.cwd), "environment": command.environment}


def prepare_binding(permit, command):
    from ai_task_manager.adapters.hosts.bridge.invocations import PreparedInvocation

    if not isinstance(permit, PreparedInvocation) or permit.mode != "launch":
        raise ValueError("A claimed native invocation is required")
    permit.issuer._validate(permit)
    controller = permit.issuer.controller
    if (type(controller).__module__, type(controller).__name__) == (
        "ai_task_manager.installation.native.controller",
        "NormalInvocationController",
    ):
        from ai_task_manager.installation.native.relay import prepare

        return prepare(permit, command_record(command))
    if (type(controller).__module__, type(controller).__name__) != (
        "tools.build_support.ledger",
        "BuildControl",
    ):
        raise ValueError("No supported inherited policy adapter is configured")
    source = Path(inspect.getfile(type(controller))).resolve().parents[2]
    files = [source / "tools/build_control.py", *(source / "tools/build_support").glob("*.py")]
    hashes = {}
    for path in files:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
            raise ValueError("Trusted accounting source is unsafe")
        hashes[str(path.relative_to(source))] = hashlib.sha256(path.read_bytes()).hexdigest()
    ledger = Path(controller.ledger)
    info = ledger.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Trusted accounting ledger is unsafe")
    return {
        "kind": "build_control_v1",
        "source_root": str(source),
        "source_sha256": hashes,
        "ledger": str(ledger),
        "ledger_identity": [info.st_dev, info.st_ino],
        "binding": asdict(permit.binding),
        "command_digest": _digest(command_record(command)),
    }


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("Duplicate inherited binding field")
        value[key] = item
    return value


def read_spec(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Inherited specification needs a private descriptor")
    os.lseek(fd, 0, os.SEEK_SET)
    raw = os.read(fd, MAX_BINDING + 1)
    if len(raw) > MAX_BINDING:
        raise ValueError("Inherited specification is oversized")
    value = json.loads(
        raw,
        object_pairs_hook=_pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite inherited binding")),
    )
    if not isinstance(value, dict):
        raise ValueError("Inherited specification must be an object")
    return value


class RelayPolicy:
    def __new__(cls, specification, command):
        if specification.get("kind") == "normal_native_v1":
            from ai_task_manager.installation.native.relay import NormalRelayPolicy

            return NormalRelayPolicy(specification, command)
        return super().__new__(cls)

    def __init__(self, specification, command):
        if specification.get("kind") != "build_control_v1":
            raise ValueError("Unsupported inherited policy adapter")
        self.spec = specification
        self.source = Path(specification["source_root"])
        self._sources_current()
        sys.path.insert(0, str(self.source))
        # Module selection is fixed here, not read from a model/task payload.
        module = importlib.import_module("tools.build_control")
        from ai_task_manager.adapters.hosts.bridge.invocations import InvocationBinding

        self.binding = InvocationBinding(**specification["binding"])
        self.control = module.BuildControl(
            specification["ledger"], expected_policy_revision=self.binding.policy_revision
        )
        self.command = command
        self.check()

    def _sources_current(self):
        expected = self.spec["source_sha256"]
        required = {"tools/build_control.py", "tools/build_support/ledger.py"}
        if not isinstance(expected, dict) or not required <= set(expected):
            raise ValueError("Inherited policy source seal is incomplete")
        complete = {
            "tools/build_control.py",
            *(
                str(path.relative_to(self.source))
                for path in (self.source / "tools/build_support").glob("*.py")
            ),
        }
        if set(expected) != complete:
            raise ValueError("Inherited policy dependency set changed")
        for relative, digest in expected.items():
            path = self.source / relative
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError("Inherited policy source path is invalid")
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
                raise ValueError("Inherited policy source is unsafe")
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("Inherited policy source changed")
        ledger = Path(self.spec["ledger"])
        info = ledger.lstat()
        if (
            [info.st_dev, info.st_ino] != self.spec["ledger_identity"]
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("Inherited policy ledger identity changed")

    def check(self):
        from ai_task_manager.adapters.hosts.bridge.invocations import InvocationBinding

        self._sources_current()
        if _digest(self.command) != self.spec["command_digest"]:
            raise ValueError("Native command differs from its trusted binding")
        snapshot = self.control.snapshot()
        call = next(
            (row for row in snapshot["calls"] if row["call_id"] == self.binding.call_id), None
        )
        state = {
            "run_id": snapshot["run_id"],
            "policy_revision": snapshot["policy"]["revision"],
            "call": call,
        }
        if call is None or InvocationBinding.observed(state) != self.binding:
            raise ValueError("Native invocation scope changed")

    @contextmanager
    def actual_spawn(self):
        self.check()  # Full source/metadata reads remain outside the transaction.
        with self.control.launch_boundary(
            self.binding.call_id, worker_identity=self.binding.worker_identity
        ) as report:
            yield report

    def _child_call(self, call_id, statuses):
        from ai_task_manager.adapters.hosts.bridge.invocations import InvocationBinding

        snapshot = self.control.snapshot()
        call = next((row for row in snapshot["calls"] if row["call_id"] == call_id), None)
        state = {
            "run_id": snapshot["run_id"],
            "policy_revision": snapshot["policy"]["revision"],
            "call": call,
        }
        binding = InvocationBinding.observed(state)
        if (
            call["status"] not in statuses
            or call["metadata"].get("parent_call_id") != self.binding.call_id
            or binding.purpose not in {"neutral", "assignment"}
            or call["uses_worker"]
            or any(
                getattr(binding, name) != getattr(self.binding, name)
                for name in (
                    "run_id",
                    "policy_revision",
                    "worker_identity",
                    "attempt_id",
                    "host",
                    "configuration_digest",
                    "model",
                    "effort",
                )
            )
        ):
            raise ValueError("Active native invocation belongs to another host")
        return call, binding

    def invocation_deadline(self, call_id):
        _, binding = self._child_call(call_id, {"launching", "running"})
        return binding.deadline

    def completed(self, call_id, *, active_call=None):
        from ai_task_manager.domains.work.errors import AccessDenied

        if active_call is None or call_id != active_call:
            return False
        try:
            call, binding = self._child_call(call_id, {"succeeded"})
        except (AccessDenied, ValueError, KeyError, TypeError):
            return False
        return (
            call["outcome"] == "succeeded"
            and call["process_stopped"] is True
            and self.control.clock().timestamp() < binding.deadline
        )
