"""Real core transitions with synthetic, independently configured authority controls."""

from dataclasses import replace

import pytest

from ai_task_manager.adapters.decisions.authority import HumanInstruction
from ai_task_manager.application.service.facade import ApplicationService
from ai_task_manager.domains.work.models import IssueStatus

from .helpers import ServiceRig


def request(task, action, key, **extras):
    return {
        "task_id": task.id,
        "action": action,
        "expected_version": task.version,
        "decision_revision": task.decision_revision,
        "operation_key": key,
        **extras,
    }


def observe(env, task, target, ref="observed-human"):
    env.observations.instructions[ref] = HumanInstruction(
        env.rig.scope,
        task.id,
        target,
        task.decision_revision,
        "Observed acceptance",
        "synthetic-public-user-message",
        task.version,
        task.prose_digest,
    )
    return ref


def review_task(env):
    task, attempt = env.rig.running()
    task, _ = env.rig.settle(task, attempt)
    task = env.rig.complete(task)
    return env.rig.transition(task, IssueStatus.IN_REVIEW, "request_review")


def test_a31_retry_and_new_service_keep_one_effect_and_current_facts(tmp_path):
    env = ServiceRig(tmp_path)
    task = env.rig.task()
    data = request(task, "enqueue", "durable-public-retry")
    first = env.service.call("task_action", data)
    assert first["ok"]
    again = env.service.call("task_action", data)
    assert again["ok"] and again["data"] == first["data"]
    assert len(env.provider.calls) == 1
    reopened = ApplicationService(
        env.rig.core,
        env.rig.handle,
        decisions=env.decisions,
        provider=env.provider,
        grant_ref="host-grant",
    )
    assert reopened.call("task_action", data)["data"] == first["data"]
    changed = {**data, "action": "dequeue"}
    assert reopened.call("task_action", changed)["outcome"] == "invalid_request"
    current = env.rig.current(task)
    env.rig.transition(current, IssueStatus.BACKLOG, "dequeue")
    # Replay reports current authority, not the old Todo response.
    assert reopened.call("task_action", data)["data"]["observed_status"] == "Backlog"


def test_a31_lost_public_reply_reuses_exact_core_proof_reference(tmp_path, monkeypatch):
    env = ServiceRig(tmp_path)
    task = env.rig.task()
    data = request(task, "enqueue", "lost-public-reply")
    original = env.service.confirm
    monkeypatch.setattr(
        env.service, "confirm", lambda _: (_ for _ in ()).throw(OSError("lost reply"))
    )
    first = env.service.call("task_action", data)
    assert first["outcome"] == "failed"
    assert env.rig.current(task).desired_status == "Todo"
    monkeypatch.setattr(env.service, "confirm", original)
    recovered = env.service.call("task_action", data)
    assert recovered["ok"] and recovered["data"]["observed_status"] == "Todo"
    assert len(env.provider.calls) == 1


@pytest.mark.parametrize(
    "missing", ["task_id", "action", "expected_version", "decision_revision", "operation_key"]
)
def test_action_omitted_boundary_fields_have_no_effect(tmp_path, missing):
    env = ServiceRig(tmp_path)
    task = env.rig.task()
    data = request(task, "enqueue", "omitted-field")
    del data[missing]
    assert not env.service.call("task_action", data)["ok"]
    assert env.rig.current(task) == task and env.provider.calls == []


@pytest.mark.parametrize("target", list(IssueStatus))
def test_review_cannot_use_an_agent_grant_as_human_acceptance(tmp_path, target):
    env = ServiceRig(tmp_path)
    task = review_task(env)
    result = env.service.call(
        "task_action", request(task, "review_decision", "no-human", review_target=target)
    )
    assert result["outcome"] == "denied"
    assert env.rig.current(task) == task


