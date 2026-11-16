"""Decision publication over authored HTTP, never genuine human/provider acceptance."""

import copy
import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.linear.progress_store import ProgressStore
from ai_task_manager.adapters.linear.prose import LinearMarkdownRenderer
from ai_task_manager.application.intake import prose_digest
from ai_task_manager.application.linear_sync.progress import (
    ProgressApprovalContext,
    ProgressPublisher,
    progress_prose,
)
from ai_task_manager.domains.work.errors import ApprovalRequired, NotFound
from ai_task_manager.domains.work.models import Approval, TaskProse
from tests.linear.helpers import CONNECTION, EXPECTED, LinearRig, uid
from tests.linear.live.adapter_fixture import AdapterProvider


class DecisionProvider(AdapterProvider):
    def __init__(self):
        self.managed = {
            "id": uid(600),
            "name": "Waiting For Decision",
            "isGroup": False,
            "archivedAt": None,
            "team": None,
        }
        self.label_candidates = [self.managed]
        self.creator_app = False
        super().__init__()

    @staticmethod
    def human(identifier):
        return {
            "id": identifier,
            "app": False,
            "active": True,
            "organization": {"id": EXPECTED.workspace_id},
            "url": "https://linear.app/fixture/profiles/" + identifier,
        }

    def _fixture_query(self, query, variables):
        if "query DecisionLabels" in query:
            return {
                "issueLabels": {
                    "nodes": copy.deepcopy(self.label_candidates),
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        if "query DecisionContext" in query:
            issue = copy.deepcopy(self.issues[variables["id"]])
            issue["creator"] = (
                dict(self.human(CONNECTION.agent_id), app=True)
                if self.creator_app
                else self.human(EXPECTED.viewer_id)
            )
            if issue["assignee"]:
                issue["assignee"] = self.human(issue["assignee"]["id"])
            issue["labels"] = {
                "nodes": [
                    {"id": value}
                    for value in self.issues[variables["id"]].get("decision_labels", [])
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
            return {
                "viewer": {"id": self.viewer_id},
                "organization": {"id": EXPECTED.workspace_id, "urlKey": "fixture"},
                "issue": issue,
            }
        if "mutation DecisionLabelsUpdate" in query:
            item = self.issues[variables["id"]]
            values = variables["input"]
            labels = set(item.get("decision_labels", []))
            labels.update(values.get("addedLabelIds", []))
            labels.difference_update(values.get("removedLabelIds", []))
            item["decision_labels"] = sorted(labels)
            item["updatedAt"] = self.tick()
            result = copy.deepcopy(item)
            result["labels"] = {
                "nodes": [{"id": value} for value in labels],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
            return {"issueUpdate": {"success": True, "issue": result}}
        return super()._fixture_query(query, variables)


class DecisionApprovals:
    def __init__(self, rig):
        self.rig = rig
        self.records = {}
        self.proofs = {}

    def approve(self, metadata):
        # Authored legacy approval fixture; production uses its signed original
        # content, not this historical template, during recovery.
        body = "## Context\n\n" + metadata["context"] + "\n\n## Options considered\n\n"
        body += "\n\n".join(
            "### Option " + o["id"] + ": " + o["label"] + "\n\n" + o["description"]
            for o in metadata["options"]
        )
        body += (
            "\n\n## Recommended option ("
            + metadata["recommendation"]
            + ")\n\n"
            + metadata["rationale"]
        )
        body += (
            "\n\n## Outcome\n\nPending an observed human choice.\n\n" + metadata["recipient"]["url"]
        )
        prose = TaskProse(metadata["title"], body, False, ("decision",))
        rendered = LinearMarkdownRenderer().render_decision(prose, metadata["recipient"]["url"])
        prose = replace(prose, body=rendered.body)
        ref = "writing-decision:" + str(uuid4())
        self.records[ref] = {"purpose": "decision", "kind": "decision", "decision": metadata}
        self.records[ref]["prose"] = asdict(prose)
        self.proofs[ref] = Approval(
            prose_digest(prose, rendered), self.rig.policy, rendered.representation
        )
        return prose, ref

    def record(self, ref):
        return self.records[ref]

    def verify_approval(self, ref):
        return self.proofs[ref]


@pytest.fixture
def env(tmp_path):
    provider = DecisionProvider()
    try:
        rig = LinearRig(tmp_path, provider)
        task = rig.task()
        approvals = DecisionApprovals(rig)
        publisher = ProgressPublisher(
            rig.core,
            rig.client,
            rig.journal,
            approval_policy=rig.policy,
            approvals=approvals,
            approval_context=ProgressApprovalContext(rig.scope, task.id, task.prose_digest),
            clock=lambda: rig.now,
        )
        yield rig, task, publisher, approvals
    finally:
        provider.close()


def fields():
    return {
        "title": "Fixture verification routes",
        "context": "Two bounded checks cover the same fixture requirement.",
        "options": [
            {
                "id": "A",
                "label": "Direct check",
                "description": "Verify the owned result directly.",
            },
            {
                "id": "B",
                "label": "Separate check",
                "description": "Use a separate bounded verifier.",
            },
        ],
        "recommendation": "A",
        "rationale": "The direct check has fewer prerequisites.",
    }


def request(env, key="decision"):
    rig, task, publisher, approvals = env
    context = rig.client.decision_context(task.issue_id)
    metadata = {
        **fields(),
        "recipient": context["recipient"],
        "label_id": context["label_id"],
        "label_name": context["label_name"],
        "decision_revision": task.decision_revision,
    }
    prose, ref = approvals.approve(metadata)
    return publisher.publish(rig.handle, task.id, prose, ref, key)


def test_canonical_case_variant_creator_and_app_assignee_are_exact(env):
    rig, task, publisher, _ = env
    for app in (False, True):
        rig.provider.creator_app = app
        context = rig.client.decision_context(task.issue_id)
        assert context["recipient"]["id"] == rig.scope.principal
        assert context["label_name"] == "Waiting For Decision"
    result = request(env)
    assert result["state"] == "confirmed" and result["label_state"] == "confirmed"
    body = rig.provider.comments[result["comment_id"]]["body"]
    assert body.endswith("https://linear.app/fixture/profiles/" + rig.scope.principal)
    assert "## Options considered" in body and "@" not in body


@pytest.mark.parametrize("change", ["missing", "ambiguous", "archived", "group", "foreign"])
def test_label_prerequisite_refuses_without_effect(env, change):
    rig, task, publisher, _ = env
    if change == "missing":
        rig.provider.label_candidates = []
    elif change == "ambiguous":
        rig.provider.label_candidates.append({**rig.provider.managed, "id": uid(601)})
    elif change == "archived":
        rig.provider.managed["archivedAt"] = "2026-01-01T00:00:00Z"
    elif change == "group":
        rig.provider.managed["isGroup"] = True
    else:
        rig.provider.managed["team"] = {"id": uid(999)}
    with pytest.raises(LinearError):
        request(env)
    assert not rig.provider.comments


def test_two_open_choices_keep_label_until_both_resolve_and_preserve_sentinel(env, monkeypatch):
    rig, task, publisher, _ = env
    sentinel = uid(700)
    rig.provider.issues[task.issue_id]["decision_labels"] = [sentinel]
    first = request(env, "one")
    second = request(env, "two")
    from ai_task_manager.adapters.decisions.authority import HumanInstruction

    choices = {}
    for row in (first, second):
        choices[row["id"]] = HumanInstruction(
            rig.scope,
            task.id,
            task.observed_status,
            task.decision_revision,
            "Authored choice",
            "authored",
            task.version,
            task.prose_digest,
            None,
            row["id"],
            "A",
            row["fingerprint"],
        )
    observations = SimpleNamespace(verify_instruction=lambda reference: choices[reference])
    # Authored observation port only. Genuine T05 source is covered by the installed case.
    observations.journal = SimpleNamespace()
    one = publisher.resolve_decision(
        rig.handle, first["id"], first["id"], "resolve-one", observations
    )
    assert one["decision_state"] == "resolved"
    assert set(rig.provider.issues[task.issue_id]["decision_labels"]) == {sentinel, uid(600)}
    two = publisher.resolve_decision(
        rig.handle, second["id"], second["id"], "resolve-two", observations
    )
    assert two["decision_state"] == "resolved" and rig.provider.issues[task.issue_id][
        "decision_labels"
    ] == [sentinel]
    assert len(rig.provider.comments) == 2


def test_comment_readback_failure_recovers_original_id_without_duplicate(env, monkeypatch):
    rig, task, publisher, _ = env
    original = rig.client.read_comment
    monkeypatch.setattr(
        rig.client,
        "read_comment",
        lambda _: (_ for _ in ()).throw(LinearError("transport_failure")),
    )
    first = request(env)
    assert first["state"] == "uncertain" and first["comment_id"]
    monkeypatch.setattr(rig.client, "read_comment", original)
    recovered = publisher.recover(rig.handle, first["id"])
    assert (
        recovered["comment_state"] == "confirmed"
        and recovered["label_state"] == "pending"
        and recovered["comment_id"] == first["comment_id"]
        and len(rig.provider.comments) == 1
    )
    assert not any("mutation DecisionLabelsUpdate" in r["query"] for r, _ in rig.provider.requests)
    # Continuing the remaining mutation is an explicit normal publication call.
    continued = publisher.publish_pending(rig.handle, first["id"])
    assert continued["state"] == "confirmed" and len(rig.provider.comments) == 1


def test_lost_label_reply_remains_uncertain_and_is_not_blindly_resent(env, monkeypatch):
    rig, task, publisher, _ = env
    original = rig.client.change_decision_label
    calls = []

    def lost(*args, **kwargs):
        calls.append(1)
        original(*args, **kwargs)
        raise LinearError("transport_failure", uncertain=True)

    monkeypatch.setattr(rig.client, "change_decision_label", lost)
    with pytest.raises(LinearError):
        request(env)
    with rig.journal.transaction() as conn:
        row = dict(
            conn.execute(
                "SELECT * FROM progress_publications WHERE decision_state='open'"
            ).fetchone()
        )
    assert row["label_state"] == "uncertain" and row["label_response"] is None
    recovered = publisher.recover(rig.handle, row["id"])
    assert recovered["state"] == "uncertain" and len(calls) == 1 and len(rig.provider.comments) == 1


def test_current_authority_and_recipient_change_block_publication(env):
    rig, task, publisher, approvals = env
    context = rig.client.decision_context(task.issue_id)
    prose, ref = approvals.approve(
        {
            **fields(),
            "recipient": context["recipient"],
            "label_id": context["label_id"],
            "label_name": context["label_name"],
            "decision_revision": 0,
        }
    )
    row = publisher.submit(rig.handle, task.id, prose, ref, "pending")
    publisher.check_authority = lambda: (_ for _ in ()).throw(ApprovalRequired("revoked"))
    with pytest.raises(ApprovalRequired):
        publisher.publish_pending(rig.handle, row["id"])
    assert not rig.provider.comments
    publisher.check_authority = lambda: None
    rig.provider.issues[task.issue_id]["assignee"] = {"id": uid(999)}
    with pytest.raises(ApprovalRequired):
        publisher.publish_pending(rig.handle, row["id"])
    rig.provider.issues[task.issue_id]["assignee"] = {"id": rig.scope.principal}
    rig.provider.agent_users.clear()
    with pytest.raises(LinearError):
        publisher.publish_pending(rig.handle, row["id"])
    assert not rig.provider.comments


def test_routine_mentions_and_wrong_decision_purpose_refuse(env):
    rig, task, publisher, approvals = env
    with pytest.raises(ApprovalRequired):
        publisher.submit(
            rig.handle,
            task.id,
            progress_prose("https://linear.app/fixture/profiles/" + rig.scope.principal),
            "not-approved",
            "routine-mention",
        )
    context = rig.client.decision_context(task.issue_id)
    prose, ref = approvals.approve(
        {**fields(), **context, "decision_revision": task.decision_revision}
    )
    approvals.records[ref]["purpose"] = "progress"
    with pytest.raises(ApprovalRequired):
        publisher.submit(rig.handle, task.id, prose, ref, "wrong-purpose")
    assert not rig.provider.comments


def test_additive_v1_migration_preserves_original_pending_row_and_history(env):
    rig, task, publisher, _ = env
    with rig.journal.transaction() as conn:
        conn.execute("DROP INDEX progress_resolution_key")
        for column in (
            "decision_state",
            "label_state",
            "label_response",
            "resolution_key",
            "resolution",
            "effect_owner",
            "effect_lease",
        ):
            conn.execute("ALTER TABLE progress_publications DROP COLUMN " + column)
        conn.execute("UPDATE progress_metadata SET version=1")
        conn.execute(
            "INSERT INTO "
            "progress_publications(id,scope,task_id,operation_key,fingerprint,payload,state,created_at)"
            " VALUES('old','scope','task','old-key','fp','{}','pending',1)"
        )
        conn.execute(
            "INSERT INTO "
            "progress_history(publication_id,state,recorded_at,detail) "
            "VALUES('old','pending',1,'original')"
        )
    ProgressStore(rig.journal)
    with rig.journal.transaction() as conn:
        row = dict(conn.execute("SELECT * FROM progress_publications WHERE id='old'").fetchone())
        assert (
            row["fingerprint"] == "fp"
            and row["payload"] == "{}"
            and row["state"] == "pending"
            and row["decision_state"] is None
        )
        assert (
            conn.execute(
                "SELECT detail FROM progress_history WHERE publication_id='old'"
            ).fetchone()[0]
            == "original"
        )


def test_resolution_keys_remain_independent_per_scope(env):
    rig, task, publisher, _ = env
    first = request(env, "same-submit")
    foreign = replace(rig.scope, principal=uid(999))
    second = publisher.store.submit(
        foreign, "separate-task", "foreign-submit", json.loads(first["payload"])
    )
    for scope, row in ((rig.scope, first), (foreign, second)):
        publisher.store.record_resolution(
            scope, row["id"], "same-resolution-key", {"source": "authored store isolation control"}
        )
        assert publisher.store.get(scope, row["id"])["resolution_key"] == "same-resolution-key"
    with pytest.raises(NotFound):
        publisher.store.get(foreign, first["id"])
    with pytest.raises(NotFound):
        publisher.store.get(rig.scope, second["id"])
    assert first["id"] != second["id"] and len(rig.provider.comments) == 1


def test_t03_saved_label_response_recovers_without_repeating_either_effect(env, monkeypatch):
    rig, task, publisher, _ = env
    sentinel = uid(701)
    rig.provider.issues[task.issue_id]["decision_labels"] = [sentinel]
    original = publisher.store.label_stage

    def crash(scope, publication, state, response=None):
        if state == "confirmed":
            raise RuntimeError("after saved label response")
        return original(scope, publication, state, response)

    monkeypatch.setattr(publisher.store, "label_stage", crash)
    with pytest.raises(RuntimeError):
        request(env, "retained-label")
    row = ProgressStore.retained(rig.journal, rig.scope, task.id)[0]
    assert row["label_state"] == "sent" and row["label_response"]
    before = len([r for r, _ in rig.provider.requests if "mutation" in r["query"]])
    publisher.store = ProgressStore.open_existing(rig.journal, clock=lambda: rig.now)
    recovered = publisher.recover(rig.handle, row["id"])
    assert recovered["state"] == "confirmed" and recovered["id"] == row["id"]
    assert recovered["operation_key"] == "retained-label"
    assert len(rig.provider.comments) == 1
    assert set(rig.provider.issues[task.issue_id]["decision_labels"]) == {sentinel, uid(600)}
    assert len([r for r, _ in rig.provider.requests if "mutation" in r["query"]]) == before


def test_t03_resolution_restart_reuses_original_choice_and_key(env, monkeypatch):
    from ai_task_manager.adapters.decisions import human
    from ai_task_manager.adapters.decisions.authority import HumanInstruction

    rig, task, publisher, _ = env
    row = request(env, "original-choice-request")
    instruction = HumanInstruction(
        rig.scope,
        task.id,
        task.observed_status,
        task.decision_revision,
        "Authored original choice",
        "authored-choice",
        task.version,
        task.prose_digest,
        None,
        row["id"],
        "A",
        row["fingerprint"],
    )
    observations = SimpleNamespace(verify_instruction=lambda _: instruction, journal=object())
    monkeypatch.setattr(
        publisher.store,
        "finish_resolution",
        lambda *a: (_ for _ in ()).throw(RuntimeError("after label settlement")),
    )
    with pytest.raises(RuntimeError):
        publisher.resolve_decision(
            rig.handle, row["id"], "original-human-ref", "original-choice-key", observations
        )
    retained = publisher.store.get(rig.scope, row["id"])
    assert retained["decision_state"] == "resolving" and retained["label_state"] == "confirmed"
    before = len([r for r, _ in rig.provider.requests if "mutation" in r["query"]])

    # This authored observation source returns only the exact original choice.
    # It supplies no genuine human receipt or provider-acceptance evidence.
    def original_choice(journal, reference):
        assert journal is observations.journal and reference == "original-human-ref"
        return instruction

    monkeypatch.setattr(human, "verify_receipt", original_choice)
    publisher.store = ProgressStore.open_existing(rig.journal, clock=lambda: rig.now)
    recovered = publisher.recover(rig.handle, row["id"], observations=observations)
    assert recovered["decision_state"] == "resolved"
    assert recovered["resolution_key"] == "original-choice-key"
    assert recovered["resolution"] == retained["resolution"]
    again = publisher.recover(rig.handle, row["id"], observations=observations)
    assert again["resolution"] == retained["resolution"]
    assert len([r for r, _ in rig.provider.requests if "mutation" in r["query"]]) == before


@pytest.mark.parametrize("original_owner_fact", [True, False])
def test_t03_completed_effect_claim_recovers_only_with_original_forward_history(
    env, monkeypatch, original_owner_fact
):
    rig, task, publisher, _ = env
    monkeypatch.setattr(
        publisher.store,
        "decision_release",
        lambda *a: (_ for _ in ()).throw(RuntimeError("after last effect before owner release")),
    )
    with pytest.raises(RuntimeError):
        request(env, "closed-effect")
    row = ProgressStore.retained(rig.journal, rig.scope, task.id)[0]
    assert row["state"] == row["label_state"] == "confirmed" and row["effect_owner"]
    if not original_owner_fact:
        # Model an older original journal without forward claim facts. Recovery
        # must preserve the busy owner instead of backfilling a new witness.
        with rig.journal.transaction() as conn:
            conn.execute("DELETE FROM progress_history WHERE state='effect-owner'")
    before = len([r for r, _ in rig.provider.requests if "mutation" in r["query"]])
    publisher.store = ProgressStore.open_existing(rig.journal, clock=lambda: rig.now)
    recovered = publisher.recover(rig.handle, row["id"])
    assert recovered["state"] == "confirmed"
    assert bool(recovered["effect_owner"]) is (not original_owner_fact)
    assert len([r for r, _ in rig.provider.requests if "mutation" in r["query"]]) == before
    if original_owner_fact:
        assert request(env, "next-owned-publication")["state"] == "confirmed"
        assert len(rig.provider.comments) == 2
    else:
        with pytest.raises(LinearError, match="decision_issue_effect_busy"):
            request(env, "next-owned-publication")
        assert len(rig.provider.comments) == 1
        with rig.journal.transaction() as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM progress_history WHERE state='effect-owner'"
                ).fetchone()[0]
                == 0
            )


def test_t03_human_change_fences_an_original_unstarted_label_stage(env, monkeypatch):
    rig, task, publisher, _ = env
    read = rig.client.read_comment
    monkeypatch.setattr(
        rig.client, "read_comment", lambda _: (_ for _ in ()).throw(LinearError("lost readback"))
    )
    first = request(env, "before-human-change")
    monkeypatch.setattr(rig.client, "read_comment", read)
    recovered = publisher.recover(rig.handle, first["id"])
    assert recovered["comment_state"] == "confirmed" and recovered["label_state"] == "pending"
    current = rig.current(task)
    proof = rig.proof(
        "human_decision", task.id, {"previous_revision": current.decision_revision, "hold": True}
    )
    rig.core.decisions.record(rig.handle, task.id, current.version, proof, rig.key())
    before = len([r for r, _ in rig.provider.requests if "mutation" in r["query"]])
    with pytest.raises(ApprovalRequired, match="version or human decision changed"):
        publisher.publish_pending(rig.handle, first["id"])
    assert len([r for r, _ in rig.provider.requests if "mutation" in r["query"]]) == before
    assert (
        rig.current(task).human_hold
        and publisher.store.get(rig.scope, first["id"])["label_state"] == "pending"
    )


@pytest.mark.parametrize("advance_version", [False, True])
def test_r01_released_resolution_survives_next_request_and_version_advance(
    env, monkeypatch, tmp_path, advance_version
):
    """Authored HTTP/human/observation ports; completed history must remain history."""
    import time

    from ai_task_manager.adapters.decisions.authority import HumanInstruction
    from ai_task_manager.adapters.decisions.observations import intent
    from ai_task_manager.application.service.facade import ApplicationService
    from ai_task_manager.installation.workflows import provider_recovery

    rig, task, publisher, approvals = env
    first = request(env, "resolved-a")
    choice = HumanInstruction(
        rig.scope,
        task.id,
        task.observed_status,
        task.decision_revision,
        "Authored original choice",
        "authored",
        task.version,
        task.prose_digest,
        None,
        first["id"],
        "A",
        first["fingerprint"],
    )
    observations = SimpleNamespace(verify_instruction=lambda _: choice, journal=object())
    from ai_task_manager.adapters.decisions import human

    def original_choice(journal, reference):
        assert journal is observations.journal and reference == "choice-a"
        return choice

    monkeypatch.setattr(human, "verify_receipt", original_choice)
    publisher.resolve_decision(rig.handle, first["id"], "choice-a", "resolve-a", observations)
    completed = publisher.store.get(rig.scope, first["id"])
    assert completed["decision_state"] == "resolved" and completed["effect_owner"] is None
    assert uid(600) not in json.loads(completed["label_response"])["labels"]
    original = {
        k: completed[k]
        for k in (
            "id",
            "fingerprint",
            "comment_id",
            "operation_key",
            "resolution_key",
            "resolution",
            "label_response",
        )
    }
    if advance_version:
        current = rig.current(task)
        proof = rig.proof(
            "human_decision",
            task.id,
            {
                "previous_revision": current.decision_revision,
                "hold": True,
            },
        )
        rig.core.decisions.record(rig.handle, task.id, current.version, proof, rig.key())
        assert rig.current(task).version > json.loads(completed["payload"])["task_version"]
    second = request((rig, rig.current(task), publisher, approvals), "open-b")
    assert (
        second["decision_state"] == "open"
        and uid(600) in rig.provider.issues[task.issue_id]["decision_labels"]
    )
    mutations = sum("mutation" in q["query"] for q, _ in rig.provider.requests)
    config_file = tmp_path / "authored-recovery.json"
    config = {"linear": {"authored": True}, "grant_ref": "authored-enrollment"}
    enrollment = {
        "scope": asdict(rig.scope),
        "config_path": str(config_file),
        "config_intent": intent(config),
        "provider": asdict(rig.client.binding.expected),
    }
    workflow = {"id": "authored-workflow", "task_id": task.id, "steps": {}}
    authority = object()
    recovery = SimpleNamespace(
        config=config,
        config_file=config_file,
        journal=authority,
        data={"enrollment": enrollment, "enrollment_revocation": None},
        spec=SimpleNamespace(data={"deadline": time.time() - 1, "scope": asdict(rig.scope)}),
        check_service=lambda _: workflow,
    )
    monkeypatch.setattr(provider_recovery, "installed_authority", lambda _: None)
    monkeypatch.setattr(
        provider_recovery,
        "history",
        lambda journal, ref: enrollment if ref == config["grant_ref"] else None,
    )
    writer = SimpleNamespace(approvals_for_decisions=lambda *args: approvals)
    service = ApplicationService(
        rig.core,
        rig.handle,
        provider=rig.outbound,
        inbound=rig.inbound,
        seal=SimpleNamespace(check=lambda: None),
        writer=writer,
        decisions=SimpleNamespace(observations=observations),
    )
    recovered = provider_recovery.reconcile_original(recovery, service, workflow)
    assert recovered["data"]["reason"] == "original_execution_unavailable", recovered
    assert recovered["data"]["provider_recovery"] == [
        {"publication_id": second["id"], "state": "confirmed"}
    ]
    replay = publisher.recover(rig.handle, first["id"], observations=observations)
    assert replay["decision_state"] == "resolved" and {k: replay[k] for k in original} == original
    resolved = publisher.resolve_decision(
        rig.handle, first["id"], "choice-a", "resolve-a", observations
    )
    assert {k: resolved[k] for k in original} == original
    with pytest.raises(ApprovalRequired, match="original key/source"):
        publisher.resolve_decision(
            rig.handle, first["id"], "choice-a", "different-key", observations
        )
    with pytest.raises(ApprovalRequired, match="original key/source"):
        publisher.resolve_decision(
            rig.handle, first["id"], "different-choice", "resolve-a", observations
        )
    # B can own the shared anchor while A remains released history. Replaying
    # A must neither reinterpret B's label nor release B's unfinished claim.
    owner = "decision-resolve:authored-b"
    anchor = publisher.store.decision_claim(
        rig.scope,
        second["id"],
        owner,
        intent={
            "kind": "resolution",
            "operation_key": "resolve-b",
            "resolution": {
                "human_ref": "choice-b",
                "option_id": "B",
                "request_digest": second["fingerprint"],
            },
        },
    )
    try:
        replay = publisher.recover(rig.handle, first["id"], observations=observations)
        assert (
            replay["decision_state"] == "resolved" and {k: replay[k] for k in original} == original
        )
        assert publisher.store.get(rig.scope, first["id"])["effect_owner"] == owner
    finally:
        publisher.store.decision_release(anchor, owner)
    assert publisher.store.get(rig.scope, second["id"])["decision_state"] == "open"
    assert uid(600) in rig.provider.issues[task.issue_id]["decision_labels"]
    assert sum("mutation" in q["query"] for q, _ in rig.provider.requests) == mutations
