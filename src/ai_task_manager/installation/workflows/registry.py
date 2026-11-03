"""Installer-held capabilities; JSON can select a reference, never manufacture one."""

from dataclasses import dataclass
from types import MappingProxyType

from ai_task_manager.adapters.hosts.bridge.admission import NativeSourcePins
from ai_task_manager.adapters.hosts.bridge.policy import NativeProfile
from ai_task_manager.application.service.models import Unavailable
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied


@dataclass(frozen=True)
class NativeAuthorities:
    policy_builder: object
    launch_permits: object
    model_accounting: object
    accounting_ref: str
    decision_preparation: object = None

    def require(self):
        required = (
            (self.policy_builder, None),
            (self.launch_permits, "check_admission"),
            (self.launch_permits, "authorize_initialization"),
            (self.model_accounting, "authorize"),
            (self.model_accounting, "record"),
        )
        if not self.accounting_ref or any(
            not callable(v if name is None else getattr(v, name, None)) for v, name in required
        ):
            raise Unavailable("Real native launch/accounting capabilities are required")


@dataclass(frozen=True)
class NativeRegistration:
    reference: str
    host: str
    profile: NativeProfile
    account_hash: str
    source: NativeSourcePins
    prepare: object

    def __post_init__(self):
        if (
            type(self.reference) is not str
            or not self.reference
            or self.host not in {"claude", "codex"}
            or not isinstance(self.profile, NativeProfile)
            or not isinstance(self.source, NativeSourcePins)
            or not callable(self.prepare)
            or type(self.account_hash) is not str
            or len(self.account_hash) != 64
            or any(c not in "0123456789abcdef" for c in self.account_hash)
        ):
            raise AccessDenied("Explicit installed native source, model and account required")

    @property
    def fingerprint(self):
        return digest(
            {
                "reference": self.reference,
                "host": self.host,
                "model": self.profile.model,
                "effort": self.profile.effort,
                "account": self.account_hash,
                "source": self.source.fingerprint,
            }
        )

    def check(self):
        self.source.check()


class NativeRegistry:
    def __init__(self, registrations=(), *, service_options=None):
        values = {r.reference: r for r in registrations if type(r) is NativeRegistration}
        if len(values) != len(registrations):
            raise AccessDenied("Distinct typed native registrations are required")
        self.values = MappingProxyType(values)
        self.service_options = MappingProxyType(dict(service_options or {}))

    def require(self, reference):
        if reference not in self.values:
            raise Unavailable("The trusted installed native registration is unavailable")
        value = self.values[reference]
        value.check()
        return value
