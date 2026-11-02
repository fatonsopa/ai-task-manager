from dataclasses import replace

import pytest

from ai_task_manager.application.core import Core
from ai_task_manager.domains.scheduling.models import Dependency
from ai_task_manager.domains.scheduling.policy import check_cycles, evaluate, validate
from ai_task_manager.domains.work.errors import EvidenceRequired, IdempotencyConflict, StaleVersion, ValidationError

from .helpers import SnapshotSource, declare, snapshot
from .test_policy import sample


@pytest.mark.parametrize("variant", ["self", "two_node", "three_node", "cross_update", "unknown_external_closure"])
def test_cycles_and_self_edges(variant, rig):
    if variant == "self":
        with pytest.raises(ValidationError):
            validate(sample((Dependency("target", "work_done"),)), 1000)
    elif variant == "unknown_external_closure":
        edge = Dependency("external:opaque", "work_done")
        assert evaluate(sample((edge,)), {}, 1000).state == "blocked"
    elif variant == "cross_update":
        a, b = rig.task(), rig.task()
        declare(rig.core, rig.handle, a, rig.scheduling_sources, rig.now,
                (Dependency(b.id, "work_done"),))
        with pytest.raises(ValidationError):
            declare(rig.core, rig.handle, b, rig.scheduling_sources, rig.now,
                    (Dependency(a.id, "work_done"),))
        assert rig.core.scheduling.explain(rig.handle, b.id)["graph_version"] == 1
    else:
        graph = {"a": ("b",), "b": ("a",)} if variant == "two_node" else {
            "a": ("b",), "b": ("c",), "c": ("a",)
        }
        with pytest.raises(ValidationError):
            check_cycles(graph)
        check_cycles({"a": ("b", "c"), "b": ("d",), "c": ("d",)})


@pytest.mark.parametrize("variant", ["missing_fields", "unknown_fields", "bool_revision", "nonfinite_time", "duplicate_edges", "oversized_graph"])
def test_strict_snapshot_schema(variant):
    if variant in {"missing_fields", "unknown_fields"}:
        from ai_task_manager.domains.scheduling.models import DependencySnapshot
        with pytest.raises(TypeError):
            DependencySnapshot(**({} if variant == "missing_fields" else {"foreign_title": "private"}))
        return
    value = sample()
    if variant == "bool_revision":
        value = replace(value, source_revision=True)
    elif variant == "nonfinite_time":
        value = replace(value, observed_at=float("nan"))
    elif variant == "duplicate_edges":
        edge = Dependency("a", "work_done")
        value = replace(value, dependencies=(edge, edge))
    else:
        value = replace(value, dependencies=tuple(Dependency(str(i), "work_done") for i in range(257)))
    with pytest.raises(ValidationError):
        validate(value, 1000)


@pytest.mark.parametrize("variant", ["missing_verifier", "raw_bool", "forged_ref", "wrong_scope", "wrong_task", "wrong_prose"])
def test_trusted_snapshot_capability(variant, rig):
    task = rig.task()
    source = rig.scheduling_sources
    value = snapshot(rig.core, rig.handle, task, rig.now)
    if variant == "missing_verifier":
        core = Core(rig.db, rig.policy, evidence=rig.proofs, principals=rig.proofs)
        with pytest.raises(EvidenceRequired):
            core.scheduling.observe(rig.handle, task.id, task.version, 0, "unknown", rig.key())
        return
    if variant == "raw_bool":
        value = True
    elif variant == "wrong_scope":
        value = replace(value, scope=replace(rig.scope, principal="foreign"))
    elif variant == "wrong_task":
        value = replace(value, task_id="foreign")
    elif variant == "wrong_prose":
        value = replace(value, prose_digest="changed")
    ref = "forged" if variant == "forged_ref" else source.issue(value)
    before = rig.core.scheduling.explain(rig.handle, task.id)
    with pytest.raises((EvidenceRequired, ValidationError)):
        rig.core.scheduling.observe(rig.handle, task.id, task.version, 0, ref, rig.key())
    assert rig.core.scheduling.explain(rig.handle, task.id) == before


@pytest.mark.parametrize("variant", ["expired", "future_observation", "old_revision", "same_revision_conflict", "new_revision"])
def test_freshness_and_order(variant, rig):
    task = rig.task()
    value = snapshot(rig.core, rig.handle, task, rig.now)
    if variant == "expired":
        value = replace(value, observed_at=900, expires_at=930)
    elif variant == "future_observation":
        value = replace(value, observed_at=1001, expires_at=1031)
    elif variant == "old_revision":
        value = replace(value, source_revision=1)
    elif variant == "same_revision_conflict":
        value = replace(value, source_revision=value.source_revision - 1, priority="urgent")
    ref = rig.scheduling_sources.issue(value)
    from ai_task_manager.domains.scheduling.models import SchedulingBlocked
    if variant != "new_revision":
        with pytest.raises((SchedulingBlocked, ValidationError, StaleVersion)):
            rig.core.scheduling.observe(rig.handle, task.id, task.version, 0, ref, rig.key())
    else:
        result = rig.core.scheduling.observe(rig.handle, task.id, task.version, 0, ref, rig.key())
        assert result["source_revision"] == value.source_revision


