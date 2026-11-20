"""Direct Core/issuer guards; actual cold issuance is proved by the installed control."""

from dataclasses import asdict
from uuid import uuid4

import pytest

from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.decisions.observations import bind_record
from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import WorkError
from ai_task_manager.installation.workflows.delivery_dependencies import DeliveryDependencies
from ai_task_manager.storage import scheduling
from tests.domains.helpers import Rig
from tests.hosts.test_continuity import AuthoredDependencyCollection


@pytest.mark.parametrize(
    "change", ["current", "hold", "graph", "source", "prose", "expired", "revoked"]
)
def test_cold_delivery_consumes_only_current_bound_authority(tmp_path, change):
    r = Rig(tmp_path / "core.sqlite")
    r.key = lambda: "fixture:" + str(uuid4())
    task, attempt = r.running()
    task, attempt = r.settle(task, attempt)
    with r.db.transaction() as conn:
        original = scheduling.current(conn, task.id)
    deadline = r.now + 300
    journal = AuthorityJournal(tmp_path / "authority.sqlite", tmp_path / "authority.key",
                               clock=lambda: r.now)
    enrollment = "session-enrollment:owned-cold"
    bind_record(journal, enrollment, {"scope": asdict(r.scope),
                "actions": ["start", "merge"], "expires_at": deadline})
    payload = {
        "workflow": "workflow:owned", "task_id": task.id, "attempt_id": attempt.id,
        "task_version": task.version, "attempt_version": attempt.version,
        "decision_revision": task.decision_revision, "prose_digest": task.prose_digest,
        "graph_version": original["graph_version"], "snapshot": asdict(original["snapshot"]),
        "remaining_work": list(task.required_work), "deadline": deadline,
    }
    if change == "graph":
        payload["graph_version"] += 1
    elif change == "source":
        payload["snapshot"]["source_id"] = "changed-source"
    elif change == "prose":
        payload["prose_digest"] = "changed-prose"
    reference = journal._evidence("workflow_cold_delivery", r.scope, task.id,
                                  payload, enrollment, deadline)
    kinds = frozenset(proof.kind for proof in r.proofs.evidence.values()) | {"human_decision"}
    r.core.context.evidence = EvidenceRouter([
        EvidenceRoute("fixture:", kinds, r.proofs),
        EvidenceRoute("decision-proof:", frozenset({"workflow_cold_delivery"}), journal),
    ])
    r.now += 31
    source = AuthoredDependencyCollection(r.core, r.handle)
    source.refresh_for_start(task.id, r.key(), expected_version=task.version,
                             decision_revision=task.decision_revision)
    with r.db.transaction() as conn:
        invalidation = scheduling.attempt_binding(conn, attempt.id)["invalidated_by"]
    assert invalidation
    if change == "hold":
        decision = r.proof("human_decision", task.id,
                           {"previous_revision": task.decision_revision, "hold": True})
        r.core.decisions.record(r.handle, task.id, task.version, decision, r.key())
    elif change == "expired":
        r.now = deadline + 1
    elif change == "revoked":
        bind_record(journal, "session-revoked:" + digest(enrollment), {"revoked": True})
    if change == "current":
        r.core.scheduling.cold_delivery(r.handle, task.id, attempt.id, reference)
        with r.core.execution.worker_commit_boundary(
            r.handle, task.id, attempt.id, task.version, attempt.version, task.decision_revision
        ):
            pass
    else:
        with pytest.raises(WorkError):
            r.core.scheduling.cold_delivery(r.handle, task.id, attempt.id, reference)
    with r.db.transaction() as conn:
        assert scheduling.attempt_binding(conn, attempt.id)["invalidated_by"] == invalidation


def test_fresh_ordinary_delivery_does_not_consume_a_cold_bootstrap(tmp_path):
    r = Rig(tmp_path / "core.sqlite")
    task, attempt = r.running()
    task, attempt = r.settle(task, attempt)
    source = AuthoredDependencyCollection(r.core, r.handle)
    # A fresh ordinary route needs no cold capability or completion receipt.
    controller = DeliveryDependencies(r.core, r.handle, source, r.now + 120, recovery=object())
    try:
        controller.start({"task_id": task.id, "attempt_id": attempt.id,
                          "prose_digest": task.prose_digest,
                          "decision_revision": task.decision_revision, "steps": {}})
        assert controller._cold_ref is None and source.calls == 1
    finally:
        controller.close()
