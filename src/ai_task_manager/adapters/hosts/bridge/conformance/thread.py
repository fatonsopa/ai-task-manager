"""Probe-only thread creation is never a normal conformance receipt."""

from ai_task_manager.domains.work.errors import AccessDenied

from .canary import CanaryGateway
from .models import ProbeContext


class ProbeThreadGate:
    def __init__(self, context, authority, gateway):
        if (
            not isinstance(context, ProbeContext)
            or not isinstance(gateway, CanaryGateway)
            or gateway.context is not context
            or gateway.authority is not authority
        ):
            raise AccessDenied("An exact enrolled canary probe is required")
        self.context, self.authority, self.gateway = context, authority, gateway
        self._bound = (context, authority, gateway)

    def authorize(self, policy, binding):
        c = self.context
        if (
            any(
                a is not b
                for a, b in zip((c, self.authority, self.gateway), self._bound, strict=True)
            )
            or self.gateway.context is not c
            or self.gateway.authority is not self.authority
            or self.gateway.native is not None
            or self.gateway.case is not None
            or (policy.host, policy.model, policy.effort) != (c.host, c.model, c.effort)
            or binding.host != c.host
            or binding.scope != c.scope
            or binding.deadline > c.deadline
        ):
            raise AccessDenied("Probe thread authority or scope changed")
        self.authority.require_current(c, policy=policy, binding=binding, phase="thread")
