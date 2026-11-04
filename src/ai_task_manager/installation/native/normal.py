"""Installer-owned normal permission lookup, independent of diagnostic evaluation."""

import json
import math

from ai_task_manager.adapters.hosts.bridge.accounting import AccountingPlan
from ai_task_manager.domains.work.errors import AccessDenied

from .admissions import NormalAdmissions
from .authority import NativePermission
from .controller import NormalInvocationController
from .store import NativeStore

PURPOSES = frozenset({"host", "relay", "auth", "neutral", "assignment"})


def enroll(store, manifest, limits, *, identity_refreshes, conformance_ref, role="worker"):
    """Trusted installer operation; permission is not an execution or billing receipt.

    This function is not a public model tool. It records the operator's exact
    subscription-only attempt restrictions. Actual authentication and conformance
    must still be observed by their separate producers.
    """
    manifest.check()
    if manifest.data.get("authority_store") != store.pointer:
        raise AccessDenied("Normal permission belongs to another physical authority store")
    if (
        type(limits) is not dict
        or set(limits) != PURPOSES
        or any(
            type(v) not in {int, float} or not math.isfinite(v) or v <= 0 for v in limits.values()
        )
    ):
        raise AccessDenied("Exact finite normal invocation limits are required")
    if (
        type(identity_refreshes) is not tuple
        or len(set(identity_refreshes)) != len(identity_refreshes)
        or any(type(v) is not str or not v or v in PURPOSES for v in identity_refreshes)
        or type(conformance_ref) is not str
        or not conformance_ref
    ):
        raise AccessDenied("Explicit finite identity-auth names and conformance reference required")
    if manifest.data["host"] != "claude" and identity_refreshes:
        raise AccessDenied("Genuine same-native identity refresh is currently Claude-only")
    installation = json.loads(manifest.installation.path.read_text())
    if role not in {"worker", "lead", "reviewer"} or installation.get("role", "worker") != role:
        raise AccessDenied("Normal permission role differs from sealed installation")
    deadline = installation["deadline"]
    if type(deadline) not in {int, float} or not math.isfinite(deadline):
        raise AccessDenied("Original finite installation deadline required")
    d = manifest.data
    permission = {
        "kind": "normal" if role == "worker" else "normal-" + role + "-v1",
        "identity_refreshes": list(identity_refreshes),
        "conformance_ref": conformance_ref,
        "reference": d["authorization_ref"],
        "manifest_digest": manifest.digest,
        "task_id": installation["task_id"],
        "scope": installation["scope"],
        "host": d["host"],
        "model": d["model"],
        "effort": d["effort"],
        "account_hash": d["account_hash"],
        "deadline": deadline,
        "limits": dict(limits),
        "billing_policy": "subscription-only",
        "revoked": False,
    }
    store.enroll(d["authorization_ref"], permission)
    return permission


class NormalAuthoritySource:
    def __init__(self, manifest, *, conformance=None, clock=None, role="worker"):
        if role not in {"worker", "lead", "reviewer"}:
            raise AccessDenied("Unknown native permission role")
        self._role = role
        pointer = manifest.data.get("authority_store")
        if not isinstance(pointer, dict) or set(pointer) != {"path", "device", "inode", "store_id"}:
            raise AccessDenied("Exact enrolled normal authority store is required")
        self.store = NativeStore(pointer["path"], pointer)
        self.manifest, self.conformance, self.clock = manifest, conformance, clock

    def resolve(self, reference, manifest):
        self.manifest.check()
        manifest.check()
        if (
            reference != manifest.data["authorization_ref"]
            or manifest.digest != self.manifest.digest
            or any(
                getattr(manifest, name).original != getattr(self.manifest, name).original
                for name in ["seal", "config", "installation"]
            )
        ):
            raise AccessDenied("Normal source belongs to another native installation")
        p = self.store.permission(reference)
        expected = {
            "reference",
            "kind",
            "identity_refreshes",
            "conformance_ref",
            "manifest_digest",
            "task_id",
            "scope",
            "host",
            "model",
            "effort",
            "account_hash",
            "deadline",
            "limits",
            "billing_policy",
            "revoked",
        }
        installation = json.loads(manifest.installation.path.read_text())
        if (
            set(p) != expected
            or installation.get("role", "worker") != self._role
            or p["kind"] != ("normal" if self._role == "worker" else "normal-" + self._role + "-v1")
            or type(p["identity_refreshes"]) is not list
            or len(set(p["identity_refreshes"])) != len(p["identity_refreshes"])
            or any(type(v) is not str or not v or v in PURPOSES for v in p["identity_refreshes"])
            or type(p["conformance_ref"]) is not str
            or not p["conformance_ref"]
            or p["reference"] != reference
            or p["manifest_digest"] != manifest.digest
            or p["revoked"] is not False
            or p["billing_policy"] != "subscription-only"
            or any(p[k] != manifest.data[k] for k in ["host", "model", "effort", "account_hash"])
            or any(p[k] != installation[k] for k in ["task_id", "scope", "deadline"])
            or type(p["limits"]) is not dict
            or set(p["limits"]) != PURPOSES
            or any(
                type(v) not in {int, float} or not math.isfinite(v) or v <= 0
                for v in p["limits"].values()
            )
        ):
            raise AccessDenied("Normal permission changed or belongs to another installation")
        c = NormalInvocationController(self.store, p, clock=self.clock)
        limits = p["limits"]
        plan = AccountingPlan(
            reference,
            "native",
            p["task_id"],
            p["host"],
            p["model"],
            p["effort"],
            "subscription-only:" + reference,
            p["deadline"],
            limits["host"],
            limits["auth"],
            limits["neutral"],
            limits["assignment"],
            identity_refreshes=tuple(p["identity_refreshes"]),
        )
        conformance = self.conformance
        if conformance is None:
            from .enrollment import CaptureEnrollments

            conformance = CaptureEnrollments(self.store).consumer(manifest, p["conformance_ref"])
        a = NormalAdmissions(manifest, self.store, p, plan, c, conformance)
        return NativePermission(reference, manifest.digest, c, a, plan)
