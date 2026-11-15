"""Public original-key continuation over authored provider HTTP replies."""

import json

import pytest

from ai_task_manager.config.service import load_service

from .test_decision_recipient import env  # noqa: F401


def original_request(f, key):
    return {
        "task_id": f.task["id"],
        "writing_ref": json.loads(f.request["payload"])["approval_ref"],
        "operation_key": key,
    }


def count(f, text):
    return sum(text in request["query"] for request, _ in f.provider.requests)


def test_public_same_key_recovers_known_comment_without_reposting(env):  # noqa: F811
    f = env
    data = original_request(f, "known-readback-recovery")
    f.provider.transform = lambda query, variables, result: (
        {"comment": None} if "query ReadComment" in query else result
    )
    first = f.service.call("task_decision_request", data)
    assert first["ok"] and first["outcome"] == "uncertain", first
    assert first["data"]["comment_id"] and len(f.provider.comments) == 2
    f.provider.transform = None
    recovered = load_service(f.config).call("task_decision_request", data)
    assert recovered["ok"] and recovered["outcome"] == "confirmed", recovered
    assert recovered["data"]["id"] == first["data"]["id"]
    assert recovered["data"]["comment_id"] == first["data"]["comment_id"]
    assert count(f, "mutation CreateComment") == 2 and len(f.provider.comments) == 2
    changed = load_service(f.config).call(
        "task_decision_request", {**data, "writing_ref": "different-approved-input"}
    )
    assert not changed["ok"] and len(f.provider.comments) == 2


@pytest.mark.parametrize("effect", ["unknown_comment", "unknown_label", "completed"])
def test_public_unknown_effects_and_completed_replay_do_not_send(env, effect):  # noqa: F811
    f = env
    if effect == "completed":
        data = original_request(f, "request-choice")
        f.provider.edit(f.task["issue_id"], title="Later provider-owned title")
    else:
        data = original_request(f, effect)
        if effect == "unknown_label":
            f.provider.issues[f.task["issue_id"]]["decision_labels"] = []
        f.provider.transform = lambda query, variables, result: (
            {"commentCreate": {"success": False}}
            if effect == "unknown_comment" and "mutation CreateComment" in query
            else {"issueUpdate": {"success": False}}
            if effect == "unknown_label" and "mutation DecisionLabelsUpdate" in query
            else result
        )
        first = f.service.call("task_decision_request", data)
        if effect == "unknown_comment":
            assert first["ok"] and first["outcome"] == "uncertain", first
            assert first["data"]["comment_id"] is None
        else:
            assert not first["ok"]
        f.provider.transform = None
    comments, labels = count(f, "mutation CreateComment"), count(f, "mutation DecisionLabelsUpdate")
    for _ in range(2):
        replay = load_service(f.config).call("task_decision_request", data)
        if effect == "completed":
            assert replay["ok"] and replay["outcome"] == "confirmed", replay
        elif effect == "unknown_label":
            assert replay["ok"] and replay["outcome"] == "uncertain", replay
        else:
            assert not replay["ok"] or replay["outcome"] == "uncertain", replay
    assert count(f, "mutation CreateComment") == comments
    assert count(f, "mutation DecisionLabelsUpdate") == labels
