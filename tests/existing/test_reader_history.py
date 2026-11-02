import json
from pathlib import Path

import pytest

from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.domains.work.existing import ExistingBlocked, assess
from tests.linear.helpers import uid

from .helpers import SECOND, UPDATED, span


def test_ea01_ea05_ea06_search_pages_and_explicit_identifier_resolution(rig, provider):
    first = provider.seed()
    second = provider.seed()
    before = rig.effects()
    page = rig.intake.find("ownership", 1)
    assert [row["issue_id"] for row in page["candidates"]] == [first]
    assert page["has_more"]
    last = rig.intake.find("ownership", 1, page["next_cursor"])
    assert [row["issue_id"] for row in last["candidates"]] == [second]
    assert not last["has_more"]
    inspected = rig.intake.inspect(provider.issues[second]["identifier"])
    assert inspected["issue_id"] == second
    assert inspected["binding"] == {"state": "unbound"}
    assert rig.effects() == before
    task = rig.admit(second)
    assert task.issue_id == second and len(rig.core.list_tasks(rig.handle)) == 1
    rig.assert_no_provider_mutations()


@pytest.mark.parametrize("field", ["assignee", "team", "project"])
def test_ea02_ea03_foreign_candidates_never_trigger_prose_queries(rig, provider, field):
    issue = provider.seed(title="Private sentinel title")
    provider.issues[issue][field] = {"id": uid(999), "key": "FOREIGN"}
    before = rig.effects()
    assert rig.intake.find("sentinel")["candidates"] == []
    with pytest.raises(AccessDenied):
        rig.intake.inspect(issue)
    queried = [request["query"] for request, _ in provider.requests]
    assert not any("ExistingTitle" in query or "ExistingProse" in query for query in queried)
    assert b"Private sentinel" not in Path(rig.journal.path).read_bytes()
    assert rig.effects() == before


def test_ea04_scope_changes_on_prose_response_are_not_persisted(rig, provider):
    issue = provider.seed()

    def change(request, result):
        if "ExistingProse" in request["query"]:
            result["issue"]["assignee"] = {"id": uid(999)}
            result["issue"]["description"] = "FOREIGN_PRIVATE_SENTINEL"

    provider.hook = change
    with pytest.raises(AccessDenied):
        rig.reader.inspect(issue)
    assert b"FOREIGN_PRIVATE_SENTINEL" not in open(rig.journal.path, "rb").read()
    assert not rig.core.list_tasks(rig.handle)


def test_ea07_partial_graphql_errors_are_sanitized(rig, provider):
    provider.seed()
    provider.fail_next = (
        200,
        json.dumps(
            {"data": {"issues": {"nodes": []}}, "errors": [{"message": "PRIVATE_SENTINEL"}]}
        ).encode(),
    )
    before = rig.effects()
    with pytest.raises(LinearError) as caught:
        rig.intake.find("ownership")
    assert caught.value.category == "graphql_failure"
    assert "PRIVATE_SENTINEL" not in str(caught.value)
    assert rig.effects() == before


@pytest.mark.parametrize("malformation", ["duplicate", "cursor", "type", "limit"])
def test_ea08_pagination_ambiguity_blocks(rig, provider, malformation):
    issue = provider.seed("Todo")
    provider.page_size = 1
    if malformation == "limit":
        rig.client.max_pages = 1
    else:

        def corrupt(result):
            connection = result["issue"]["stateHistory"]
            if malformation == "duplicate":
                connection["nodes"][0]["id"] = "same-span"
            elif malformation == "cursor":
                connection["pageInfo"] = {"hasNextPage": True, "endCursor": "0"}
            else:
                connection["pageInfo"]["hasNextPage"] = 1
            return result

        provider.history_override = corrupt
    with pytest.raises(LinearError):
        rig.reader.inspect(issue)
    assert not rig.core.list_tasks(rig.handle)


@pytest.mark.parametrize("change", [{"archivedAt": UPDATED}, {"trashed": True}])
def test_ea09_archived_or_removed_is_not_admissible(rig, provider, change):
    issue = provider.seed()
    provider.issues[issue].update(change)
    with pytest.raises(AccessDenied):
        rig.reader.inspect(issue)
    with pytest.raises(AccessDenied):
        rig.reader.inspect("invalid\ncommand")


@pytest.mark.parametrize(
    "status,category",
    [
        (401, "authentication"),
        (403, "authentication"),
        (429, "rate_limited"),
        (500, "http_failure"),
    ],
)
def test_ea10_http_failures_do_not_create(rig, provider, status, category):
    issue = provider.seed()
    provider.fail_next = status, b"PRIVATE_SENTINEL"
    before = rig.effects()
    with pytest.raises(LinearError) as caught:
        rig.reader.inspect(issue)
    assert caught.value.category == category
    assert rig.effects() == before
    rig.assert_no_provider_mutations()


@pytest.mark.parametrize("status", ["Backlog", "Todo"])
def test_ea11_ea12_complete_timeline_is_binding_only(rig, provider, status):
    issue = provider.seed(status)
    provider.page_size = 1
    inspected = rig.intake.inspect(issue)
    assert inspected["history"]["assessment"] == "eligible"
    task = rig.admit(issue)
    assert task.observed_status == status and task.sync_state == "confirmed"
    assert not task.queued_once and task.execution_state == "idle"
    assert not rig.effects()["attempts"] and not rig.effects()["outbox"]


