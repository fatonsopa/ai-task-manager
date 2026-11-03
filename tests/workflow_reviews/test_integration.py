"""Actual controller/store/inspector integration; captured review is an authored control."""

import json

import pytest

from ai_task_manager.adapters.workflow_reviews import CurrentArtifacts
from ai_task_manager.application.workflows.reviews import ReviewedEvidence
from ai_task_manager.config.files import write_private
from tests.workflow_reviews.helpers import ReviewRig
from tests.workflows.helpers import WorkflowFixture


def installed_control(wf, root, artifact_digest):
    """Reuse only capture-fixture construction around the actual production ports."""
    value = object.__new__(ReviewRig)
    value.root, value.runtime, value.r = root, wf.rr, wf.rig
    value.scope = wf.rig.scope
    value.task = wf.rig.current(wf.task)
    value.saved = wf.store.verified(value.scope, value.task.id, artifact_digest)
    value.artifacts = CurrentArtifacts(
        wf.rig.core,
        wf.rig.handle,
        wf.store,
        wf.driver.repository,
        wf.runtime,
        {value.task.id: wf.plan},
        {value.task.id: (("request", "The artifact addresses the declared request"),)},
    )
    value.issuer_key, value.capture_key = root / "issuer.key", root / "capture.key"
    for path in (value.issuer_key, value.capture_key):
        path.write_bytes(b"x" * 32)
        path.chmod(0o600)
    value.revoked = root / "revoked.json"
    write_private(value.revoked, {"revoked": []})
    value.install()
    return value


class LostReply:
    def __init__(self, stage):
        self.stage, self.fired = stage, False

    def hit(self, stage):
        if stage == self.stage and not self.fired:
            self.fired = True
            raise RuntimeError("Authored lost controller reply")


@pytest.mark.parametrize("interruption", [None, "after:unit:complete", "after:deliver"])
def test_real_workflow_inspection_and_core_consume_independent_issuer(tmp_path, interruption):
    wf = WorkflowFixture(tmp_path / "actual-workflow")
    try:
        waiting = wf.driver.run(wf.ref)
        assert (
            waiting["state"] == "awaiting_review"
            and waiting["reason"] == "semantic_review_unavailable"
        )
        assert not wf.rig.current(wf.task).completed_work
        root = tmp_path / "installed-capture"
        root.mkdir(mode=0o700)
        control = installed_control(wf, root, waiting["artifact_digest"])
        reviews = ReviewedEvidence(wf.rig.core, control.issuer.lookup)
        faults = LostReply(interruption) if interruption else None
        first = wf.new_driver(reviews=reviews, faults=faults).run(wf.ref)
        if interruption:
            assert first["state"] == "blocked" and faults.fired
        else:
            assert first["state"] == "done"
        final = wf.new_driver(reviews=reviews).run(wf.ref)
        assert final["state"] == "done"
        task = wf.rig.current(wf.task)
        assert task.observed_status == "Done" and task.completed_work == task.required_work
        assert len(wf.runtime.dispatches) == 1
        events = wf.rig.core.notifications.list_events(wf.rig.handle)
        assert sum(e.kind == "work_unit_verified" for e in events) == 1
        assert (
            sum(
                e.kind == "transition_requested"
                and e.payload.get("cause") == "non_code_complete"
                and e.payload.get("evidence_refs", {})
                .get("non_code_completion", "")
                .startswith("workflow-review:")
                for e in events
            )
            == 1
        )
    finally:
        wf.close()
        workers = list(wf.rr.runtime.workers.values())
        (tmp_path / "review-cleanup.json").write_text(
            json.dumps(
                {
                    "all_workers_stopped": all(w.process.returncode is not None for w in workers),
                    "actual_ports": "WorkflowStore, RepositoryInspector, WorkflowDriver, Core",
                    "review_source": "Authored control; no real model/human judgment",
                }
            )
        )