@pytest.mark.parametrize("variant", ["exact", "changed_input", "other_session", "after_newer_state", "existing_fixture_admission_replay", "existing_fixture_queue_replay", "existing_fixture_direct_reserve", "existing_fixture_unqueued_rejection", "existing_fixture_stale_expected_version", "existing_fixture_queue_rejection"])
def test_operation_replay(variant, rig):
    if variant.startswith("existing_fixture_"):
        if variant == "existing_fixture_admission_replay":
            existing_fixture_replay(rig)
        else:
            existing_queue_fixture_control(rig, variant)
        return
    task = rig.task()
    value = snapshot(rig.core, rig.handle, task, rig.now)
    ref, key = rig.scheduling_sources.issue(value), rig.key()
    args = (rig.handle, task.id, task.version, 0, ref, key)
    first = rig.core.scheduling.observe(*args)
    if variant == "changed_input":
        with pytest.raises(IdempotencyConflict):
            rig.core.scheduling.observe(*args[:-2], "changed", key)
    elif variant == "other_session":
        other = rig.session(replace(rig.scope, logical_session="other"))
        with pytest.raises(IdempotencyConflict):
            rig.core.scheduling.observe(other, *args[1:])
    else:
        if variant == "after_newer_state":
            declare(rig.core, rig.handle, task, rig.scheduling_sources, rig.now, priority="urgent")
            rig.scheduling_sources.receipts.clear()
        assert rig.core.scheduling.observe(*args) == first


@pytest.mark.parametrize("variant", ["same_graph_refresh", "changed_graph", "reopen_store"])
def test_graph_revision_and_restart(variant, rig):
    task = rig.task()
    before = rig.core.scheduling.explain(rig.handle, task.id)
    edges = (Dependency("unknown-predecessor", "work_done"),) if variant == "changed_graph" else ()
    result = declare(rig.core, rig.handle, task, rig.scheduling_sources, rig.now, edges)
    assert result["graph_version"] == before["graph_version"] + int(variant == "changed_graph")
    if variant == "reopen_store":
        rig.core = rig.new_core()
        assert rig.core.scheduling.explain(rig.handle, task.id) == result
    with rig.db.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM scheduling_history WHERE task_id=?", (task.id,)).fetchone()[0] >= 3


def authored_existing_fixture(rig):
    """Actual enrolled Core admission/history with authored local observations."""
    import os
    from pathlib import Path
    from types import SimpleNamespace
    from uuid import uuid4

    from ai_task_manager.adapters.decisions.journal import AuthorityJournal
    from ai_task_manager.config.claim_store import enroll_claim_store, verify_claim_store
    from ai_task_manager.domains.work.existing import digest, readback_context
    from ai_task_manager.domains.work.models import TaskProse
    from tests.existing.helpers import ExistingRig, ScopedApprovals

    os.chmod(rig.db.path, 0o600)
    directory = Path(rig.db.path).parent
    authority = AuthorityJournal(directory / "claim.sqlite", directory / "claim.key")
    enroll_claim_store(rig.db, authority)
    claim = verify_claim_store(rig.db, authority)
    issue, key = str(uuid4()), "authored-existing-replay"
    created = "2026-11-01T00:00:00Z"
    data = {
        "issue_id": issue, "identifier": "TST-989", "title": "Verify admission replay",
        "body": "Retain original admission history.", "status": "Backlog",
        "created_at": created, "source_revision": created, "team_id": "team",
        "state_id": "backlog", "history": [{
            "id": "span", "started_at": created, "ended_at": None, "state_id": "backlog",
            "state": {"id": "backlog", "team_id": "team", "name": "Backlog",
                      "type": "backlog", "created_at": created, "updated_at": created},
        }],
    }
    payload = {"snapshot": data, "discovery_digest": digest(data), "claim_store": claim.context}
    discovery = rig.proof("existing_issue_snapshot", issue, payload)
    fresh = rig.proof("existing_issue_readback", issue, payload | {
        "context": readback_context("admission", key, discovery_ref=discovery)})
    selection = rig.proof("existing_issue_selection", issue, {
        "discovery_digest": digest(data), "claim_store": claim.context})
    prose = TaskProse(data["title"], data["body"])
    approvals = ScopedApprovals(rig.renderer, rig.policy)
    approved = approvals.add(rig.scope, prose)
    rig.core.context.approvals = approvals
    wrapper = ExistingRig.__new__(ExistingRig)
    wrapper.core, wrapper.handle = rig.core, rig.handle
    wrapper.db = rig.db
    wrapper.decision = lambda task, target="Todo": rig.decision(task, target)
    wrapper.scheduling_sources = rig.scheduling_sources
    wrapper.packet = lambda unused: (discovery, prose, approved, selection)
    wrapper.intake = SimpleNamespace(admit=lambda d, p, a, selected, operation:
        rig.core.existing.admit(rig.handle, d, fresh, p, a, selected, operation))
    def source(task, context):
        current = rig.core.get_task(rig.handle, task.id)
        observed = dict(data)
        if current.observed_status == "Todo":
            observed["status"], observed["state_id"] = "Todo", "todo"
            observed["source_revision"] = "2026-11-01T00:02:00Z"
            initial = dict(data["history"][0], ended_at="2026-11-01T00:01:00Z")
            observed["history"] = [initial, {
                "id": "todo-span", "started_at": "2026-11-01T00:01:00Z",
                "ended_at": None, "state_id": "todo",
                "state": {"id": "todo", "team_id": "team", "name": "Todo",
                          "type": "unstarted", "created_at": created, "updated_at": created},
            }]
        return rig.proof("existing_issue_readback", issue, {
            "snapshot": observed, "discovery_digest": digest(observed),
            "claim_store": claim.context, "context": context})
    def queue_readback(task_id, version, revision, operation):
        current = rig.core.get_task(rig.handle, task_id)
        rig.core.context.check(current, version, revision)
        return source(current, readback_context("queue", operation, task_id=task_id,
            expected_version=version, decision_revision=revision))
    wrapper.intake.queue_readback = queue_readback
    wrapper.intake.guard = lambda task, context: source(task, context)
    return wrapper, issue, key


