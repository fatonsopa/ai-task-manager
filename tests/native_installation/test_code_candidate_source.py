"""Candidate reconstruction uses actual Git plus authored canonical-stage metadata.

These isolated adapter controls do not prove complete normal producer acceptance.
"""

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.resources.git import git
from ai_task_manager.adapters.workflow_reviews.code_candidates import CodeCandidateReviews
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.installation.native import NativeManifest
from ai_task_manager.installation.native.review_stages import CodeStageSource, InspectionOnly
from tests.code_provisioning.helpers import Provisioned


@pytest.fixture
def actual_candidate(tmp_path):
    fixture = Provisioned(tmp_path / "owned")
    try:
        from ai_task_manager.config.files import write_private

        configuration = json.loads(fixture.manifest.read_text())
        configuration["attempts"] = 2
        write_private(fixture.manifest, configuration, replace=True)
        fixture.reseal()
        fixture.prepare(operation_key="workflow:source-control:candidate")
        task = fixture.task
        receipt = {
            "task_id": task.id,
            "attempt_id": fixture.attempt.id,
            "commit": fixture.grant.source_head,
            "tree": git(fixture.pair.worktree, "rev-parse", "HEAD^{tree}"),
            "branch": "managed/" + fixture.attempt.id,
            "state": "committed",
            "intent_digest": "a" * 64,
        }
        specification = json.loads(fixture.bundle.installation.read_text())
        specification["files"] = ["verify.py", "src/a.txt"]
        recorder = InspectionOnly()
        CodeCandidateReviews(
            fixture.r.core,
            fixture.r.handle,
            fixture.candidates,
            tuple(specification["files"]),
            recorder,
        ).read(task, fixture.candidate)
        source = object.__new__(CodeStageSource)
        source.author = NativeManifest(
            fixture.bundle.manifest, fixture.bundle.config, fixture.bundle.installation
        )
        source.spec = SimpleNamespace(data=specification)
        source.core, source.handle, source.task = fixture.r.core, fixture.r.handle, task
        source.scope = fixture.r.scope
        source.attempt, source.pair, source.journal = (
            fixture.attempt,
            fixture.pair,
            fixture.runtime.journal,
        )
        source.resources = fixture.runtime.resources
        source.plan = SimpleNamespace(id="author", deadline=fixture.context.deadline)
        source.receipt, source.inspection = receipt, recorder.value
        source.row = {
            "id": "workflow:source-control",
            "steps": {
                "unit:" + unit: {"result": {"task_version": task.version}}
                for unit in task.required_work
            },
        }
        source.row["steps"]["code-plan"] = {
            "result": {"installed": True},
            "payload": {
                "commit": receipt,
                "registration": fixture.code.fingerprint,
                "plan": {
                    "id": "author:merge",
                    "deadline": fixture.context.deadline,
                    "require_human_acceptance": False,
                    "grant": asdict(fixture.grant),
                },
            },
        }
        from ai_task_manager.application.merging.workflows.models import MergePlan
        from ai_task_manager.application.merging.workflows.store import MergeWorkflowStore
        from ai_task_manager.config.files import digest

        merge_plan = MergePlan("author:merge", fixture.grant, fixture.context.deadline)
        store = MergeWorkflowStore(fixture.candidates.store.journal)
        reference = store.create(
            merge_plan, fixture.candidate_profile, source.row["id"] + ":merge", maximum_attempts=2
        )
        owner = store.claim(reference)
        store.intend(reference, owner, "candidate", {"grant": digest(asdict(fixture.grant))})
        store.finish(reference, owner, "candidate", asdict(fixture.candidate))
        store.release(reference, owner)
        fixture.native_merge_store, fixture.native_merge_ref = store, reference
        fixture.native_original_plan = merge_plan
        yield fixture, source, recorder.value
    finally:
        fixture.close()


def test_actual_candidate_reconstruction_matches_observed_commit_tree_and_witnesses(
    actual_candidate,
):
    fixture, source, inspection = actual_candidate
    reconstructed = source._candidate(fixture.state)
    assert reconstructed.digest == inspection.digest
    assert reconstructed.witnesses == inspection.witnesses
    assert reconstructed.context["source_version"]["head"] == fixture.candidate.commit
    assert reconstructed.context["source_version"]["tree"] == fixture.candidate.tree
    assert fixture.candidate.commit != fixture.grant.source_head


