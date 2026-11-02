"""Registered accounting capabilities; these are not task or host proofs."""

import math
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType

from ai_task_manager.domains.work.errors import AccessDenied


@dataclass(frozen=True)
class InvocationBinding:
    run_id: str
    policy_revision: int
    call_id: str
    worker_identity: str
    attempt_id: str
    host: str
    configuration_digest: str
    model: str
    effort: str
    purpose: str
    deadline: float

    def __post_init__(self):
        text = (
            self.run_id,
            self.call_id,
            self.worker_identity,
            self.attempt_id,
            self.configuration_digest,
            self.model,
            self.effort,
            self.purpose,
        )
        if (
            any(not isinstance(value, str) or not value or "\x00" in value for value in text)
            or type(self.policy_revision) is not int
            or self.policy_revision < 1
            or self.host not in {"claude", "codex"}
            or isinstance(self.deadline, bool)
            or not isinstance(self.deadline, (int, float))
            or not math.isfinite(self.deadline)
        ):
            raise AccessDenied("A complete registered invocation binding is required")

    @classmethod
    def observed(cls, state):
        """Decode a trusted controller observation, never a model-supplied receipt."""
        try:
            call = state["call"]
            scope = call["metadata"]["invocation_binding"]
            deadline = datetime.fromisoformat(call["invocation_bound"]["execution_deadline"])
            if deadline.tzinfo is None:
                raise ValueError("Naive invocation deadline")
            return cls(
                state["run_id"],
                state["policy_revision"],
                call["call_id"],
                call["worker_identity"],
                scope["attempt_id"],
                scope["host"],
                scope["configuration_digest"],
                scope["model"],
                scope["effort"],
                scope["purpose"],
                deadline.timestamp(),
            )
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise AccessDenied("The actual invocation binding is unavailable") from error


@dataclass(frozen=True)
class PreparedInvocation:
    issuer: object = field(repr=False)
    reference: str
    binding: InvocationBinding
    _key: object = field(repr=False)
    mode: str = "running"

    @contextmanager
    def effect_boundary(self):
        if not isinstance(self.issuer, InvocationPermits):
            raise AccessDenied("Invocation data alone cannot grant an effect")
        with self.issuer._effect(self):
            yield

    @contextmanager
    def launch_boundary(self):
        if not isinstance(self.issuer, InvocationPermits) or self.mode != "launch":
            raise AccessDenied("A registered claimed invocation is required")
        with self.issuer._launch(self) as record_started:
            yield record_started

    def running(self):
        if not isinstance(self.issuer, InvocationPermits):
            raise AccessDenied("The invocation authority is unavailable")
        return self.issuer.prepare(self.reference)

    def bounded_timeout(self, maximum, *, original_deadline, clock):
        values = (maximum, original_deadline)
        if (
            any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                for value in values
            )
            or maximum <= 0
        ):
            raise AccessDenied("A finite original operation bound is required")
        remaining = min(self.binding.deadline, original_deadline) - clock()
        if remaining <= 0:
            raise AccessDenied("The original invocation deadline expired")
        return min(maximum, remaining)


class InvocationPermits:
    """Trusted composition registers exact bindings on one injected controller.

    No tool exposes this constructor or its controller. Arbitrary same-user
    Python access is outside this capability boundary; binding data is no proof.
    """

    def __init__(self, controller, registered):
        if (
            not callable(getattr(controller, "snapshot", None))
            or not callable(getattr(controller, "effect_boundary", None))
            or not isinstance(registered, dict)
            or not registered
            or any(
                not isinstance(ref, str) or not ref or not isinstance(binding, InvocationBinding)
                for ref, binding in registered.items()
            )
        ):
            raise AccessDenied("Invocation references need trusted registered bindings")
        self.controller = self._bound_controller = controller
        self.registered = self._bound_registered = MappingProxyType(dict(registered))
        self._key = object()

    def _current_owner(self):
        if (
            self.controller is not self._bound_controller
            or self.registered is not self._bound_registered
        ):
            raise AccessDenied("The registered invocation authority changed")

    def prepare(self, reference):
        """Read full controller state before entering Root/model/Core locks."""
        return self._prepare(reference, "running")

    def prepare_launch(self, reference):
        return self._prepare(reference, "launching")

    def _prepare(self, reference, status):
        self._current_owner()
        expected = self.registered.get(reference)
        if expected is None:
            raise AccessDenied("The invocation reference is not registered")
        snapshot = self.controller.snapshot()
        call = next((row for row in snapshot["calls"] if row["call_id"] == expected.call_id), None)
        state = {
            "run_id": snapshot["run_id"],
            "policy_revision": snapshot["policy"]["revision"],
            "call": call,
        }
        if (
            call is None
            or call["status"] != status
            or InvocationBinding.observed(state) != expected
        ):
            raise AccessDenied("The registered invocation differs from its actual owner")
        return PreparedInvocation(
            self,
            reference,
            expected,
            self._key,
            "launch" if status == "launching" else "running",
        )

    def _validate(self, prepared):
        self._current_owner()
        if (
            prepared.issuer is not self
            or prepared._key is not self._key
            or self.registered.get(prepared.reference) is not prepared.binding
        ):
            raise AccessDenied("The prepared invocation belongs to another authority")

    @contextmanager
    def _launch(self, prepared):
        self._validate(prepared)
        with self.controller.launch_boundary(
            prepared.binding.call_id, worker_identity=prepared.binding.worker_identity
        ) as record_started:
            self._validate(prepared)
            yield record_started

    @contextmanager
    def _effect(self, prepared):
        self._validate(prepared)
        with self.controller.effect_boundary(
            prepared.binding.call_id, worker_identity=prepared.binding.worker_identity
        ) as state:
            self._current_owner()
            if InvocationBinding.observed(state) != prepared.binding:
                raise AccessDenied("The current invocation scope changed")
            yield


