"""Restricted startup observations are required facts, not full workflow conformance."""

import hashlib
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied

from .gateway import SCHEMAS, tool_schema
from .policy import NativePolicy

MANAGED_TOOLS = frozenset("mcp__aitm__" + name for name in SCHEMAS)


class NativeSourcePins:
    """Installer-selected executable dependencies; never discover credential files."""

    def __init__(self, paths_sha256):
        if not isinstance(paths_sha256, dict) or not paths_sha256:
            raise AccessDenied("Reviewed native source dependencies are required")
        self._pins = tuple(sorted((str(Path(p).absolute()), h) for p, h in paths_sha256.items()))
        self._original_entries = self._pins
        self.check()

    @property
    def fingerprint(self):
        return digest(self._pins)

    def check(self):
        if self._pins is not self._original_entries:
            raise AccessDenied("The original reviewed native dependency set changed")
        for name, expected in self._pins:
            path = Path(name)
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o022
                or not isinstance(expected, str)
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected
            ):
                raise AccessDenied("Reviewed native source changed or is unsafe")


def launch_template(policy):
    """The owned working directory is separately bound by each actual startup."""
    from ai_task_manager.adapters.hosts.transports.claude import sdk_command, sdk_options

    if not isinstance(policy, NativePolicy) or policy.host != "claude":
        raise AccessDenied("Only the pinned Claude managed launch is supported here")
    if set(policy.allowed_tools) != MANAGED_TOOLS:
        raise AccessDenied("The managed launch must expose exactly its registered tools")
    gateway = type("SchemaOnlyGateway", (), {"schemas": {n: tool_schema(n) for n in SCHEMAS}})()
    options = sdk_options(policy, gateway)
    command = sdk_command(policy, options, 1)
    return {
        "host": policy.host,
        "executable": str(policy.executable),
        "executable_sha256": policy.executable_sha256,
        "environment": dict(policy.environment),
        "argv": list(command.argv),
        "model": policy.model,
        "effort": policy.effort,
        "seals": [(str(s.path), s.sha256) for s in policy.seals],
    }


class LaunchTemplate:
    def __init__(self, policy, pins):
        if not isinstance(pins, NativeSourcePins):
            raise AccessDenied("The launch template needs reviewed source pins")
        self.pins = self._original_pins = pins
        self._value = launch_template(policy)
        self.fingerprint = digest({"launch": self._value, "source": pins.fingerprint})
        self._original_fingerprint = self.fingerprint

    def check(self, policy):
        if (
            self.pins is not self._original_pins
            or self.fingerprint != self._original_fingerprint
            or digest({"launch": self._value, "source": self.pins.fingerprint})
            != self._original_fingerprint
        ):
            raise AccessDenied("The native source owner changed")
        self.pins.check()
        if launch_template(policy) != self._value:
            raise AccessDenied("Actual launch differs from the reviewed native template")


@dataclass(frozen=True)
class _ObservedStartup:
    ref: str
    ready_ref: str
    native: object
    payload_digest: str
    process: object
    identity: object


