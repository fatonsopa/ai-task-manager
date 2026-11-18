import time
from dataclasses import replace
from uuid import uuid4

import pytest

from ai_task_manager.adapters.linear.existing import ExistingReader
from ai_task_manager.domains.work.errors import EvidenceRequired, InvalidTransition, StaleVersion
from ai_task_manager.domains.work.existing import readback_context


def prepared(rig, provider, phase):
    issue = provider.seed("Todo")
    if phase == "admission":
        packet = rig.packet(issue)
        return issue, None, None, packet
    task = rig.admit(issue)
    if phase in {"reserve", "start"}:
        task = rig.queue(task)
    attempt = None
    if phase == "start":
        attempt = rig.reserve(task)
        task = rig.core.get_task(rig.handle, task.id)
    return issue, task, attempt, None


def context(phase, key, task, attempt=None, discovery_ref=None):
    if phase == "admission":
        return readback_context("admission", key, discovery_ref=discovery_ref)
    return readback_context(
        "queue" if phase == "queue" else "start",
        key,
        phase=phase if phase != "queue" else None,
        task_id=task.id,
        attempt_id=attempt.id if attempt else None,
        expected_version=task.version,
        decision_revision=task.decision_revision,
    )


def consume(
    rig, phase, key, ref, task=None, attempt=None, packet=None, decision_ref=None, startup_ref=None
):
    if phase == "admission":
        discovery, prose, approved, selection = packet
        return rig.core.existing.admit(rig.handle, discovery, ref, prose, approved, selection, key)
    if phase == "queue":
        return rig.core.transitions.transition(
            rig.handle,
            task.id,
            "Todo",
            "enqueue",
            task.version,
            task.decision_revision,
            decision_ref or rig.decision(task),
            {"existing_source": ref},
            key,
        )
    if phase == "reserve":
        return rig.core.execution.reserve(
            rig.handle,
            task.id,
            "fixture",
            task.version,
            task.decision_revision,
            decision_ref or rig.decision(task, "In Progress"),
            key,
            readback_ref=ref,
        )
    startup = startup_ref or rig.runtime_proof(
        "worker_start",
        attempt.id,
        {"task_id": task.id, "host": "fixture", "worker_id": "authored-fixture-worker"},
    )
    return rig.core.execution.start(
        rig.handle,
        attempt.id,
        task.version,
        task.decision_revision,
        startup,
        decision_ref or rig.decision(task, "In Progress"),
        key,
        readback_ref=ref,
    )


@pytest.mark.parametrize("phase", ["admission", "queue", "reserve", "start"])
def test_ea72_ordinary_discovery_is_never_a_fresh_effect_receipt(rig, provider, phase):
    issue, task, attempt, packet = prepared(rig, provider, phase)
    _, ordinary = rig.reader.inspect(issue)
    if phase == "admission":
        ordinary = packet[0]
    before = rig.effects()
    with pytest.raises(EvidenceRequired, match="existing_issue_readback"):
        consume(rig, phase, str(uuid4()), ordinary, task, attempt, packet)
    assert rig.effects() == before


def test_ea72_old_latest_fresh_row_cannot_authorize_new_reservation(rig, provider):
    issue, task, _, _ = prepared(rig, provider, "reserve")
    _, old = rig.reader.inspect(issue)
    with rig.db.transaction() as conn:
        conn.execute(
            "INSERT INTO existing_checks VALUES(?,?,?,?,?)",
            (task.id, task.version, task.decision_revision, old, time.time()),
        )
    before = rig.effects()
    with pytest.raises(EvidenceRequired):
        consume(rig, "reserve", str(uuid4()), None, task)
    assert rig.effects() == before


@pytest.mark.parametrize("phase", ["admission", "queue", "reserve", "start"])
def test_ea73_wrong_purpose_or_phase_is_rejected_inside_core(rig, provider, phase):
    issue, task, attempt, packet = prepared(rig, provider, phase)
    key = str(uuid4())
    if phase in {"queue", "reserve", "start"}:
        _, discovery = rig.reader.inspect(issue)
        wrong = readback_context("admission", key, discovery_ref=discovery)
    else:
        wrong = readback_context(
            "queue", key, task_id=str(uuid4()), expected_version=1, decision_revision=0
        )
    _, ref = rig.reader.readback(issue, wrong)
    before = rig.effects()
    with pytest.raises(EvidenceRequired, match="operation context"):
        consume(rig, phase, key, ref, task, attempt, packet)
    assert rig.effects() == before


