"""Establish one real provider-backed session with explicit delegated actions."""

import math
import time
from dataclasses import asdict
from pathlib import Path

from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.decisions.observations import (
    PREFIX,
    SessionObservations,
    bind_record,
    identity,
    intent,
)
from ai_task_manager.application.core import Core
from ai_task_manager.application.service.linear import LinearComponents
from ai_task_manager.config.claim_store import enroll_claim_store, open_claim_store
from ai_task_manager.config.files import FileSeal, digest, private_path, read_private, write_private
from ai_task_manager.config.sessions import SessionControl, SessionFile
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.domains.work.models import ApprovalPolicy, Scope
from ai_task_manager.storage.database import Database

from .recovery import (
    history,
    installed_authority,
    provider_history,
    renewal,
    setup_lock,
    setup_origin,
)

# Capability names only; transition semantics remain exclusively in Core.
ACTIONS = frozenset(
    {
        "enqueue",
        "dequeue",
        "hold",
        "start",
        "cancel",
        "duplicate",
        "merge",
        "stage",
        "release",
        "non_code_complete",
        "request_review",
        "resume",
    }
)


class _PendingSetup:
    def __init__(self, observations):
        self.observations = observations

    def verify_enrollment(self, reference):
        # Only the trusted setup may consume its intent before capability publication.
        return self.observations._pending_enrollment(reference)


def setup(
    config_file,
    *,
    logical_session,
    actions,
    deadline,
    operation_key,
    components=None,
    renew_from=None,
    workflow_control=None,
):
    """Trusted operator/host port, never a tool or a JSON verdict importer.

    The caller is the installer acting on actual operator authorization. Explicit
    action names grant only delegation; they never record a human instruction.
    ``components`` is a trusted code port for bounded transport controls.
    ``renew_from`` explicitly selects completed expired enrollment history;
    optional new owner paths preserve a verified stopped owner's descriptor.
    """
    config_file = private_path(config_file)
    with setup_lock(config_file):
        return _setup(
            config_file,
            logical_session,
            actions,
            deadline,
            operation_key,
            components,
            renew_from,
            workflow_control,
        )


def _setup(
    config_file,
    logical_session,
    actions,
    deadline,
    operation_key,
    components,
    renew_from,
    workflow_control,
):
    seal = FileSeal(config_file)
    config = read_private(config_file)
    if (
        not isinstance(logical_session, str)
        or not logical_session.strip()
        or not isinstance(operation_key, str)
        or not operation_key
        or type(actions) is not tuple
        or not actions
        or len(set(actions)) != len(actions)
        or not set(actions) <= ACTIONS
        or type(deadline) not in {int, float}
        or not math.isfinite(deadline)
        or deadline <= time.time()
    ):
        raise AccessDenied("Explicit finite session delegation required")
    reference = PREFIX + digest({"config": str(config_file), "key": operation_key})
    if renew_from is not None or config.get("grant_ref") is not None:
        installed_authority(config)
    journal = AuthorityJournal(config["authority_database"], config["authority_key"])
    if renew_from is not None:
        provider_history(journal, config, config.get("grant_ref"))
    components = components or LinearComponents(config["linear"])
    if components.configuration_hash != digest(config["linear"]):
        raise AccessDenied("Session provider differs from configured provider")
    binding = components.client.verify_binding()
    expected = binding.expected
    scope = Scope(
        "linear", expected.workspace_id, expected.viewer_id, expected.project_id, logical_session
    )
    if identity(components.journal.path) != identity(config["linear"]["journal"]):
        raise AccessDenied("Session provider journal differs from configured history")
    db_path = private_path(config["core_database"], existing=False)
    database = Database(db_path)
    db_path.chmod(0o600)
    # This trusted installer enrolls the current store, never re-elects another.
    enroll_claim_store(database, journal)
    database, _ = open_claim_store(db_path, journal)
    core = Core(
        database,
        ApprovalPolicy(**config["approval_policy"]),
        principals=components.journal,
        evidence=components.evidence(journal),
        renderer=components.renderer,
    )
    destination = Path(config["session_file"]).absolute()
    result_ref = "session-setup-result:" + digest(reference)
    published = config.get("grant_ref") == reference
    if published and history(journal, result_ref) is None:
        raise EvidenceRequired("Published session setup is incomplete; reconcile without replay")
    origin_ref, origin = setup_origin(
        journal, reference, asdict(scope), destination, config, renew_from, workflow_control
    )
    if workflow_control is not None and renew_from is None:
        raise AccessDenied("Owner changes require explicit session renewal")
    if destination.exists():
        handle = SessionFile(destination).load(core)
        if core.session_binding(handle)[1] != scope:
            raise AccessDenied("Existing capability belongs to another logical session")
        if origin["capability"] is not None and origin["capability"] != identity(
            destination, content=True
        ):
            raise AccessDenied("Existing capability changed during setup recovery")
        if renew_from is not None and config.get("grant_ref") != reference:
            config = renewal(
                journal,
                config_file,
                config,
                renew_from,
                asdict(scope),
                time.time(),
                core,
                handle,
                workflow_control,
            )
            # Refresh the connected application as well as the human/project binding.
            components.client.check_connection(scope, refresh=True)
    elif renew_from is not None:
        raise AccessDenied("Renewal requires its existing session capability")
    prior = history(journal, reference)
    if prior is not None and not destination.exists():
        raise EvidenceRequired("Prior session setup may have committed; reconcile without replay")
    if deadline <= time.time():
        raise AccessDenied("Session delegation expired during provider verification")
    if renew_from is None and origin["capability"] is None:
        bind_record(
            journal,
            "session-initial-setup:" + digest(str(config_file)),
            {
                "enrollment": reference,
                "scope": asdict(scope),
            },
        )
    bind_record(journal, origin_ref, origin)
    payload = {
        "scope": asdict(scope),
        "provider": asdict(expected),
        "config_path": str(config_file),
        "config_intent": intent(config),
        "actions": list(actions),
        "expires_at": deadline,
    }
    bind_record(journal, reference, payload)
    if published:
        result = {"scope": asdict(scope), "session_file": identity(destination, content=True)}
        bind_record(journal, result_ref, result)
        seal.check()
        return {"enrollment_ref": reference, "scope": asdict(scope), "capability_saved": True}
    pointer = {
        "reference": reference,
        "journal": identity(journal.path),
        "key": identity(journal.key_path, content=True),
    }
    observations = SessionObservations(journal, components.client, config_file, pointer)
    pending = _PendingSetup(observations)
    if renew_from is None:
        pending.verify_enrollment(reference)
    if not destination.exists():
        SessionControl(core, components.client, components.journal, pending).establish(
            reference, destination
        )
    result = {"scope": asdict(scope), "session_file": identity(destination, content=True)}
    bind_record(journal, result_ref, result)
    updated = {**config, "grant_ref": reference, "session_observations": pointer}
    seal.check()
    if updated != config:
        write_private(config_file, updated, replace=True)
    return {"enrollment_ref": reference, "scope": asdict(scope), "capability_saved": True}
