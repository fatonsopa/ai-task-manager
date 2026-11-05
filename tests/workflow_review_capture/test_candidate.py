"""Credential-free native protocol fixture for exact prepared-candidate review."""

from types import SimpleNamespace

from ai_task_manager.adapters.workflow_reviews.capture_store import CaptureStore, NativeReviewSource
from ai_task_manager.adapters.workflow_reviews.code_candidates import CodeCandidateReviews
from tests.merge.workflows.helpers import Flow
from tests.workflow_review_capture.helpers import reviewer

FILES = ("src/a.txt", "tests/a.txt", "config/checks.json")


class InspectionRecorder:
    def resolve(self, inspection):
        self.inspection = inspection
        return None


def test_G1_authored_native_protocol_captures_exact_candidate(tmp_path):
    flow = Flow(tmp_path / "author")
    producer = None
    try:
        candidate = flow.candidate.prepare(flow.grant)
        recorder = InspectionRecorder()
        reviews = CodeCandidateReviews(flow.r.core, flow.r.handle, flow.candidate, FILES, recorder)
        assert reviews.read(flow.task, candidate) is None
        inspection = recorder.inspection
        author = SimpleNamespace(
            r=flow.r,
            runtime=flow.runtime,
            artifacts=SimpleNamespace(prepare=lambda *args: inspection),
            scope=flow.r.scope,
            task=flow.task,
            saved={"digest": "authored-prepared-candidate"},
        )
        verdict = {
            "context_digest": inspection.digest,
            "criteria": [
                {"id": unit, "judgment": "accept", "reason": "Authored protocol observation"}
                for unit in flow.task.required_work
            ],
            "reason": "Authored credential-free protocol fixture; no actual model judgment",
        }
        inspected, producer = reviewer(author, tmp_path / "reviewer", verdict)
        store = CaptureStore(tmp_path / "captures")
        captured = producer.capture(inspected, store)
        assert captured.context_digest == inspection.digest
        assert captured.reviewer_id != flow.attempt.worker_id
        assert captured.accepted(flow.task.required_work, flow.r.now)
        assert (
            producer.workers.core.get_task(producer.handle, producer.task_id).code_bearing is False
        )
        assert all(native.process._closed for native in producer.workers.hosts.owned.values())
        replay = CodeCandidateReviews(
            flow.r.core, flow.r.handle, flow.candidate, FILES, NativeReviewSource(store)
        )
        result = replay.read(flow.task, candidate)
        assert result["candidate_commit"] == candidate.commit
        assert result["candidate_tree"] == candidate.tree
        assert result["context_digest"] == inspection.digest
        replay.check_prepared(candidate.commit)
        assert len(producer.workers.dispatches) == 1
    finally:
        if producer is not None:
            for native in producer.workers.hosts.owned.values():
                if native.process is not None and not native.process._closed:
                    assert native.stop().confirmed
        flow.close()