@pytest.mark.parametrize(
    "field",
    ["scope", "store", "issue", "task", "attempt", "version", "revision", "operation", "phase"],
)
def test_ea74_exact_context_cannot_be_borrowed(rig, provider, field):
    issue, task, attempt, packet = prepared(rig, provider, "start")
    key = str(uuid4())
    ctx = context("start", key, task, attempt)
    reader = rig.reader
    if field == "scope":
        reader = ExistingReader(
            rig.client, rig.journal, replace(rig.scope, logical_session="other"), rig.claim.context
        )
    elif field == "store":
        reader = ExistingReader(
            rig.client,
            rig.journal,
            rig.scope,
            {"installation_id": str(uuid4()), "store_id": str(uuid4())},
        )
    elif field == "issue":
        issue = provider.seed("Todo")
    elif field == "task":
        ctx["task_id"] = str(uuid4())
    elif field == "attempt":
        ctx["attempt_id"] = str(uuid4())
    elif field == "version":
        ctx["expected_version"] += 1
    elif field == "revision":
        ctx["decision_revision"] += 1
    elif field == "operation":
        ctx["operation_key"] = "another-operation"
    else:
        ctx["phase"], ctx["attempt_id"] = "reserve", None
    _, ref = reader.readback(issue, ctx)
    before = rig.effects()
    with pytest.raises(EvidenceRequired):
        consume(rig, "start", key, ref, task, attempt)
    assert rig.effects() == before


@pytest.mark.parametrize("offset,accepted", [(29.999, True), (30, False), (31, False)])
def test_ea75_readback_expiry_is_shorter_than_discovery(rig, provider, offset, accepted):
    issue, task, _, _ = prepared(rig, provider, "queue")
    now = time.time()
    rig.journal.clock = lambda: now
    rig.core.context.clock = lambda: now + offset
    key = str(uuid4())
    _, discovery = rig.reader.inspect(issue)
    _, readback = rig.reader.readback(issue, context("queue", key, task))
    assert rig.journal.verify_evidence(discovery).expires_at == now + 900
    assert rig.journal.verify_evidence(readback).expires_at == now + 30
    before = rig.effects()
    if accepted:
        assert consume(rig, "queue", key, readback, task).queued_once
    else:
        with pytest.raises(EvidenceRequired):
            consume(rig, "queue", key, readback, task)
        assert rig.effects() == before


@pytest.mark.parametrize("fault", ["pagination", "graphql", "scope", "revision"])
def test_ea76_complete_reread_precedes_receipt_and_incomplete_read_issues_none(
    rig, provider, fault
):
    issue = provider.seed("Todo")
    provider.page_size = 1
    _, original = rig.reader.inspect(issue)
    provider.requests.clear()
    ctx = readback_context("admission", "readback-operation", discovery_ref=original)
    snapshot, ref = rig.reader.readback(issue, ctx)
    proof = rig.journal.verify_evidence(ref)
    assert proof.kind == "existing_issue_readback" and proof.payload["context"] == ctx
    queries = [request["query"] for request, _ in provider.requests]
    assert "ExistingMetadata" in queries[0] and "ExistingProse" in queries[1]
    assert sum("ExistingStateHistory" in query for query in queries) == 2
    assert "ExistingMetadata" in queries[-1]
    with rig.journal.transaction() as conn:
        before = conn.execute("SELECT COUNT(*) FROM proofs").fetchone()[0]
    if fault == "pagination":
        provider.history_override = lambda result: {
            "issue": {
                "id": issue,
                "stateHistory": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": None}},
            }
        }
    elif fault == "graphql":
        provider.fail_next = 200, b'{"errors":[{"message":"authored error"}],"data":{}}'
    else:
        from tests.linear.helpers import uid

        def changed(request, result):
            if fault == "scope" and "ExistingProse" in request["query"]:
                result["issue"]["assignee"] = {"id": uid(999)}
            if fault == "revision" and "ExistingStateHistory" in request["query"]:
                provider.issues[issue]["updatedAt"] = "2026-11-01T00:03:00Z"

        provider.hook = changed
    from ai_task_manager.adapters.linear.errors import LinearError
    from ai_task_manager.domains.work.errors import AccessDenied

    with pytest.raises((LinearError, AccessDenied)):
        rig.reader.readback(issue, ctx)
    with rig.journal.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM proofs").fetchone()[0] == before
    rig.assert_no_provider_mutations()


