"""A separate finite canary permission; it never authorizes normal task work."""

import math
import os
import stat
from dataclasses import asdict
from datetime import UTC, datetime

from ai_task_manager.adapters.hosts.bridge.conformance.models import (
    CASE_IDS,
    ProbeContext,
)
from ai_task_manager.adapters.hosts.bridge.conformance.profile import (
    ProfileSources,
    StableProfile,
    prepare_profile,
)
from ai_task_manager.config.files import digest, read_private
from ai_task_manager.domains.work.errors import AccessDenied

from .controller import NormalInvocationController
from .normal import PURPOSES


def context_data(context):
    value = asdict(context)
    value["fixture_root"] = str(context.fixture_root)
    return value


class NativeProbeAuthority:
    def __init__(self, store, manifest, pins, context, *, profile, clock=None):
        if type(context) is not ProbeContext:
            raise AccessDenied("Typed enrolled native probe context required")
        self.store, self.manifest, self.pins, self.context = store, manifest, pins, context
        self.profile = profile
        self.clock = clock or (lambda: datetime.now(UTC))
        self.reference = "probe:" + context.reference
        self.permission = store.permission(self.reference)
        if self.permission.get("kind") != "native-conformance-probe":
            raise AccessDenied("Normal work permission cannot become probe authority")
        self.controller = NormalInvocationController(store, self.permission, clock=self.clock)
        self._original = (store, manifest, pins, context, self.clock, self.controller, profile)
        self._context_digest = digest(context_data(context))
        if (
            self.permission.get("context_digest") != self._context_digest
            or self.permission.get("scope") != asdict(context.scope)
            or self.permission.get("deadline") != context.deadline
            or self.permission.get("manifest_digest") != manifest.digest
        ):
            raise AccessDenied("Probe context differs from its enrolled permission")
        self.require_current(context, phase="observe")

    @classmethod
    def enroll(cls, store, manifest, pins, context, limits, *, identity_refreshes, profile):
        if (
            type(context) is not ProbeContext
            or not isinstance(pins, ProfileSources)
            or type(profile) is not StableProfile
        ):
            raise AccessDenied("Actual typed probe context and source pins required")
        manifest.check()
        pins.check()
        if manifest.data.get("authority_store") != store.pointer:
            raise AccessDenied("Probe permission belongs to another physical authority store")
        info = context.fixture_root.lstat()
        if (
            not context.reference
            or type(context.deadline) not in {int, float}
            or not math.isfinite(context.deadline)
            or context.deadline <= datetime.now(UTC).timestamp()
            or context.fixture_root.is_symlink()
            or context.fixture_root.resolve() != context.fixture_root
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode))
            != context.fixture_identity
            or stat.S_IMODE(info.st_mode) != 0o700
            or type(context.case_ids) is not tuple
            or not context.case_ids
            or len(set(context.case_ids)) != len(context.case_ids)
            or not set(context.case_ids) <= set(CASE_IDS)
            or context.deadline > read_private(manifest.installation.path)["deadline"]
        ):
            raise AccessDenied("Invalid original probe fixture, cases or deadline")
        d = manifest.data
        if (
            context.installation_digest != manifest.digest
            or context.profile_digest != profile.fingerprint
            or profile != prepare_profile(manifest, pins, profile.cli_version)
            or (context.host, context.model, context.effort, context.account_hash)
            != (d["host"], d["model"], d["effort"], d["account_hash"])
            or type(limits) is not dict
            or set(limits) != PURPOSES
            or any(
                type(v) not in {int, float} or not math.isfinite(v) or v <= 0
                for v in limits.values()
            )
            or type(identity_refreshes) is not tuple
            or len(set(identity_refreshes)) != len(identity_refreshes)
            or any(type(v) is not str or not v or v in PURPOSES for v in identity_refreshes)
        ):
            raise AccessDenied("Exact source/profile and finite probe-only plan required")
        if context.host != "claude" and identity_refreshes:
            raise AccessDenied("Genuine same-native identity refresh is currently Claude-only")
        reference = "probe:" + context.reference
        store.enroll(
            reference,
            {
                "kind": "native-conformance-probe",
                "reference": reference,
                "manifest_digest": manifest.digest,
                "context_digest": digest(context_data(context)),
                "task_id": reference,
                "scope": asdict(context.scope),
                "host": context.host,
                "model": context.model,
                "effort": context.effort,
                "account_hash": context.account_hash,
                "deadline": context.deadline,
                "limits": limits,
                "revoked": False,
                "billing_policy": "subscription-only",
                "identity_refreshes": list(identity_refreshes),
                "case_ids": list(context.case_ids),
            },
        )
        return cls(store, manifest, pins, context, profile=profile)

    def require_current(self, context, *, policy=None, binding=None, phase):
        if (
            any(
                a is not b
                for a, b in zip(
                    (
                        self.store,
                        self.manifest,
                        self.pins,
                        self.context,
                        self.clock,
                        self.controller,
                        self.profile,
                    ),
                    self._original,
                    strict=True,
                )
            )
            or context is not self.context
            or digest(context_data(context)) != self._context_digest
        ):
            raise AccessDenied("Native probe authority/context owner changed")
        if phase not in {
            "fixture",
            "observe",
            "start",
            "thread",
            "auth",
            "neutral",
            "canary",
            "identity",
            "stop",
        }:
            raise AccessDenied("Unknown native probe permission phase")
        current = self.store.permission(self.reference)
        if digest({**current, "revoked": self.permission["revoked"]}) != digest(self.permission):
            raise AccessDenied("Native probe enrollment changed")
        if phase != "stop":
            self.manifest.check()
            self.pins.check()
            if current["revoked"] or self.clock().timestamp() >= context.deadline:
                raise AccessDenied("Native probe permission expired or was revoked")
            info = context.fixture_root.lstat()
            if (
                context.fixture_root.is_symlink()
                or context.fixture_root.resolve() != context.fixture_root
                or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode))
                != context.fixture_identity
                or stat.S_IMODE(info.st_mode) != 0o700
                or context.installation_digest != self.manifest.digest
                or context.profile_digest != self.profile.fingerprint
                or self.profile
                != prepare_profile(self.manifest, self.pins, self.profile.cli_version)
                or type(context.case_ids) is not tuple
                or not context.case_ids
                or len(set(context.case_ids)) != len(context.case_ids)
                or not set(context.case_ids) <= set(CASE_IDS)
                or context.deadline > read_private(self.manifest.installation.path)["deadline"]
            ):
                raise AccessDenied("Original probe fixture/source/profile/deadline changed")
        if policy is not None and (
            (
                policy.host,
                policy.model,
                policy.effort,
                policy.executable_sha256,
                policy.environment,
                policy.cwd,
            )
            != (
                context.host,
                context.model,
                context.effort,
                self.manifest.data["executable_sha256"],
                self.manifest.environment,
                context.fixture_root,
            )
        ):
            raise AccessDenied("Probe policy differs from enrolled profile")
        if binding is not None and (
            binding.scope != context.scope
            or binding.host != context.host
            or binding.deadline > context.deadline
            or not binding.core_session.startswith("probe:")
            or not binding.attempt_id.startswith("probe:")
            or policy is not None
            and binding.configuration_digest != policy.configuration_digest
        ):
            raise AccessDenied("Probe binding differs from its isolated owner")
