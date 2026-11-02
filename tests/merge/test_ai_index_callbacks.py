from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from ai_task_manager.domains.work.errors import WorkError

VARIANTS = [
    "operation_ref",
    "binding_digest",
    "kind",
    "sequence",
    "source_digest",
    "range_ref",
    "head",
    "issue_id",
    "team_id",
    "principal_id",
    "changed_duplicate",
    "exact_duplicate",
]


@pytest.mark.parametrize("variant", VARIANTS, ids=VARIANTS)
def test_callback_scope_and_sequence(harness, variant):
    harness.publish()
    cb = harness.callback()
    if variant in {"changed_duplicate", "exact_duplicate"}:
        first = harness.callbacks.submit(cb, harness.version())
        if variant == "changed_duplicate":
            before = harness.journal.events(harness.ref)
            with pytest.raises(WorkError):
                harness.callbacks.submit(replace(cb, content_digest="f" * 64), harness.version())
            assert harness.journal.events(harness.ref) == before
        else:
            assert harness.callbacks.submit(cb, harness.version()) == first
            barrier = Barrier(2)

            def send(_):
                barrier.wait(timeout=3)
                return harness.callbacks.dispatch(harness.ref, 1)

            with ThreadPoolExecutor(max_workers=2) as pool:
                answers = list(pool.map(send, range(2)))
            assert answers[0] == answers[1]
            assert len(harness.downstream.calls) == 1
    else:
        value = (
            2
            if variant == "sequence"
            else (
                "f" * 64
                if variant.endswith("digest")
                else "f" * 40
                if variant == "head"
                else "foreign"
            )
        )
        bad = replace(cb, **{variant: value})
        before = harness.journal.events(harness.ref)
        with pytest.raises(WorkError):
            harness.callbacks.submit(bad, harness.version())
        assert harness.journal.events(harness.ref) == before
        assert harness.downstream.calls == []


VARIANTS = ["part_incomplete", "other_required_work", "core_denied", "complete_delegation"]


@pytest.mark.parametrize("variant", VARIANTS, ids=VARIANTS)
def test_partial_work_never_implies_whole_issue_delivery(harness, variant):
    harness.publish()
    if variant == "other_required_work":
        harness.downstream.required.add("independent-part-c")
        harness.downstream.completed = {"part-a", "part-b"}
    elif variant == "complete_delegation":
        harness.downstream.completed = set(harness.downstream.required)
    elif variant == "core_denied":
        harness.downstream.mode = "deny"
    harness.queue(part_id="partial")
    harness.callbacks.dispatch(harness.ref, 1)
    assert len(harness.downstream.calls) == 1
    expected = "accepted_intent" if variant == "complete_delegation" else "denied"
    assert harness.journal.callback(harness.ref, 1)["state"] == expected
    assert "publication" in harness.current["facts"]
    assert "status" not in harness.current["facts"]
    assert "all_required_work_complete" not in harness.current["facts"]


VARIANTS = [
    "missing_approval",
    "stale_approval",
    "foreign_approval",
    "caller_boolean",
    "wrong_head",
    "selected",
    "carried",
    "not_run",
    "unexpected_observation_fields",
]


@pytest.mark.parametrize("variant", VARIANTS, ids=VARIANTS)
def test_prose_and_evidence_are_not_minted(harness, variant):
    harness.publish()
    if variant == "unexpected_observation_fields":
        before = harness.current
        with pytest.raises(WorkError):
            harness.observe("database", {"state": "removed", "foreign_payload": "sentinel"})
        assert harness.current == before
        assert harness.downstream.calls == []
    elif variant in {"caller_boolean", "wrong_head"}:
        with pytest.raises(WorkError):
            harness.queue(
                **({"approval_ref": True} if variant == "caller_boolean" else {"head": "f" * 40})
            )
        assert harness.downstream.calls == []
    else:
        if variant in {"selected", "carried", "not_run"}:
            harness.observe("test_evidence", {"coverage": variant, "head": "b" * 40})
            harness.queue(result_ref=variant)
        else:
            harness.queue(
                approval_ref={
                    "missing_approval": "absent",
                    "stale_approval": "stale",
                    "foreign_approval": "foreign",
                }[variant]
            )
        harness.callbacks.dispatch(harness.ref, 1)
        assert harness.journal.callback(harness.ref, 1)["state"] == "denied"
        assert harness.downstream.effects == []
        if variant in {"selected", "carried", "not_run"}:
            assert harness.current["facts"]["test_evidence"]["coverage"] == variant


VARIANTS = ["delivered", "duplicate", "uncertain", "downgrade_request"]


@pytest.mark.parametrize("variant", VARIANTS, ids=VARIANTS)
def test_delivered_followup_and_rollback_are_separate_facts(harness, variant):
    harness.publish()
    cb, receipt = harness.queue(kind="rolled_back")
    if variant == "uncertain":
        harness.downstream.mode = "uncertain"
    harness.callbacks.dispatch(harness.ref, 1)
    assert harness.current["facts"]["followup_obligation"]["state"] == "pending_review"
    assert harness.current["facts"]["publication"]["new"] == "b" * 40
    assert "status" not in harness.current["facts"]
    if variant == "duplicate":
        assert harness.callbacks.submit(cb, harness.version()) == receipt
        harness.callbacks.dispatch(harness.ref, 1)
        assert harness.downstream.effects == ["rolled_back"]
    elif variant == "downgrade_request":
        with pytest.raises(WorkError):
            harness.queue(kind="set_status", sequence=2)
        assert harness.downstream.effects == ["rolled_back"]
    elif variant == "uncertain":
        assert harness.journal.callback(harness.ref, 1)["state"] == "uncertain"


VARIANTS = ["CHECKS PASSED", "WOULD NOT MERGE", "MERGED", "NOT MERGED", "NOTHING TO MERGE"]


@pytest.mark.parametrize("answer", VARIANTS, ids=VARIANTS)
def test_result_classes_and_cleanup_warnings(harness, answer):
    if answer == "MERGED":
        harness.publish()
    else:
        harness.launch()
        if answer == "NOTHING TO MERGE":
            harness.observe("content_equal", {"confirmed": True})
    harness.observe("result", {"answer": answer})
    harness.observe("database", {"state": "removed"})
    harness.observe("database", {"state": "warning"})
    assert harness.current["facts"]["result"] == answer
    assert harness.current["facts"]["database"] == "warning"
    if answer == "MERGED":
        harness.queue()
        harness.downstream.mode = "deny"
        harness.callbacks.dispatch(harness.ref, 1)
        assert harness.current["facts"]["result"] == "MERGED"
        assert harness.current["facts"]["heavy_obligation"] == "pending"
    else:
        assert "publication" not in harness.current["facts"]