def fixture_history(rig):
    with rig.db.transaction() as conn:
        return {name: [tuple(row) for row in conn.execute("SELECT * FROM " + name)]
                for name in ("tasks", "events", "operations", "attempts", "resource_leases",
                             "scheduling_snapshots", "scheduling_history", "scheduling_edges")}


def existing_fixture_replay(rig):
    wrapper, issue, key = authored_existing_fixture(rig)
    first = wrapper.admit(issue, key)
    with rig.db.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    human = rig.proof("human_decision", first.id, {"previous_revision": 0, "hold": True})
    changed = rig.core.decisions.record(rig.handle, first.id, first.version, human, rig.key())
    assert changed.version > first.version
    before = fixture_history(rig)
    assert wrapper.admit(issue, key) == first
    assert fixture_history(rig) == before
    assert wrapper.known_dependencies[first.id] == ((), "normal")
    assert not rig.scheduling_sources.receipts


def existing_queue_fixture_control(rig, variant):
    from ai_task_manager.domains.work.errors import InvalidTransition, StaleVersion

    wrapper, issue, admission_key = authored_existing_fixture(rig)
    first = wrapper.admit(issue, admission_key)
    if variant == "existing_fixture_unqueued_rejection":
        before = fixture_history(rig)
        with pytest.raises(InvalidTransition):
            wrapper.reserve(first, "unqueued-reserve")
        assert fixture_history(rig) == before
        assert not rig.scheduling_sources.receipts
        return
    if variant == "existing_fixture_queue_rejection":
        proof = rig.proof("human_decision", first.id, {"previous_revision": 0, "hold": True})
        rig.core.decisions.record(rig.handle, first.id, first.version, proof, rig.key())
        before = fixture_history(rig)
        with pytest.raises(StaleVersion):
            wrapper.queue(first, "stale-queue")
        assert fixture_history(rig) == before
        assert not rig.scheduling_sources.receipts
        return
    requested = wrapper.queue(first, "explicit-fixture-queue")
    with rig.db.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM scheduling_snapshots").fetchone()[0] == 1
    if variant == "existing_fixture_queue_replay":
        before = fixture_history(rig)
        receipts = dict(rig.scheduling_sources.receipts)
        assert wrapper.queue(first, "explicit-fixture-queue") == requested
        assert fixture_history(rig) == before
        assert rig.scheduling_sources.receipts == receipts
        return
    queued = rig.confirm(requested)
    if variant == "existing_fixture_stale_expected_version":
        before = fixture_history(rig)
        with pytest.raises(StaleVersion):
            wrapper.reserve(first, "stale-reserve")
        assert fixture_history(rig) == before
        return
    key = "direct-core-reserve"
    source = wrapper.startup_source(queued, key)
    attempt = rig.core.execution.reserve(rig.handle, queued.id, "host", queued.version,
        queued.decision_revision, rig.decision(queued, "In Progress"), key, readback_ref=source)
    assert attempt.state == "reserved" and attempt.task_id == queued.id
