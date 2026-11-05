"""Real local code publication with explicitly authored candidate review captures."""

from dataclasses import replace

import pytest

from ai_task_manager.adapters.resources.git import git
from ai_task_manager.adapters.workflow_reviews.code_candidates import CodeCandidateReviews
from ai_task_manager.adapters.workflow_reviews.models import CapturedReview
from tests.merge.workflows.helpers import Flow


class Captures:
    def __init__(self, clock, *, kind="accept"):
        self.clock, self.kind, self.calls = clock, kind, []

    def resolve(self, inspection):
        self.calls.append(inspection.digest)
        if self.kind == "missing":
            return None
        value = CapturedReview(
            "authored-candidate-review",
            inspection.digest,
            "independent-authored-reviewer",
            "authored-local",
            None,
            "authored-provenance",
            "a" * 64,
            "b" * 64,
            tuple(
                (unit, "accept", "Authored criterion observation")
                for unit in inspection.context["required_work"]
            ),
            "Authored local semantic input, not genuine model review",
            self.clock() + 100,
            (),
        )
        if self.kind == "wrong_context":
            value = replace(value, context_digest="c" * 64)
        elif self.kind == "self_review":
            value = replace(value, reviewer_id=inspection.context["author"]["worker_id"])
        elif self.kind == "expired":
            value = replace(value, expires_at=self.clock())
        return value


@pytest.mark.parametrize("kind", ["accept", "missing", "wrong_context", "self_review", "expired"])
def test_exact_prepared_candidate_review_guards_actual_publication(tmp_path, kind):
    flow = Flow(tmp_path / "owned")
    try:
        source = Captures(lambda: flow.r.now, kind=kind)
        reviews = CodeCandidateReviews(
            flow.r.core,
            flow.r.handle,
            flow.candidate,
            ("src/a.txt", "tests/a.txt", "config/checks.json"),
            source,
        )
        flow.workflow = flow.compose(candidate_reviews=reviews)
        flow.app.workflows = flow.workflow
        before = git(flow.tree, "rev-parse", "refs/heads/" + flow.target)
        result = flow.run()
        if kind == "accept":
            assert result["data"]["state"] == "done", result
            row = flow.workflow.store.read(result["data"]["workflow"])
            reviewed = row["steps"]["candidate_review"]["result"]
            candidate = row["steps"]["candidate"]["result"]
            assert reviewed["candidate_commit"] == candidate["commit"]
            assert reviewed["candidate_tree"] == candidate["tree"]
            assert reviewed["candidate_commit"] != flow.new
            assert flow.r.core.get_task(flow.r.handle, flow.task.id).observed_status == "In Dev"
            calls = len(source.calls)
            assert flow.resume(result["data"]["workflow"])["data"]["state"] == "done"
            assert len(source.calls) == calls
        else:
            if "data" in result:
                assert result["data"]["state"] in {"blocked", "awaiting_review"}, result
            else:
                assert not result["ok"] and result["exit_code"] == 3, result
            assert git(flow.tree, "rev-parse", "refs/heads/" + flow.target) == before
            assert not flow.r.core.get_task(flow.r.handle, flow.task.id).delivered_once
    finally:
        flow.close()


def test_candidate_file_scope_prepared_expiry_and_port_replacement_refuse(tmp_path):
    from ai_task_manager.domains.work.errors import EvidenceRequired

    flow = Flow(tmp_path / "owned")
    try:
        candidate = flow.candidate.prepare(flow.grant)
        source = Captures(lambda: flow.r.now)
        omitted = CodeCandidateReviews(
            flow.r.core, flow.r.handle, flow.candidate, ("src/a.txt",), source
        )
        with pytest.raises(EvidenceRequired, match="omits changed files"):
            omitted.read(flow.task, candidate)
        assert source.calls == []
        reviews = CodeCandidateReviews(
            flow.r.core,
            flow.r.handle,
            flow.candidate,
            ("src/a.txt", "tests/a.txt", "config/checks.json"),
            source,
        )
        assert reviews.read(flow.task, candidate)
        flow.r.now += 101
        with pytest.raises(EvidenceRequired, match="expired"):
            reviews.check_prepared(candidate.commit)
        flow.workflow = flow.compose(candidate_reviews=reviews)
        flow.app.workflows = flow.workflow
        flow.workflow.candidate_reviews = None
        result = flow.run()
        assert not result["ok"] and result["exit_code"] == 3
        assert git(flow.tree, "rev-parse", "refs/heads/" + flow.target) == flow.old
    finally:
        flow.close()
