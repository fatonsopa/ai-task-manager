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
