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
from ai_task_manager.config.files import digest, private_path, read_private, write_private
from ai_task_manager.config.sessions import SessionControl, SessionFile
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.domains.work.models import ApprovalPolicy, Scope
from ai_task_manager.storage.database import Database

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


def setup(config_file, *, logical_session, actions, deadline, operation_key, components=None):
    """Trusted operator/host port, never a tool or a JSON verdict importer.

    The caller is the installer acting on actual operator authorization. Explicit
    action names grant only delegation; they never record a human instruction.
    ``components`` is a trusted code port for bounded transport controls.
    """
    config_file = private_path(config_file)
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
    components = components or LinearComponents(config["linear"])
    if components.configuration_hash != digest(config["linear"]):
        raise AccessDenied("Session provider differs from configured provider")
    binding = components.client.verify_binding()
    expected = binding.expected
    scope = Scope(
        "linear", expected.workspace_id, expected.viewer_id, expected.project_id, logical_session
    )
    journal = AuthorityJournal(config["authority_database"], config["authority_key"])
    reference = PREFIX + digest({"config": str(config_file), "key": operation_key})
    payload = {
        "scope": asdict(scope),
        "provider": asdict(expected),
        "config_path": str(config_file),
        "config_intent": intent(config),
        "actions": list(actions),
        "expires_at": deadline,
    }
    created = bind_record(journal, reference, payload)
    pointer = {
        "reference": reference,
        "journal": identity(journal.path),
        "key": identity(journal.key_path, content=True),
    }
    observations = SessionObservations(journal, components.client, config_file, pointer)
    pending = _PendingSetup(observations)
    pending.verify_enrollment(reference)
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
    if not created and not destination.exists():
        raise EvidenceRequired("Prior session setup may have committed; reconcile without replay")
    if destination.exists():
        handle = SessionFile(destination).load(core)
        if core.session_binding(handle)[1] != scope:
            raise AccessDenied("Existing capability belongs to another logical session")
        if created:
            raise AccessDenied("New enrollment cannot adopt an existing capability")
    else:
        SessionControl(core, components.client, components.journal, pending).establish(
            reference, destination
        )
    result = {"scope": asdict(scope), "session_file": identity(destination, content=True)}
    bind_record(journal, result_ref, result)
    updated = {**config, "grant_ref": reference, "session_observations": pointer}
    if updated != config:
        write_private(config_file, updated, replace=True)
    return {"enrollment_ref": reference, "scope": asdict(scope), "capability_saved": True}
