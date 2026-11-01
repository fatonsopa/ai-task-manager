"""Trusted adapters resolve opaque proof references; callers cannot assert approval."""

from typing import Protocol

from .errors import AccessDenied, ApprovalRequired, EvidenceRequired
from .models import Approval, Evidence, RenderedProse, Scope, TaskProse


class PrincipalVerifier(Protocol):
    def verify_principal(self, proof_ref: str) -> Scope: ...


class ApprovalVerifier(Protocol):
    def verify_approval(self, proof_ref: str) -> Approval: ...


class EvidenceVerifier(Protocol):
    def verify_evidence(self, proof_ref: str) -> Evidence: ...


class ProseRenderer(Protocol):
    def render(self, prose: TaskProse) -> RenderedProse: ...


class ExactRenderer:
    def render(self, prose: TaskProse) -> RenderedProse:
        return RenderedProse(prose.title, prose.body, "exact-v1")


class RejectProofs:
    def verify_principal(self, proof_ref: str) -> Scope:
        raise AccessDenied("A trusted principal binding is required")

    def verify_approval(self, proof_ref: str) -> Approval:
        raise ApprovalRequired("A trusted prose approval is required")

    def verify_evidence(self, proof_ref: str) -> Evidence:
        raise EvidenceRequired("A trusted evidence reference is required")
