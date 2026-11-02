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

    @contextmanager
    def effect_boundary(self):
        if not isinstance(self.issuer, InvocationPermits):
            raise AccessDenied("Invocation data alone cannot grant an effect")
        with self.issuer._effect(self):
            yield

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
            or call["status"] != "running"
            or InvocationBinding.observed(state) != expected
        ):
            raise AccessDenied("The registered invocation differs from its actual owner")
        return PreparedInvocation(self, reference, expected, self._key)

    @contextmanager
    def _effect(self, prepared):
        self._current_owner()
        if (
            prepared.issuer is not self
            or prepared._key is not self._key
            or self.registered.get(prepared.reference) is not prepared.binding
        ):
            raise AccessDenied("The prepared invocation belongs to another authority")
        with self.controller.effect_boundary(
            prepared.binding.call_id, worker_identity=prepared.binding.worker_identity
        ) as state:
            self._current_owner()
            if InvocationBinding.observed(state) != prepared.binding:
                raise AccessDenied("The current invocation scope changed")
            yield