class NativeStartupObservations:
    """Only actual initialization on the owned object can enter this registry.

    Test processes exercise this same boundary with their actual fixture source
    hashes. They do not establish execution by an official CLI or model.
    """

    def __init__(self, journal, template, expected_account):
        if not isinstance(template, LaunchTemplate) or not expected_account:
            raise AccessDenied("A reviewed launch and intended account are required")
        self.journal, self.template, self.expected_account = journal, template, expected_account
        self._bound = (journal, template, expected_account)
        self._observed = {}
        self._by_ready = {}

    def _current(self):
        if (
            self.journal is not self._bound[0]
            or self.template is not self._bound[1]
            or self.expected_account != self._bound[2]
        ):
            raise AccessDenied("The native observation owner changed")

    def _inspect(self, native, ready_ref):
        from .bootstrap import BootstrapPermits
        from .controller import ManagedNativeHost
        from .startup import StartupObserver

        self._current()
        if (
            not isinstance(native, ManagedNativeHost)
            or not isinstance(native.permits, BootstrapPermits)
            or native.journal is not self.journal
            or native.core is not None
            or native.resources is not None
            or native.gateway.delegate is not None
            or not isinstance(native.startup, StartupObserver)
            or native.startup.admitted
            or native.startup.ready_ref != ready_ref
            or native.startup.initialization_tool_calls
        ):
            raise AccessDenied("Only an actual diagnostic startup can supply these observations")
        self.template.check(native.policy)
        native.permits.authorization.require_bootstrap(native.policy, native.binding)
        identity = native.verify_active()
        record = self.journal._read(ready_ref, "startup")
        data = record["payload"]
        if (
            any(data.get(k) != value for k, value in asdict(native.binding).items())
            or data["process_id"] != identity.pid
            or data["process_start_identity"] != identity.start_identity
            or data["model"] != native.policy.model
            or data["requested_effort"] != native.policy.effort
            or data["observed_effort"] is not None
        ):
            raise AccessDenied("Diagnostic startup does not match its actual owner/profile")
        frames = []
        auth = []
        for source in record["sources"]:
            payload = self.journal._read(source)["payload"]
            if payload.get("auth_method"):
                auth.append(payload)
            frame = payload.get("frame", {})
            if frame.get("type") == "system" and frame.get("subtype") == "init":
                frames.append(frame)
        if len(auth) != 1 or len(frames) != 1:
            raise AccessDenied("Actual native auth and init sources are both required")
        frame, account = frames[0], auth[0]
        if (
            account.get("auth_method") != "claude.ai"
            or account.get("account_hash") != self.expected_account
            or account.get("configuration_digest") != native.binding.configuration_digest
            or frame.get("apiKeySource") != "none"
            or frame.get("claude_code_version") != "2.1.301"
            or frame.get("permissionMode") != "dontAsk"
            or set(frame.get("tools", [])) != MANAGED_TOOLS
            or frame.get("mcp_servers")
            != [{"name": "aitm", "source": "sdk", "status": "connected"}]
            or frame.get("cwd") != str(native.policy.cwd)
            or frame.get("session_id") != data["session_id"]
        ):
            raise AccessDenied("Restricted authenticated native startup was not observed")
        return record, identity, frame

    def capture(self, native, ready_ref):
        if ready_ref in self._by_ready:
            raise AccessDenied("The original startup cannot be reused or renewed")
        record, identity, frame = self._inspect(native, ready_ref)
        payload = {
            "effect": "restricted_native_startup_observed",
            "startup_ref": ready_ref,
            "binding": asdict(native.binding),
            "process": asdict(identity),
            "template": self.template.fingerprint,
            "model": frame["model"],
            "requested_effort": native.policy.effort,
            "observed_effort": None,
            "tools": sorted(frame["tools"]),
            "mcp_servers": frame["mcp_servers"],
            "account_hash": self.expected_account,
            "full_workflow_conformance": "not_established",
        }
        ref = self.journal._append(
            "trace", payload, sources=(ready_ref,), expires_at=record["expires_at"]
        )
        self._observed[ref] = _ObservedStartup(
            ref, ready_ref, native, digest(payload), native.process, identity
        )
        self._by_ready[ready_ref] = ref
        return ref

    def verify(self, ref):
        self._current()
        original = self._observed.get(ref)
        if original is None:
            raise AccessDenied("Startup data alone is not an observed diagnostic capability")
        stored = self.journal._read(ref, "trace")
        _, identity, _ = self._inspect(original.native, original.ready_ref)
        if (
            digest(stored["payload"]) != original.payload_digest
            or original.native.process is not original.process
            or identity != original.identity
        ):
            raise AccessDenied("The original diagnostic process or observation changed")
        return stored


class NativeAdmissionVerifier:
    """Combine actual restricted startup with a purpose-limited evaluation grant."""

    def __init__(self, observations, bootstrap_ref, grant):
        from .evaluation_enrollment import EvaluationGrant

        if (
            not isinstance(observations, NativeStartupObservations)
            or not isinstance(grant, EvaluationGrant)
            or grant.observations is not observations
            or grant.data["bootstrap_ref"] != bootstrap_ref
        ):
            raise AccessDenied("The actual native observation owner is required")
        self.observations, self.bootstrap_ref, self.grant = observations, bootstrap_ref, grant
        self._bound = (observations, bootstrap_ref, grant)

    def check_admission(self, plan):
        if (
            self.observations is not self._bound[0]
            or self.bootstrap_ref != self._bound[1]
            or self.grant is not self._bound[2]
        ):
            raise AccessDenied("Native admission composition changed")
        observed = self.observations.verify(self.bootstrap_ref)
        self.grant.require_worker(plan, observed)

    def verify_policy(self, policy, binding, plan, controller):
        self.check_admission(plan)
        original = self.observations._observed[self.bootstrap_ref].native
        if controller is not original.permits.initialization.native.issuer.controller:
            raise AccessDenied("The worker uses another root accounting authority")
        self.observations.template.check(policy)
        self.grant.require_binding(binding)

    def verify_authenticated(self, policy, binding, journal):
        self.observations.template.check(policy)
        self.grant.require_binding(binding)
        if journal is not self.observations.journal:
            raise AccessDenied("The worker authentication journal changed")
        for stored in reversed(journal._history("trace")):
            payload = stored["payload"]
            if (
                payload.get("configuration_digest") == binding.configuration_digest
                and payload.get("auth_method") == "claude.ai"
            ):
                journal._read(stored["ref"], "trace")
                if payload.get("account_hash") == self.observations.expected_account:
                    return
                break
        raise AccessDenied("A fresh matching native subscription authentication is required")

    def require_native_conformance(self, policy, binding, journal):
        raise AccessDenied("Full native-tool conformance is not established by diagnostic startup")
