"""Real local code task/Core with authored captures; no native model acceptance."""

from dataclasses import replace

import pytest

from ai_task_manager.adapters.workflow_reviews import WorkflowReviews
from ai_task_manager.adapters.workflow_reviews.code_worker_units import CodeWorkerUnitIssuer
from ai_task_manager.adapters.workflow_reviews.native import review_prompt
from ai_task_manager.domains.work.errors import EvidenceRequired, WorkError
from tests.workflow_reviews.helpers import ReviewRig


@pytest.fixture
def rig(tmp_path):
    value = object.__new__(ReviewRig)
    try:
        value.__init__(tmp_path / "owned", code=True)
        yield value
    finally:
        if hasattr(value, "runtime"):
            value.close()


def test_code_units_actual_core_replay_and_restart(rig):
    refs = rig.lookup()
    assert set(refs) == {"units"} and set(refs["units"]) == {"complete"}
    reference = refs["units"]["complete"]
    original = rig.task
    task = rig.complete(reference, key="code-unit-replay", task=original)
    assert task.completed_work == ("complete",) and not task.delivered_once
    assert rig.complete(reference, key="code-unit-replay", task=original) == task
    fresh = CodeWorkerUnitIssuer(rig.artifacts, rig.issuer_key, rig.source, clock=lambda: rig.r.now)
    with pytest.raises(EvidenceRequired):
        fresh.verify_evidence(reference)
    assert fresh.lookup(rig.scope, task, rig.saved["digest"]) == refs
    prompt = review_prompt(rig.artifacts.prepare(rig.scope, task, rig.saved["digest"]))
    assert "committed code-worker artifacts" in prompt and "non-code artifact" not in prompt
    assert "Authored report artifact" in prompt
    old = WorkflowReviews(rig.artifacts, rig.issuer_key, rig.source)
    assert old.lookup(rig.scope, task, rig.saved["digest"]) is None


@pytest.mark.parametrize(
    "change", ["scope", "rejection", "self_review", "omission", "dirty", "bytes", "expiry"]
)
def test_code_unit_authority_refusals(rig, change):
    if change == "scope":
        with pytest.raises(WorkError):
            rig.issuer.lookup(
                replace(rig.scope, principal="foreign"), rig.task, rig.saved["digest"]
            )
    elif change == "self_review":
        rig.install(changes={"reviewer_id": rig.attempt.worker_id}, suffix="self")
        with pytest.raises(WorkError):
            rig.lookup()
    elif change == "omission":
        rig.artifacts.plans = {rig.task.id: replace(rig.plan, artifacts=("missing.txt",))}
        with pytest.raises(WorkError):
            rig.lookup()
    elif change == "dirty":
        rig.report.write_text("uncommitted code")
        with pytest.raises(WorkError):
            rig.lookup()
    elif change == "rejection":
        rig.install(judgment="reject", suffix="reject")
        assert rig.lookup() is None
    else:
        reference = rig.lookup()["units"]["complete"]
        if change == "bytes":
            rig.report.write_text("changed after review")
        else:
            rig.r.now += 6
        with pytest.raises(WorkError):
            rig.complete(reference)
    assert rig.r.core.get_task(rig.r.handle, rig.task.id).completed_work == ()
