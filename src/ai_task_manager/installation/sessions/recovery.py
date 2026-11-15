"""Inspect expired enrollment history without granting its old authority."""

import fcntl
import hashlib
import hmac
import json
import math
import os
import sqlite3
import subprocess
from contextlib import contextmanager
from pathlib import Path

from ai_task_manager.adapters.decisions.observations import PREFIX, bind_record, identity, intent
from ai_task_manager.adapters.linear.receipts import encoded
from ai_task_manager.adapters.workflow_control.identity import file_binding, settings
from ai_task_manager.config.files import digest, read_private
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired, IdempotencyConflict
from ai_task_manager.domains.work.models import ACTIVE_ATTEMPTS, AttemptState, Scope


@contextmanager
def setup_lock(config_file):
    """Serialize trusted installers without holding a Core transaction over I/O."""
    from ai_task_manager.config.files import private_path

    path = private_path(str(config_file) + ".session-setup.lock", existing=False)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_nlink != 1 or (info.st_dev, info.st_ino) != (
            path.stat().st_dev,
            path.stat().st_ino,
        ):
            raise AccessDenied("Session setup lock must be an original physical file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise EvidenceRequired("Another trusted session setup is active") from exc
        yield
    finally:
        os.close(fd)


def history(journal, reference):
    """Verify an immutable historical record; deliberately do not consume it."""
    with journal.transaction() as conn:
        row = conn.execute("SELECT data,mac FROM records WHERE ref=?", (reference,)).fetchone()
    return _verified_history(row, journal._key, reference)


def _verified_history(row, key, reference):
    if row is None:
        return None
    expected = hmac.new(key, row[0].encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, row[1]):
        raise AccessDenied("Session setup history changed")
    value = json.loads(row[0])
    if value.get("ref") != reference:
        raise AccessDenied("Session setup history has another identity")
    return {k: v for k, v in value.items() if k != "ref"}


def installed_authority(config):
    """Check the published physical binding before constructors can create files."""
    pointer = config.get("session_observations")
    if (
        not isinstance(pointer, dict)
        or set(pointer) != {"reference", "journal", "key"}
        or pointer["reference"] != config.get("grant_ref")
        or pointer["journal"] != identity(config["authority_database"])
        or pointer["key"] != identity(config["authority_key"], content=True)
    ):
        raise AccessDenied("Renewal requires the original installed enrollment")


def provider_history(journal, config, reference):
    """Missing continuity evidence requires reconciliation, never a new journal."""
    origin = history(journal, "session-setup-origin:" + digest(reference))
    return _provider_history(origin, config)


def installed_provider_history(config):
    """Read published continuity before constructors can initialize or migrate stores."""
    reference = "session-setup-origin:" + digest(config.get("grant_ref"))
    path = Path(config["authority_database"]).absolute()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT data,mac FROM records WHERE ref=?", (reference,)).fetchone()
    finally:
        conn.close()
    origin = _verified_history(row, Path(config["authority_key"]).read_bytes(), reference)
    return _provider_history(origin, config)


def _provider_history(origin, config):
    if origin is None or origin.get("provider_journal") is None:
        raise EvidenceRequired(
            "Original provider journal evidence requires explicit reconciliation"
        )
    try:
        current = identity(config["linear"]["journal"])
    except (AccessDenied, OSError) as exc:
        raise AccessDenied("Original provider journal is unavailable") from exc
    if origin["provider_journal"] != current:
        raise AccessDenied("Original provider journal changed")
    return current


def setup_origin(journal, reference, scope, destination, config, renew_from, workflow_control):
    """A refused enrollment must never become an adoption on a same-key retry."""
    origin_ref = "session-setup-origin:" + digest(reference)
    origin = history(journal, origin_ref)
    if origin is not None:
        if (
            origin["scope"] != scope
            or origin["renew_from"] != renew_from
            or origin["workflow_control"] != workflow_control
        ):
            raise IdempotencyConflict("Session setup origin has different immutable arguments")
        return origin_ref, origin
    existing = history(journal, reference)
    if existing is not None:
        raise EvidenceRequired(
            "Existing legacy enrollment requires explicit recovery and reconciliation"
        )
    if destination.exists() and existing is None and renew_from is None:
        raise AccessDenied("New enrollment cannot adopt an existing capability")
    return origin_ref, {
        "scope": scope,
        "renew_from": renew_from,
        "workflow_control": workflow_control,
        "capability": identity(destination, content=True) if destination.exists() else None,
        "provider_journal": identity(config["linear"]["journal"]),
    }


def intake_recovery_authorization(service, config_file, config, target, recovery_ref, deadline):
    """Trusted operator port: finite read-only recovery, never an execution grant."""
    now = service.core.context.clock()
    if (
        not isinstance(recovery_ref, str)
        or not 1 <= len(recovery_ref) <= 256
        or type(deadline) not in {int, float}
        or not math.isfinite(deadline)
        or not now < deadline <= now + 300
    ):
        raise AccessDenied("Explicit finite read-only recovery authorization required")
    journal = service.decisions.journal
    original = service.grant_ref
    prior = history(journal, original)
    completed = history(journal, "session-setup-result:" + digest(original))
    from dataclasses import asdict

    if (
        prior is None
        or completed is None
        or prior.get("scope") != asdict(service.scope)
        or prior.get("config_path") != str(Path(config_file).absolute())
        or prior.get("config_intent") != intent(config)
        or prior.get("expires_at", float("inf")) > now
        or completed.get("scope") != asdict(service.scope)
        or completed.get("session_file", {}).get("path") != config["session_file"]
        or history(journal, "session-revoked:" + digest(original)) is not None
    ):
        raise AccessDenied("Recovery requires unchanged completed expired enrollment")
    reference = "initial-intake-recovery:" + digest(recovery_ref)
    value = {
        "authorization": recovery_ref,
        "permission": "existing-intake-update-readback-only",
        "scope": asdict(service.scope),
        "config": file_binding(config_file),
        "original_enrollment": original,
        "original_enrollment_digest": digest(prior),
        "target": target,
        "expires_at": deadline,
    }
    bind_record(journal, reference, value)

    def check():
        service.seal.check()
        service.claim_store.check()
        installed_authority(read_private(config_file))
        installed_provider_history(config)
        if (
            history(journal, reference) != value
            or history(journal, original) != prior
            or service.core.context.clock() >= deadline
            or history(journal, "session-revoked:" + digest(original)) is not None
        ):
            raise AccessDenied("Read-only recovery authorization expired or changed")

    check()
    return check


def _authenticated_repaired_creates(core, handle, provider):
    """Recognize retained historical uncertainty only through confirmed exact repair."""
    _, scope = core.session_binding(handle)
    operations = core.sync.list_operations(handle)
    accepted = set()
    for original in operations:
        if (
            original.kind != "create_backlog"
            or original.state != "uncertain"
            or original.error != "readback_conflict"
            or not original.fenced_by
        ):
            continue
        linked = [
            op
            for op in operations
            if op.kind == "update_prose"
            and op.task_id == original.task_id
            and op.payload.get("event_id") == original.fenced_by
        ]
        if len(linked) != 1:
            continue
        update = linked[0]
        if update.state != "confirmed" or update.fenced_by:
            continue
        try:
            proof = provider.verify_evidence(update.payload.get("repair_readback_ref"))
        except EvidenceRequired:
            continue
        fact = proof.payload
        sends = provider.sends(scope)
        old = [row for row in sends if row["id"] == fact.get("send_id")]
        confirmed = [row for row in sends if row["operation_id"] == update.id]
        if (
            proof.kind != "external_prose_readback"
            or proof.scope != scope
            or proof.subject != original.task_id
            or fact.get("repair_kind") != "pending_create_code_spans"
            or fact.get("create_operation_id") != original.id
            or update.payload.get("create_operation_id") != original.id
            or fact.get("create_attempt") != original.attempt_count
            or fact.get("old_prose_digest") != original.payload.get("prose_digest")
            or fact.get("new_prose_digest") != update.payload.get("prose_digest")
            or len(old) != 1
            or len(confirmed) != 1
        ):
            continue
        old, sent = old[0], confirmed[0]
        if (
            old["operation_id"] != original.id
            or old["attempt"] != original.attempt_count
            or old["state"] != "uncertain"
            or old["error"] != "readback_conflict"
            or not old["snapshot"]
            or not sent["snapshot"]
            or sent["state"] != "confirmed"
            or sent["attempt"] != update.attempt_count
            or old["connection_digest"] != fact.get("connection_digest")
            or sent["connection_digest"] != old["connection_digest"]
            or digest(json.loads(old["snapshot"])) != fact.get("original_snapshot_digest")
        ):
            continue
        snapshot = json.loads(sent["snapshot"])
        if (
            snapshot.get("id") == fact.get("issue_id") == update.payload.get("issue_id")
            and snapshot.get("title") == update.payload.get("title")
            and snapshot.get("body") == update.payload.get("body")
            and snapshot.get("status") == update.payload.get("desired_status")
            and snapshot.get("assignee_id") == scope.principal
            and snapshot.get("project_id") == scope.project
            and snapshot.get("delegate_id") == json.loads(old["snapshot"]).get("delegate_id")
            and snapshot.get("delegate_is_application") is True
        ):
            accepted.add(original.id)
    return accepted


def renewal(journal, config_file, config, reference, scope, now, core, handle, workflow_control):
    """Fresh trusted operator delegation for the same expired, quiescent session."""
    pointer = config.get("session_observations", {})
    if (
        not isinstance(reference, str)
        or not reference.startswith(PREFIX)
        or config.get("grant_ref") != reference
        or pointer.get("reference") != reference
        or pointer.get("journal") != identity(journal.path)
        or pointer.get("key") != identity(journal.key_path, content=True)
    ):
        raise AccessDenied("Renewal requires the original installed enrollment")
    prior = history(journal, reference)
    completed = history(journal, "session-setup-result:" + digest(reference))
    if (
        prior is None
        or completed is None
        or prior.get("scope") != scope
        or prior.get("config_path") != str(config_file)
        or prior.get("config_intent") != intent(config)
        or prior.get("expires_at", float("inf")) > now
        or completed.get("scope") != scope
        or completed.get("session_file", {}).get("path") != config["session_file"]
        or history(journal, "session-revoked:" + digest(reference)) is not None
    ):
        raise AccessDenied("Renewal requires completed, expired, unchanged delegation")
    sid, _ = core.session_binding(handle)
    provider = core.context.principals
    repaired = _authenticated_repaired_creates(core, handle, provider)
    with core.context.db.transaction() as conn:
        rows = conn.execute("SELECT id,active,data FROM attempts WHERE session_id=?", (sid,))
        for row in rows:
            try:
                state = AttemptState(json.loads(row["data"])["state"])
            except (TypeError, ValueError, KeyError) as exc:
                raise EvidenceRequired("Attempt state is unavailable for renewal") from exc
            if row["active"] or state in ACTIVE_ATTEMPTS:
                raise EvidenceRequired("Active or unresolved work prevents session renewal")
        if conn.execute(
            "SELECT 1 FROM resource_leases r JOIN attempts a ON a.id=r.attempt_id "
            "WHERE a.session_id=? LIMIT 1",
            (sid,),
        ).fetchone():
            raise EvidenceRequired("Owned resources prevent session renewal")
        unsettled = core.intake._unsettled_preparation(conn, sid)
        if any(op.state in {"leased", "uncertain"} for op in unsettled) or any(
            row[0] not in repaired
            for row in conn.execute(
                "SELECT id FROM outbox WHERE session_id=? AND state='uncertain'", (sid,)
            )
        ):
            raise EvidenceRequired("Uncertain provider effects prevent session renewal")
    provider = core.context.principals
    if provider_history(journal, config, reference) != identity(provider.path):
        raise AccessDenied("Renewal provider journal differs from installed history")
    latest = {}
    for send in provider.sends(Scope(**scope)):
        old_send = latest.get(send["operation_id"])
        if old_send is None or old_send["attempt"] < send["attempt"]:
            latest[send["operation_id"]] = send
    if any(
        send["state"] not in {"confirmed", "failed"} and send["operation_id"] not in repaired
        for send in latest.values()
    ):
        raise EvidenceRequired("Unsettled provider sends prevent session renewal")
    with provider.transaction() as conn:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='progress_publications'"
        ).fetchone():
            columns = {row[1] for row in conn.execute("PRAGMA table_info(progress_publications)")}
            pending = "state IN ('leased','uncertain')"
            if {"label_state", "decision_state", "effect_owner"} <= columns:
                pending += (
                    " OR label_state IN ('sent','uncertain')"
                    " OR decision_state='resolving' OR effect_owner IS NOT NULL"
                )
            if conn.execute(
                "SELECT 1 FROM progress_publications WHERE scope=? AND (" + pending + ") LIMIT 1",
                (encoded(scope),),
            ).fetchone():
                raise EvidenceRequired("Unsettled progress publication prevents session renewal")
    old = config.get("workflow_control")
    if old is None:
        if workflow_control is not None:
            raise AccessDenied("Renewal cannot introduce an owner into another installation")
        return config
    old = settings(old)
    record = Path(old["record"])
    endpoint = Path(old["endpoint"])
    if endpoint.exists() or endpoint.is_symlink():
        raise EvidenceRequired("An existing owner endpoint prevents session renewal")
    if record.exists():
        descriptor = read_private(record)
        if (
            descriptor.get("scope") != scope
            or descriptor.get("instance") != old["instance"]
            or descriptor.get("config") != file_binding(config_file)
            or type(descriptor.get("pid")) is not int
            or descriptor["pid"] < 1
            or not isinstance(descriptor.get("start"), str)
        ):
            raise AccessDenied("Existing owner record belongs to another session")
        process = subprocess.run(
            ["ps", "-p", str(descriptor["pid"]), "-o", "uid=,lstart="],
            capture_output=True,
            text=True,
            timeout=2,
        )
        row = process.stdout.strip().split(maxsplit=1)
        dead = process.returncode == 1 and not process.stdout.strip()
        reused = (
            process.returncode == 0
            and len(row) == 2
            and (row[0] != str(os.getuid()) or row[1] != descriptor["start"])
        )
        if not dead and not reused:
            raise EvidenceRequired("The previous owner process has not stopped")
        if workflow_control is None:
            raise EvidenceRequired("Stopped owner history needs new explicit owner paths")
    if workflow_control is None:
        return config
    new = settings(workflow_control)
    if any(new[k] == old[k] for k in ("record", "endpoint", "instance")):
        raise AccessDenied("Renewal must preserve the previous owner record and identity")
    for name in ("record", "endpoint"):
        path = Path(new[name])
        if (
            path.exists()
            or path.is_symlink()
            or path.parent.resolve() != path.parent
            or not path.parent.is_dir()
            or path.parent.stat().st_mode & 0o077
            or path.parent.stat().st_uid != os.getuid()
        ):
            raise AccessDenied("New owner paths require unused private physical storage")
    return {**config, "workflow_control": new}
