"""Connect sealed workflow declarations to Core without model-selected fields."""

import json

from ai_task_manager.domains.scheduling.resources import AdmissionResources
from ai_task_manager.domains.work.errors import EvidenceRequired
from ai_task_manager.domains.work.models import Scope


class PlanResourceDeclarations:
    def __init__(self, previous, scope, task, plan, repository, check, clock, witnesses):
        self.previous, self.scope, self.task_id = previous, scope, task.id
        self.prose_digest, self.plan, self.repository = task.prose_digest, plan, repository
        self.check, self.clock = check, clock
        self.witnesses = tuple(witnesses)

    def read(self, scope, task):
        if task.id != self.task_id:
            return self.previous.read(scope, task)
        self.check(task, self.plan)
        if (scope != self.scope or task.prose_digest != self.prose_digest
                or self.plan.deadline <= self.clock() or self.plan.resource_locks is None):
            raise EvidenceRequired("Current complete installed resource declarations are required")
        return AdmissionResources(
            scope, task.id, task.prose_digest, task.decision_revision,
            self.repository, self.plan.allowed_files, self.plan.resource_locks,
            self.plan.deadline, self.witnesses,
        )


def bind_resources(core, task, plan, repository, check, *, witnesses=()):
    previous = core.execution.resource_sources
    if (isinstance(previous, PlanResourceDeclarations) and previous.task_id == task.id
            and previous.plan == plan and previous.repository == repository):
        if plan.resource_locks is not None and plan.deadline > core.context.clock():
            core.execution.publish_resources(task)
        return
    with core.context.db.transaction() as conn:
        row = conn.execute("SELECT scope FROM sessions WHERE id=?", (task.session_id,)).fetchone()
    scope = Scope(**json.loads(row[0]))
    core.execution.resource_sources = PlanResourceDeclarations(
        previous, scope, task, plan, repository, check, core.context.clock, witnesses,
    )
    if plan.resource_locks is not None and plan.deadline > core.context.clock():
        core.execution.publish_resources(task)