@pytest.mark.parametrize("fault", ["unit", "unfinished", "grant", "registration"])
def test_candidate_requires_canonical_prior_stage_authority(actual_candidate, fault):
    fixture, source, _ = actual_candidate
    selected = source.row["steps"]["code-plan"]
    if fault == "unit":
        source.row["steps"]["unit:complete"]["result"] = None
    elif fault == "unfinished":
        selected["result"] = None
    elif fault == "grant":
        selected["payload"]["plan"]["grant"]["source_head"] = "0" * 40
    else:
        selected["payload"]["registration"] = "0" * 64
    with pytest.raises(AccessDenied):
        source._candidate(fixture.state)
    assert not fixture.journal._intents()


def test_changed_prepared_candidate_refuses_source_reconstruction(actual_candidate):
    fixture, source, inspection = actual_candidate
    from pathlib import Path

    (Path(fixture.candidate.path) / "src/a.txt").write_text("changed after capture")
    with pytest.raises(EvidenceRequired):
        inspection.check()
    with pytest.raises(EvidenceRequired):
        source._candidate(fixture.state)


def test_current_forward_successor_uses_new_canonical_candidate_and_grant(actual_candidate):
    from ai_task_manager.application.merging.workflows.recovery import successor
    from ai_task_manager.config.files import digest

    fixture, source, original_inspection = actual_candidate
    original = fixture.native_original_plan
    store, ref = fixture.native_merge_store, fixture.native_merge_ref
    target = git(
        fixture.runtime.repo,
        "commit-tree",
        fixture.base + "^{tree}",
        "-p",
        fixture.base,
        "-m",
        "Independent forward target",
    )
    git(fixture.runtime.repo, "update-ref", "refs/heads/integration", target, fixture.base)
    selected = successor(original, target)
    candidate = fixture.candidates.target_successor(original.grant, selected.grant)
    assert candidate.status == "ready"
    archived = store.read(ref)["steps"]
    owner = store.claim(ref)
    store.update(
        ref,
        owner,
        active_plan=asdict(selected),
        target_lineage=[{"plan": asdict(original), "target": target, "steps": archived}],
        steps={},
    )
    store.intend(ref, owner, "candidate", {"grant": digest(asdict(selected.grant))})
    store.finish(ref, owner, "candidate", asdict(candidate))
    store.release(ref, owner)
    recorder = InspectionOnly()
    CodeCandidateReviews(
        fixture.r.core,
        fixture.r.handle,
        fixture.candidates,
        tuple(source.spec.data["files"]),
        recorder,
    ).read(fixture.task, candidate)
    source.inspection = recorder.value
    reconstructed = source._candidate(fixture.state)
    assert reconstructed.digest == recorder.value.digest != original_inspection.digest
    assert source.grant == selected.grant and source.grant.binding.operation_key.startswith(
        "candidate-target:"
    )
    assert source.row["steps"]["code-plan"]["payload"]["plan"]["grant"] == asdict(original.grant)
    assert reconstructed.context["source_version"]["head"] == candidate.commit


@pytest.mark.parametrize("fault", ["original_request", "active_plan", "candidate"])
def test_changed_merge_lineage_or_current_candidate_refuses_before_review(actual_candidate, fault):
    fixture, source, _ = actual_candidate
    store, ref = fixture.native_merge_store, fixture.native_merge_ref
    row = store.read(ref)
    owner = store.claim(ref)
    if fault == "original_request":
        request = dict(row["request"], plan="0" * 64)
        store.update(ref, owner, request=request)
    elif fault == "active_plan":
        plan = asdict(fixture.native_original_plan)
        plan["grant"]["target_head"] = "0" * 40
        store.update(ref, owner, active_plan=plan)
    else:
        steps = dict(row["steps"])
        steps["candidate"] = {**steps["candidate"], "result": None}
        store.update(ref, owner, steps=steps)
    store.release(ref, owner)
    with pytest.raises(AccessDenied):
        source._candidate(fixture.state)
    assert not fixture.capability.checks.checks.source_guard.runner.jobs
