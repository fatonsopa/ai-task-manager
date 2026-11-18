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


def test_immutable_candidate_review_releases_repository_turn(tmp_path):
    import json
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from ai_task_manager.adapters.git.candidates import CandidateProfile

    flow = Flow(tmp_path / "owned")
    entered, release, progressed = Event(), Event(), Event()
    captures = Captures(lambda: flow.r.now)
    resolve = captures.resolve

    def delayed(inspection):
        entered.set()
        assert release.wait(4), "Finite immutable review barrier expired"
        return resolve(inspection)

    captures.resolve = delayed
    reviews = CodeCandidateReviews(flow.r.core, flow.r.handle, flow.candidate,
                                  ("src/a.txt", "tests/a.txt", "config/checks.json"), captures)
    flow.workflow = flow.compose(candidate_reviews=reviews)
    flow.app.workflows = flow.workflow
    other_root = flow.root / "independent-candidate"
    other_root.mkdir(mode=0o700)
    other = CandidateProfile(flow.profile, flow.candidate_profile.worktree_id,
                             flow.candidate_profile.repository_id, other_root)

    contender_task = flow.r.queued()

    def current_contender():
        current = flow.r.core.get_task(flow.r.handle, contender_task.id)
        assert not current.human_hold
        assert flow.r.core.scheduling.explain(flow.r.handle, current.id)["state"] == "ready"
        other.validate()

    def contend():
        with other.merge_turn(deadline=flow.plan.deadline, clock=lambda: flow.r.now,
                              check=current_contender):
            progressed.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            delivery = pool.submit(flow.run)
            assert entered.wait(4), "Actual candidate review was not reached"
            contender = pool.submit(contend)
            before_release = progressed.wait(0.3)
            release.set()
            result = delivery.result(timeout=12)
            contender.result(timeout=5)
        facts = {"scope": "Actual public merge workflow, authored immutable review, separate physical repository-turn contender",
                 "contender_progressed_during_review": before_release, "result": result}
        (tmp_path / "repository-turn-facts.json").write_text(json.dumps(facts, indent=2) + "\n")
        assert before_release and result.get("ok"), facts
        assert len(captures.calls) == 1, captures.calls
    finally:
        release.set()
        flow.close()


def test_repository_wait_is_cumulative_across_review_reentry_and_original_key(tmp_path, monkeypatch):
    import fcntl
    import json
    import os
    from types import SimpleNamespace
    from ai_task_manager.adapters.git.candidates import profile as profile_module

    flow = Flow(tmp_path / "owned")
    ticks, first = [0.0], [True]
    descriptor = os.open(flow.candidate_profile.source["common"], os.O_RDONLY)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def wait(seconds):
        ticks[0] += seconds
        if first[0] and ticks[0] >= 60:
            first[0] = False
            fcntl.flock(descriptor, fcntl.LOCK_UN)

    monkeypatch.setattr(profile_module, "time", SimpleNamespace(monotonic=lambda: ticks[0], sleep=wait))
    captures = Captures(lambda: flow.r.now)
    resolve = captures.resolve

    def review(inspection):
        value = resolve(inspection)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return value

    captures.resolve = review
    reviews = CodeCandidateReviews(flow.r.core, flow.r.handle, flow.candidate,
                                  ("src/a.txt", "tests/a.txt", "config/checks.json"), captures)
    flow.workflow = flow.compose(candidate_reviews=reviews)
    flow.app.workflows = flow.workflow
    try:
        result = flow.run()
        ref = next(iter(flow.workflow.references))
        first_wait = flow.workflow.store.read(ref)["repository_wait"]
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        replay = flow.run()
        final_wait = flow.workflow.store.read(ref)["repository_wait"]
        facts = {"scope": "Actual flock and workflow journal, controlled monotonic acquisition time; no model",
                 "first": result, "same_original_key": replay, "first_wait": first_wait,
                 "final_wait": final_wait, "timings": flow.workflow.timings}
        (tmp_path / "cumulative-wait-facts.json").write_text(json.dumps(facts, indent=2) + "\n")
        assert not result["ok"] and not replay["ok"], facts
        assert first_wait == final_wait == {"seconds": 120.0, "pending": False}, facts
        assert len(captures.calls) == 1
        assert git(flow.tree, "rev-parse", "refs/heads/" + flow.target) == flow.old
        assert not flow.r.core.get_task(flow.r.handle, flow.task.id).delivered_once
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        flow.close()



def test_nested_failed_turn_preserves_outer_preparation_state(tmp_path):
    from ai_task_manager.domains.work.errors import AccessDenied

    flow = Flow(tmp_path / "owned")
    profile = flow.candidate_profile
    try:
        with profile.merge_turn(deadline=flow.plan.deadline, clock=lambda: flow.r.now,
                                check=flow.workflow._ready):
            original = profile.inherited_turn()
            with pytest.raises(AccessDenied, match="wait budget expired"):
                with profile.merge_turn(deadline=flow.r.now, clock=lambda: flow.r.now,
                                        check=flow.workflow._ready):
                    pytest.fail("Expired nested turn cannot acquire")
            assert profile.inherited_turn() == original
            with profile.immutable_preparation():
                assert profile.inherited_turn() is None
            assert profile.inherited_turn() == original
    finally:
        flow.close()


def test_committed_receipt_reconciles_without_new_repository_wait(tmp_path):
    from tests.merge.workflows.test_workflow import FaultOnce

    flow = Flow(tmp_path / "owned")
    flow.check_inputs.select_retained = flow.check_inputs.discover
    try:
        flow.workflow.faults = FaultOnce("after_publication")
        assert not flow.run()["ok"]
        ref = next(iter(flow.workflow.references))
        flow.workflow.store.update(ref, None, repository_wait={"seconds": 120.0, "pending": True})
        flow.workflow.faults = None
        result = flow.resume(ref)
        assert result.get("ok") and result["data"]["state"] == "done", result
        assert flow.workflow.store.read(ref)["repository_wait"] == {"seconds": 120.0, "pending": True}
    finally:
        flow.close()