def test_ea77_operation_cannot_borrow_another_queue_receipt(rig, provider):
    issue, task, _, _ = prepared(rig, provider, "queue")
    _, ref = rig.reader.readback(issue, context("queue", "operation-a", task))
    before = rig.effects()
    with pytest.raises(EvidenceRequired):
        consume(rig, "queue", "operation-b", ref, task)
    assert rig.effects() == before


def test_ea78_committed_queue_replay_precedes_expired_readback_consumption(rig, provider):
    issue, task, _, _ = prepared(rig, provider, "queue")
    now = time.time()
    rig.journal.clock = lambda: now
    key = str(uuid4())
    _, ref = rig.reader.readback(issue, context("queue", key, task))
    # Keep the same exact authority reference, as the public invocation journal does.
    decision = rig.decision(task)
    args = (rig.handle, task.id, "Todo", "enqueue", task.version, task.decision_revision, decision)
    first = rig.core.transitions.transition(*args, {"existing_source": ref}, key)
    before = rig.effects()
    rig.core.context.clock = lambda: now + 1000
    replay = rig.core.transitions.transition(
        *args, {"existing_source": "unavailable-after-reply"}, key
    )
    assert replay == first and rig.effects() == before


@pytest.mark.parametrize("phase", ["admission", "queue", "reserve", "start"])
def test_ea78_each_committed_phase_replays_without_new_source_authority(rig, provider, phase):
    issue, task, attempt, packet = prepared(rig, provider, phase)
    now, key = time.time(), str(uuid4())
    rig.journal.clock = lambda: now
    ctx = context(phase, key, task, attempt, packet[0] if packet else None)
    _, source = rig.reader.readback(issue, ctx)
    decision = rig.decision(task, "Todo" if phase == "queue" else "In Progress") if task else None
    startup = (
        rig.runtime_proof(
            "worker_start",
            attempt.id,
            {"task_id": task.id, "host": "fixture", "worker_id": "authored-fixture-worker"},
        )
        if attempt
        else None
    )
    first = consume(rig, phase, key, source, task, attempt, packet, decision, startup)
    before = rig.effects()
    rig.core.context.clock = lambda: now + 1000
    rig.journal.clock = lambda: now + 1000
    if packet:
        del rig.approvals.rows[packet[2]]
    replay = consume(rig, phase, key, None, task, attempt, packet, decision, startup)
    assert replay == first and rig.effects() == before


def test_ea79_uncommitted_retry_can_get_real_new_readback_without_changing_authority(rig, provider):
    issue, task, _, _ = prepared(rig, provider, "queue")
    now = time.time()
    key, decision = str(uuid4()), rig.decision(task)
    rig.journal.clock = lambda: now
    _, stale = rig.reader.readback(issue, context("queue", key, task))
    rig.journal.clock = lambda: now + 31
    rig.core.context.clock = lambda: now + 31
    before = rig.effects()
    args = (rig.handle, task.id, "Todo", "enqueue", task.version, task.decision_revision, decision)
    with pytest.raises(EvidenceRequired):
        rig.core.transitions.transition(*args, {"existing_source": stale}, key)
    assert rig.effects() == before
    _, fresh = rig.reader.readback(issue, context("queue", key, task))
    assert fresh != stale
    assert rig.core.transitions.transition(*args, {"existing_source": fresh}, key).queued_once


def test_ea80_callback_does_not_replace_stale_expected_version(rig, provider):
    issue, task, _, _ = prepared(rig, provider, "reserve")
    callback = rig.intake.worker_readbacks(task.id, "worker-operation")
    before = rig.effects()
    requests = len(provider.requests)
    with pytest.raises(StaleVersion):
        callback(
            phase="reserve",
            task_id=task.id,
            expected_version=task.version - 1,
            decision_revision=task.decision_revision,
            attempt_id=None,
            operation_key="worker-operation:reserve",
        )
    assert rig.effects() == before and len(provider.requests) == requests


