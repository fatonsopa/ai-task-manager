"""Explicit installer delegation for one lead event; never human-presence evidence."""

from dataclasses import asdict

from ai_task_manager.adapters.decisions.authority import HostGrant
from ai_task_manager.adapters.decisions.observations import identity
from ai_task_manager.adapters.hosts.bridge.controller import ManagedNativeHost
from ai_task_manager.config.files import digest, read_private
from ai_task_manager.domains.work.errors import AccessDenied

from .manifest import NativeManifest
from .normal import NormalAuthoritySource, lead_operator

KIND = "delegated_enrollment"


def capture(manifest, permits, native, service, event):
    """Trusted owner calls after genuine native startup and conformance admission."""
    from .lead_startup import LeadAccounting, projected_event

    if (
        type(permits) is not LeadAccounting
        or permits.manifest is not manifest
        or type(native) is not ManagedNativeHost
        or native.permits is not permits
        or native.journal is not permits.journal
        or permits.service is not service
    ):
        raise AccessDenied("Actual finite lead accounting owner required")
    permits._owner(native.policy, native.binding)
    native.verify_active()
    permits.require_native_conformance(native.policy, native.binding)
    spec = read_private(manifest.installation.path)
    if lead_operator(spec) != "delegated":
        raise AccessDenied("Explicit standing lead delegation is absent")
    if projected_event(service.call, spec["lead"]["event_id"], spec["task_id"]) != event:
        raise AccessDenied("Delegated event changed before enrollment")
    grant = service.decisions.observations.verify_enrollment(service.grant_ref)
    permission = permits.admissions.store.permission(permits.permission.reference)
    if permission.get("operator_mode") != "delegated":
        raise AccessDenied("Native permission does not explicitly delegate lead enrollment")
    payload = {
        "scope": asdict(service.scope),
        "logical_session": service.scope.logical_session,
        "session_id": service.session_id,
        "provider_enrollment": grant.source_ref,
        "task_id": spec["task_id"],
        "event_id": event["id"],
        "startup_ref": native.startup.ready_ref,
        "binding": asdict(native.binding),
        "manifest": str(manifest.seal.path),
        "config": str(manifest.config.path),
        "installation": str(manifest.installation.path),
        "manifest_digest": manifest.digest,
        "files": {
            name: identity(getattr(manifest, name).path, content=True)
            for name in ("seal", "config", "installation")
        },
        "permission_ref": permits.permission.reference,
        "permission_hash": digest(permission),
        "actions": [],
    }
    return native.journal._append(
        KIND,
        payload,
        sources=(native.startup.ready_ref,),
        expires_at=min(grant.expires_at, native.binding.deadline, permission["deadline"]),
    )


def verify(journal, record, grant, binding, session_id):
    """Refresh current signed role/source authority, including cold receipt use.

    Provider observations occur outside Core through the existing session owner;
    this consumer performs only local permission/source checks.
    """
    if record["kind"] != KIND:
        raise AccessDenied("Distinct delegated enrollment provenance required")
    p = record["payload"]
    if set(p) != {
        "scope",
        "logical_session",
        "session_id",
        "provider_enrollment",
        "task_id",
        "event_id",
        "startup_ref",
        "binding",
        "manifest",
        "config",
        "installation",
        "manifest_digest",
        "files",
        "permission_ref",
        "permission_hash",
        "actions",
    }:
        raise AccessDenied("Exact delegated enrollment fields required")
    if set(p["files"]) != {"seal", "config", "installation"} or any(
        identity(value["path"], content=True) != value for value in p["files"].values()
    ):
        raise AccessDenied("Original delegated installation files changed")
    if any(
        p["files"][name]["path"] != p[field]
        for name, field in (
            ("seal", "manifest"),
            ("config", "config"),
            ("installation", "installation"),
        )
    ):
        raise AccessDenied("Delegated installation paths differ from original sources")
    manifest = NativeManifest(p["manifest"], p["config"], p["installation"])
    spec = read_private(manifest.installation.path)
    source = NormalAuthoritySource(manifest, role="lead")
    permission = source.resolve(p["permission_ref"], manifest)
    permission.check(manifest, spec["task_id"], spec["deadline"])
    current = source.store.permission(p["permission_ref"])
    startup = journal._read(p["startup_ref"], "startup", fresh=False)
    if (
        lead_operator(spec) != "delegated"
        or current.get("operator_mode") != "delegated"
        or digest(current) != p["permission_hash"]
        or manifest.digest != p["manifest_digest"]
        or p["scope"] != asdict(grant.scope)
        or p["logical_session"] != grant.logical_session
        or p["provider_enrollment"] != grant.source_ref
        or p["session_id"] != session_id
        or p["binding"] != binding
        or p["actions"] != []
        or p["task_id"] != spec["task_id"]
        or p["event_id"] != spec["lead"]["event_id"]
        or session_id != spec["lead"]["session_id"]
        or startup["payload"]["scope"] != p["scope"]
        or set(record["sources"]) != {p["startup_ref"]}
        or record["expires_at"] > min(grant.expires_at, current["deadline"], binding["deadline"])
    ):
        raise AccessDenied("Current delegated lead event, source or authority changed")
    return HostGrant(grant.scope, grant.logical_session, (), record["ref"], record["expires_at"])