@pytest.mark.parametrize(
    "kind",
    ["prefix", "gap", "overlap", "duplicate", "open", "last", "missing", "renamed", "initial_todo"],
)
def test_ea13_ea14_ea15_ea16_missing_or_ambiguous_history_blocks(rig, provider, kind):
    issue = provider.seed("Todo")
    history = provider.histories[issue]
    if kind == "prefix":
        history.pop(0)
    elif kind == "gap":
        history[0]["endedAt"] = "2026-11-01T00:00:30Z"
    elif kind == "overlap":
        history[0]["endedAt"] = UPDATED
    elif kind == "duplicate":
        history[1]["id"] = history[0]["id"]
    elif kind == "open":
        history[0]["endedAt"] = None
    elif kind == "last":
        history[-1]["stateId"] = history[0]["stateId"]
    elif kind == "missing":
        history[0]["state"] = None
    elif kind == "renamed":
        history[0]["state"]["updatedAt"] = SECOND
    else:
        history[0] = span("Todo", end=SECOND)
    try:
        snapshot, _ = rig.reader.inspect(issue)
    except LinearError:
        assert kind == "duplicate"
        return
    assessment = assess(snapshot)
    assert not assessment.eligible
    before = rig.effects()
    with pytest.raises((ExistingBlocked, EvidenceRequired)):
        rig.admit(issue)
    assert rig.effects() == before


@pytest.mark.parametrize("historic", ["In Dev", "In Staging", "Done", "Canceled", "Duplicate"])
def test_ea17_ea18_ea69_delivery_and_terminal_history_survive_reset(rig, provider, historic):
    issue = provider.seed("Todo")
    provider.histories[issue] = [
        span("Backlog", end=SECOND),
        span(historic, SECOND, "2026-11-01T00:01:30Z", 1),
        span("Todo", "2026-11-01T00:01:30Z", number=2),
    ]
    inspected = rig.intake.inspect(issue)
    assert (
        historic in {"Canceled", "Duplicate"} or "ever_delivered" in inspected["history"]["reasons"]
    )
    before = rig.effects()
    with pytest.raises((ExistingBlocked, EvidenceRequired)):
        rig.admit(issue)
    assert rig.effects() == before
    provider.histories[issue] = [span("Backlog", end=SECOND), span("Todo", SECOND, number=1)]
    with pytest.raises((ExistingBlocked, EvidenceRequired)):
        rig.admit(issue)
    assert not rig.core.list_tasks(rig.handle)


def test_ea19_ea45_final_revision_recheck_refuses_mixed_snapshot(rig, provider):
    issue = provider.seed("Todo")
    before = rig.effects()

    def change(request, result):
        if "ExistingStateHistory" in request["query"]:
            provider.issues[issue]["updatedAt"] = "2026-11-01T00:03:00Z"

    provider.hook = change
    with pytest.raises(LinearError, match="existing_issue_changed"):
        rig.reader.inspect(issue)
    assert rig.effects() == before


@pytest.mark.parametrize("field", ["state", "prose", "owner"])
def test_ea45_final_check_rejects_each_provider_change(rig, provider, field):
    issue = provider.seed("Todo")
    before = rig.effects()

    def change(request, result):
        if "ExistingStateHistory" in request["query"]:
            target = provider.issues[issue]
            target["updatedAt"] = "2026-11-01T00:03:00Z"
            if field == "state":
                target["state"] = {"id": provider.histories[issue][0]["stateId"], "name": "Backlog"}
            elif field == "prose":
                target["description"] += " A newer authorized edit."
            else:
                target["assignee"] = {"id": uid(999)}

    provider.hook = change
    with pytest.raises((LinearError, AccessDenied)):
        rig.reader.inspect(issue)
    assert rig.effects() == before


def test_ea09_missing_issue_has_no_receipt_or_effect(rig):
    from uuid import uuid4

    before = rig.effects()
    with pytest.raises(AccessDenied):
        rig.reader.inspect(str(uuid4()))
    assert rig.effects() == before


def test_ea16_actual_historic_name_rename_is_not_negative_delivery_evidence(rig, provider):
    issue = provider.seed("Todo")
    state = provider.histories[issue][0]["state"]
    state["name"], state["updatedAt"] = "Renamed historic state", SECOND
    inspected = rig.intake.inspect(issue)
    assert "historic_state_classification_unknown" in inspected["history"]["reasons"]
    with pytest.raises(ExistingBlocked):
        rig.admit(issue)
    assert not rig.core.list_tasks(rig.handle)


@pytest.mark.parametrize("status", ["In Progress", "On Hold", "In Review"])
def test_ea20_unbound_guarded_status_never_invents_ownership(rig, provider, status):
    issue = provider.seed(status)
    result = rig.intake.inspect(issue)
    assert result["history"]["assessment"] == "blocked"
    with pytest.raises((ExistingBlocked, EvidenceRequired)):
        rig.admit(issue)
    assert not rig.core.list_tasks(rig.handle)
