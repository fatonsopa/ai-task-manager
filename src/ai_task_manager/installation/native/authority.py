"""Permission to attempt launch is distinct from observed auth/conformance/billing."""

from dataclasses import dataclass
from typing import Protocol

from ai_task_manager.adapters.hosts.bridge.accounting import AccountingPlan
from ai_task_manager.application.service.models import Unavailable
from ai_task_manager.domains.work.errors import AccessDenied


@dataclass(frozen=True)
class NativePermission:
    reference: str
    manifest_digest: str
    controller: object
    admissions: object
    plan: AccountingPlan
    decision_preparation: object = None

    def check(self, manifest, task_id, deadline):
        d = manifest.data
        if (
            self.reference != d["authorization_ref"]
            or self.manifest_digest != manifest.digest
            or type(self.plan) is not AccountingPlan
            or (self.plan.task_id, self.plan.host, self.plan.model, self.plan.effort)
            != (task_id, d["host"], d["model"], d["effort"])
            or self.plan.deadline > deadline
            or not callable(getattr(self.controller, "snapshot", None))
            or any(
                not callable(getattr(self.admissions, n, None))
                for n in (
                    "check_admission",
                    "verify_policy",
                    "verify_authenticated",
                    "require_native_conformance",
                )
            )
        ):
            raise AccessDenied("Current normal launch permission differs from native intent")
        # Existing authority must establish restrictions and billing eligibility;
        # neither this record nor the manifest claims actual native startup.
        self.admissions.check_admission(self.plan)


class MissingNativeAuthority:
    def resolve(self, reference, manifest):
        raise Unavailable(
            "Normal native permission/accounting/admission supplier is not implemented"
        )


class NativeAuthoritySource(Protocol):
    """Installer-supplied code, never selected by manifest import or callable names.

    The normal supplier must bind a finite controller reservation, current scope,
    intended account and billing eligibility to the exact manifest. Its admissions
    capability must separately require actual source-bound startup, account and
    conformance observations. A launch permission alone satisfies none of those.
    """

    def resolve(self, reference: str, manifest) -> NativePermission: ...
