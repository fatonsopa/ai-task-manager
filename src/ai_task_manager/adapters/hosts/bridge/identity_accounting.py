"""Consume exact finite auth-refresh names from the existing accounting owner."""

from threading import Event

from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import AttemptState


class IdentityAuthPermits:
    def __init__(self, accounting, native):
        self.accounting, self.native = accounting, native
        self._original = (accounting, native, accounting.plan.identity_refreshes)
        self.observer = self._bound_observer = None
        self._next = 0
        self._pending = None
        self.cancelled = Event()

    def bind(self, observer):
        if self.observer is not None or observer.native is not self.native:
            raise AccessDenied("Identity accounting cannot be rebound")
        self.observer = self._bound_observer = observer

    def authorize_auth(self, policy, binding):
        a = self.accounting
        if (
            (a, self.native, a.plan.identity_refreshes) != self._original
            or self.observer is None or self.observer is not self._bound_observer
            or policy is not self.native.policy or binding is not self.native.binding
            or self._pending is not None or self._next >= len(a.plan.identity_refreshes)
        ):
            raise AccessDenied("The finite identity invocation owner is unavailable")
        attempt = a._owner(policy, binding)
        if attempt.state == AttemptState.RUNNING:
            a.core.execution.validate_work_lease(a.handle, attempt.id, attempt.version)
        elif attempt.state not in {AttemptState.RESERVED, AttemptState.STARTING}:
            raise AccessDenied("Stopped execution cannot authenticate itself back to work")
        current = self.observer.prepare().observation
        label = a.plan.identity_refreshes[self._next]
        self._next += 1
        self._pending = a._claim(
            policy, binding, "auth", a.plan.auth_seconds, reference=label,
            parent=a._initialization.native.reference, not_after=current.expires_at,
        )
        return self._pending

    def record_auth(self, binding, completion):
        if self._pending is None or binding is not self.native.binding:
            raise AccessDenied("No exact identity observation invocation is pending")
        self.accounting._record_auth_call(
            self._pending.reference, completion, cancelled=self.cancelled
        )
        self._pending = None