@pytest.mark.parametrize("target", [x for x in IssueStatus if x != IssueStatus.IN_PROGRESS])
def test_a30_observed_review_exits_use_destination_guards(tmp_path, target):
    env = ServiceRig(tmp_path)
    task = review_task(env)
    ref = observe(env, task, target)
    proofs = env.rig.delivery(task)
    proofs["duplicate_target"] = env.rig.proof(
        "duplicate_target", task.id, {"canonical_issue_id": "TST:canonical"}
    )
    response = env.service.call(
        "task_action",
        request(
            task,
            "review_decision",
            "observed-exit",
            review_target=target,
            human_ref=ref,
            evidence_refs=proofs,
        ),
    )
    assert response["ok"], response
    current = env.rig.current(task)
    assert (
        current.observed_status == target
        and current.decision_revision == task.decision_revision + 1
    )


def test_a30_human_hold_release_records_new_authority_not_agent_label(tmp_path):
    env = ServiceRig(tmp_path)
    task, attempt = env.rig.running()
    task, _ = env.rig.settle(task, attempt, state="paused")
    task = env.rig.transition(task, IssueStatus.ON_HOLD, "hold")
    held = env.rig.core.decisions.record(
        env.rig.handle,
        task.id,
        task.version,
        env.rig.proof(
            "human_decision", task.id, {"previous_revision": task.decision_revision, "hold": True}
        ),
        env.rig.key(),
    )
    denied = env.service.call("task_action", request(held, "enqueue", "agent-cannot-release"))
    assert denied["outcome"] == "invalid_request"
    ref = observe(env, held, "Todo")
    accepted = env.service.call(
        "task_action", request(held, "enqueue", "actual-release", human_ref=ref)
    )
    assert accepted["ok"] and accepted["data"]["human_hold"] is False
    assert accepted["data"]["observed_status"] == "Todo"
    assert accepted["data"]["decision_revision"] == held.decision_revision + 1
    newer = env.rig.current(held)
    assert not env.service.call(
        "task_action", request(newer, "dequeue", "reuse-old-source", human_ref=ref)
    )["ok"]


def test_a30_stale_human_revision_and_changed_scope_refuse_before_action(tmp_path):
    env = ServiceRig(tmp_path)
    task = review_task(env)
    ref = observe(env, task, "Backlog")
    env.observations.instructions[ref] = replace(
        env.observations.instructions[ref], prose_digest="old-work"
    )
    assert (
        env.service.call(
            "task_action",
            request(task, "review_decision", "wrong-work", review_target="Backlog", human_ref=ref),
        )["outcome"]
        == "denied"
    )
    assert env.rig.current(task) == task


def test_intake_without_backlog_confirmation_cannot_enqueue(tmp_path):
    env = ServiceRig(tmp_path, provider=False)
    task = env.rig.task(confirmed=False)
    assert env.service.call("task_show", {"task_id": task.id})["ok"]
    response = env.service.call("task_action", request(task, "enqueue", "premature-queue"))
    assert response["outcome"] == "denied"
    assert env.rig.current(task) == task


def test_actual_current_human_intent_can_continue_when_missing_evidence_arrives(tmp_path):
    env = ServiceRig(tmp_path)
    task = review_task(env)
    ref = observe(env, task, "In Dev")
    missing = env.service.call(
        "task_action",
        request(
            task,
            "review_decision",
            "waiting-for-merge-proof",
            review_target="In Dev",
            human_ref=ref,
        ),
    )
    assert not missing["ok"]
    current = env.rig.current(task)
    assert (
        current.observed_status == "In Review"
        and current.decision_revision == task.decision_revision + 1
    )
    continued = env.service.call(
        "task_action",
        request(
            current,
            "review_decision",
            "continue-observed-intent",
            review_target="In Dev",
            human_ref=current.decision_ref,
            evidence_refs=env.rig.delivery(current),
        ),
    )
    assert continued["ok"] and continued["data"]["observed_status"] == "In Dev", continued
