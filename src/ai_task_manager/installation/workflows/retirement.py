"""Retire only the original stopped resources under finite operator authority."""

from dataclasses import dataclass
from pathlib import Path

from ai_task_manager.adapters.resources.manager import ResourceManager
from ai_task_manager.application.service.models import result
from ai_task_manager.domains.work.errors import EvidenceRequired
from ai_task_manager.supervisor.journal import RuntimeJournal


@dataclass
class RetirementObservation:
    core: object
    handle: object
    scope: object
    claim_store: object


class RetirementReadView:
    """Expose only current observations, without a session or mutation capability."""

    def __init__(self, core, handle, proof, task_id, check):
        self._core, self._handle, self._proof = core, handle, proof
        self.task_id, self._check, self.context = task_id, check, core.context
        self._version = None

    def get_attempt(self, handle, attempt_id):
        self._check()
        if handle != self._handle:
            raise EvidenceRequired("Retirement observation capability changed")
        task, attempt = self._core.retirement_subject(handle, self._proof, attempt_id)
        current = (task.version, task.decision_revision, task.prose_digest)
        if self._version is not None and current != self._version:
            raise EvidenceRequired("Current task changed during resource retirement")
        self._version = current
        return attempt

    def get_task(self, handle, task_id):
        self._check()
        if handle != self._handle or task_id != self.task_id:
            raise EvidenceRequired("Retirement observation belongs to another task")
        task, _ = self._core.retirement_subject(handle, self._proof, self.attempt_id)
        if (task.version, task.decision_revision, task.prose_digest) != self._version:
            raise EvidenceRequired("Current task changed during resource retirement")
        return task


def observation_service(recovery):
    from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
    from ai_task_manager.application.core import Core
    from ai_task_manager.config.claim_store import open_claim_store
    from ai_task_manager.config.sessions import SessionFile
    from ai_task_manager.domains.work.models import ApprovalPolicy, Scope, SessionHandle

    recovery.check()
    config = recovery.config
    db, claims = open_claim_store(config["core_database"], recovery.journal, existing_only=True)
    scope = Scope(**recovery.spec.data["scope"])
    handle = SessionHandle(SessionFile(config["session_file"]).read()["capability"])
    row = recovery.store().read(recovery.data["request"]["workflow_ref"], scope)
    if not row["attempt_id"]:
        raise EvidenceRequired("Original resource-owning attempt is unavailable")
    proof = recovery.journal._evidence(
        "workflow_retirement_observation",
        scope,
        row["task_id"],
        {"workflow": row["id"]},
        recovery.reference,
        recovery.data["deadline"],
    )
    core = Core(
        db,
        ApprovalPolicy(**config["approval_policy"]),
        evidence=EvidenceRouter(
            [
                EvidenceRoute(
                    "decision-proof:",
                    frozenset({"workflow_retirement_observation"}),
                    recovery.journal,
                )
            ]
        ),
    )
    view = RetirementReadView(core, handle, proof, row["task_id"], recovery.check)
    view.attempt_id = row["attempt_id"]
    view.get_attempt(handle, row["attempt_id"])
    return RetirementObservation(view, handle, scope, claims)


def retire(recovery, service, row):
    recovery.validate_stores()
    attempt_id = row["attempt_id"] or recovery._reservation(service, row)
    if not attempt_id:
        raise EvidenceRequired("Original resource-owning attempt is unavailable")
    root = Path(recovery.spec.data["state_root"])
    journal = RuntimeJournal.open_existing(root / "runtime.sqlite")
    resources = ResourceManager(
        journal,
        root / "resources",
        Path(recovery.spec.data["repository"]),
        metadata_paths=(recovery.config["core_database"], root / "workflow.sqlite"),
        existing_only=True,
    )

    def check():
        recovery.check_service(service)
        service.core.get_task(service.handle, row["task_id"])

    removed = resources.cleanup(service.core, service.handle, attempt_id, check=check)
    return result(
        {"workflow": row["id"], "attempt_id": attempt_id, "state": "retired", "resources": removed}
    )