def test_ea81_canonical_delivery_fence_blocks_lease_and_atomic_short_publication(
    rig, provider, tmp_path
):
    from tests.linear.helpers import STATES

    from .helpers import span

    issue = provider.seed("Todo")
    task, attempt = rig.running(rig.admit(issue))
    # The actual authored provider reports history; local Task state is intentionally
    # unchanged so the shared canonical fence, rather than delivered_once, must act.
    end = "2026-11-01T12:00:00Z"
    provider.histories[issue][-1]["endedAt"] = end
    provider.histories[issue].append(span("Done", end, number=3))
    provider.issues[issue]["state"] = {"id": STATES["Done"], "name": "Done"}
    provider.issues[issue]["updatedAt"] = "2026-11-01T12:01:00Z"
    _, ref = rig.reader.inspect(issue)
    assert rig.core.existing.observed(rig.handle, ref).delivered
    assert not rig.core.get_task(rig.handle, task.id).delivered_once
    with pytest.raises(InvalidTransition):
        rig.core.execution.validate_work_lease(rig.handle, attempt.id, attempt.version)
    destination = tmp_path / "short-publication.txt"
    with pytest.raises(InvalidTransition):
        with rig.core.execution.work_boundary(rig.handle, attempt.id, attempt.version):
            destination.write_text("This effect must not execute.")
    assert not destination.exists() and len(rig.effects()["resource_leases"]) == 2


@pytest.mark.parametrize("validation", ["current", "wrong-account", "unavailable"])
def test_shared_revalidation_keeps_imported_start_readback_coherent(rig, provider, tmp_path, validation):
    """Actual client verification overlaps current imported startup source readback."""
    import json
    import traceback
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event
    from ai_task_manager.adapters.linear.errors import LinearError
    from tests.linear.helpers import uid

    issue = provider.seed("Todo")
    task = rig.queue(rig.admit(issue))
    entered, release, reading = Event(), Event(), Event()
    send = rig.client.transport.send
    original = rig.client.verify_binding
    callback = rig.intake.worker_readbacks(task.id, "shared-binding-start")

    def controlled_send(query, variables, credentials):
        if "query VerifyBinding" in query:
            entered.set()
            assert release.wait(5), "Owned verification barrier expired"
            if validation == "wrong-account":
                provider.viewer_id = uid(999)
            elif validation == "unavailable":
                provider.fail_next = (503, b"authored verification failure")
        return send(query, variables, credentials)

    def read():
        reading.set()
        try:
            reference = callback(phase="reserve", task_id=task.id, expected_version=task.version,
                                 decision_revision=task.decision_revision, attempt_id=None,
                                 operation_key="shared-binding-start:reserve")
            return {"kind": rig.journal.verify_evidence(reference).kind}
        except Exception as error:
            return {"error": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}

    rig.client.transport.send = controlled_send
    with ThreadPoolExecutor(max_workers=2) as pool:
        verification = pool.submit(original)
        try:
            assert entered.wait(5)
            pending = pool.submit(read)
            assert reading.wait(5)
            try:
                early = pending.result(timeout=0.2)
            except TimeoutError:
                early = {"waiting": True}
        finally:
            release.set()
        verification_error = None
        try:
            binding = verification.result(timeout=5)
        except LinearError as error:
            binding = None
            verification_error = type(error).__name__
        observed = pending.result(timeout=5)
    if validation != "current":
        with pytest.raises(LinearError):
            rig.client.update_issue(task, "set_status")
    facts = {"scope": "Controlled localhost interleave; exact G13 live thread/stack remains unknown",
             "validation": validation, "verification_error": verification_error,
             "early": early, "reader": observed, "binding_verified": binding is not None,
             "effect_counts": {key: len(rows) for key, rows in rig.effects().items()},
             "mutations": sum("mutation " in q["query"] for q, _ in provider.requests)}
    (tmp_path / "binding-race-facts.json").write_text(json.dumps(facts, indent=2) + "\n")
    if validation == "current":
        assert observed == {"kind": "existing_issue_readback"} and verification_error is None, facts
    else:
        assert observed.get("error") == "LinearError" and verification_error is not None, facts
        assert rig.client.binding is None, facts
    assert facts["effect_counts"]["attempts"] == facts["effect_counts"]["resource_leases"] == 0, facts
    assert facts["mutations"] == 0
