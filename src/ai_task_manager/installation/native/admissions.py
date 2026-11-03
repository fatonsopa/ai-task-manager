"""Normal launch permission and genuine owned observations have separate checks."""

from dataclasses import asdict

from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.hosts.bridge.policy import NativePolicy
from ai_task_manager.application.service.models import Unavailable
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied


class UnreleasedNative:
    def require_installation(self, manifest):
        raise Unavailable("Normal source-bound native conformance capture is not implemented")

    def require_native_conformance(self, *args):
        raise Unavailable("Normal source-bound native conformance capture is not implemented")


class NormalAdmissions:
    def __init__(self, manifest, store, permission, plan, controller, conformance=None):
        self.manifest, self.store, self.permission, self.plan, self.controller = (
            manifest,
            store,
            permission,
            plan,
            controller,
        )
        self.conformance = conformance or UnreleasedNative()
        self._bound = (manifest, store, permission, plan, controller, self.conformance)
        self._binding = None

    def check_admission(self, plan):
        if (
            any(
                a is not b
                for a, b in zip(
                    (
                        self.manifest,
                        self.store,
                        self.permission,
                        self.plan,
                        self.controller,
                        self.conformance,
                    ),
                    self._bound,
                )
            )
            or plan != self.plan
        ):
            raise AccessDenied("Normal native admission owner or finite plan changed")
        self.manifest.check()
        current = self.store.permission(self.permission["reference"])
        if digest(current) != digest(self.permission) or current["revoked"]:
            raise AccessDenied("Normal native permission changed or was revoked")
        if self.controller.clock().timestamp() >= min(plan.deadline, current["deadline"]):
            raise AccessDenied("Normal native permission expired")
        # Permission to attempt never proves actual source-bound native conformance.
        self.conformance.require_installation(self.manifest)

    def verify_policy(self, policy, binding, plan, controller):
        self.check_admission(plan)
        d, p = self.manifest.data, self.permission
        if (
            not isinstance(policy, NativePolicy)
            or controller is not self.controller
            or asdict(binding.scope) != p["scope"]
            or (
                policy.host,
                policy.model,
                policy.effort,
                str(policy.executable),
                policy.executable_sha256,
                policy.environment,
            )
            != (
                d["host"],
                d["model"],
                d["effort"],
                d["executable"],
                d["executable_sha256"],
                self.manifest.environment,
            )
            or binding.configuration_digest != policy.configuration_digest
            or binding.host != p["host"]
            or binding.deadline > p["deadline"]
            or self._binding is not None
            and self._binding != binding
        ):
            raise AccessDenied("Normal native policy, scope, account or binding changed")
        policy.check()
        self._binding = binding

    def verify_authenticated(self, policy, binding, journal):
        self.verify_policy(policy, binding, self.plan, self.controller)
        if not isinstance(journal, HostJournal):
            raise AccessDenied("Actual owned native journal is required")
        expected = asdict(binding)
        startup = None
        for stored in reversed(journal._history("startup")):
            if all(stored["payload"].get(k) == value for k, value in expected.items()):
                startup = journal._read(stored["ref"], "startup")
                break
        if startup is None or not 0 <= journal.clock() - startup["observed_at"] <= 60:
            raise AccessDenied("Fresh exact owned native startup is unavailable")
        observed, pending = [], list(startup["sources"])
        seen = set()
        while pending:
            ref = pending.pop()
            if ref in seen:
                continue
            seen.add(ref)
            if len(seen) > 64:
                raise AccessDenied("Native authentication source chain exceeds its bound")
            record = journal._read(ref)
            if not 0 <= journal.clock() - record["observed_at"] <= 60:
                raise AccessDenied("Native authentication source is stale")
            observed.append(record["payload"])
            pending.extend(record["sources"])
        d = self.manifest.data
        if binding.host == "claude":
            identity = any(
                p.get("attempt_id") == binding.attempt_id
                and p.get("worker_id") == binding.worker_id
                and p.get("configuration_digest") == binding.configuration_digest
                and p.get("process", {}).get("pid") == startup["payload"]["process_id"]
                and p.get("process", {}).get("start_identity")
                == startup["payload"]["process_start_identity"]
                and p.get("frame", {}).get("type") == "system"
                and p.get("frame", {}).get("subtype") == "init"
                and p.get("frame", {}).get("apiKeySource") == "none"
                for p in observed
            )
            account = identity and any(
                p.get("auth_method") == "claude.ai"
                and p.get("account_hash") == d["account_hash"]
                and p.get("configuration_digest") == binding.configuration_digest
                and p.get("owned_group", {}).get("confirmed_stop") is True
                for p in observed
            )
        else:
            status = any(
                p.get("effect") == "codex_existing_auth"
                and p.get("auth_method") == "chatgpt"
                and all(p.get(k) == v for k, v in expected.items())
                and p.get("source_fingerprint") == policy.codex_settings.fingerprint
                and p.get("executable_hash") == policy.executable_sha256
                and p.get("group", {}).get("confirmed_stop") is True
                for p in observed
            )
            account = status and any(
                p.get("attempt_id") == binding.attempt_id
                and p.get("worker_id") == binding.worker_id
                and p.get("configuration_digest") == binding.configuration_digest
                and p.get("process", {}).get("pid") == startup["payload"]["process_id"]
                and p.get("process", {}).get("start_identity")
                == startup["payload"]["process_start_identity"]
                and (account := p.get("frame", {}).get("result", {}).get("account"))
                and account.get("type") == "chatgpt"
                and digest(account) == d["account_hash"]
                for p in observed
            )
        if not account:
            raise AccessDenied("Actual matching subscription account sources are unavailable")

    def require_native_conformance(self, policy, binding, journal):
        self.verify_policy(policy, binding, self.plan, self.controller)
        return self.conformance.require_native_conformance(self.manifest, policy, binding, journal)