@dataclass(frozen=True)
class InitializationInvocation:
    native: PreparedInvocation
    relay: PreparedInvocation

    def __post_init__(self):
        if (
            not isinstance(self.native, PreparedInvocation)
            or not isinstance(self.relay, PreparedInvocation)
            or self.native.mode != "launch"
            or self.relay.mode != "launch"
            or self.native.binding.purpose != "host"
            or self.relay.binding.purpose != "relay"
            or self.native.issuer.controller is not self.relay.issuer.controller
        ):
            raise AccessDenied("Initialization needs exact host and relay invocations")
        common = (
            "run_id",
            "policy_revision",
            "worker_identity",
            "attempt_id",
            "host",
            "configuration_digest",
            "model",
            "effort",
        )
        if (
            any(
                getattr(self.native.binding, name) != getattr(self.relay.binding, name)
                for name in common
            )
            or self.relay.binding.deadline > self.native.binding.deadline
        ):
            raise AccessDenied("Relay and native invocation scopes differ")
        snapshot = self.native.issuer.controller.snapshot()
        calls = {row["call_id"]: row for row in snapshot["calls"]}
        host = calls[self.native.binding.call_id]
        relay = calls[self.relay.binding.call_id]
        if (
            not host["uses_worker"]
            or relay["uses_worker"]
            or relay["metadata"].get("parent_call_id") != self.native.binding.call_id
        ):
            raise AccessDenied("Relay preparation must use its counted host reservation")


def require_invocation(permit, policy, owner, purpose):
    """Validate a trusted producer result outside the final short boundary."""
    if not isinstance(permit, PreparedInvocation) or permit.mode != "launch":
        raise AccessDenied("A typed claimed invocation is required")
    binding = permit.binding
    if (
        (
            binding.attempt_id,
            binding.worker_identity,
            binding.host,
            binding.configuration_digest,
            binding.model,
            binding.effort,
            binding.purpose,
        )
        != (
            owner.attempt_id,
            owner.worker_id,
            owner.host,
            owner.configuration_digest,
            policy.model,
            policy.effort,
            purpose,
        )
        or binding.deadline > owner.deadline
        or permit.issuer.prepare_launch(permit.reference) != permit
    ):
        raise AccessDenied("Invocation does not belong to this exact native owner")
    return permit


def spawn_claimed(permit, argv, **options):
    """Create one child under Root; reconcile a post-Popen callback failure."""
    import subprocess

    from ai_task_manager.adapters.process.groups import OwnedProcessGroup

    process = None
    try:
        with permit.launch_boundary() as report:
            process = subprocess.Popen(argv, **options)
            report(process_pid=None, process_start_identity=None)
        return process
    except BaseException as error:
        if process is not None:
            error.owned_process = process
            try:
                group = OwnedProcessGroup(process)
                error.owned_group = group
                group.capture()
                before, leader = group.observe(), group.leader()
                stopped = group.stop()
                error.owned_stop_evidence = group.evidence(
                    stopped, orphaned=bool(leader.exited and before.live_members)
                )
                if stopped.confirmed:
                    group.reap_exited_leader()
            except BaseException as cleanup_error:
                error.owned_stop_error = type(cleanup_error).__name__
        raise
