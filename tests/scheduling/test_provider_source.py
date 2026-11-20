"""Real Core/client scheduling boundaries with authored, never live, Linear data."""

import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.linear.dependencies.receipts import PREFIX
from ai_task_manager.adapters.linear.models import WireResponse
from ai_task_manager.application.service.facade import ApplicationService
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired, StaleVersion
from ai_task_manager.domains.work.models import IssueStatus as S
from ai_task_manager.storage import work

from .provider_fixture import ProviderFixture


@pytest.fixture
def provider(tmp_path):
    return ProviderFixture(tmp_path / "owned")


def test_complete_empty_prepare_order_local_resolver_and_historical_retry(provider):
    p, r = provider, provider.rig
    task = p.task()
    prepared = p.source.prepare(task.id, "stable")
    assert r.core.scheduling.explain(r.handle, task.id)["state"] == "unknown"
    count = len(p.calls)
    p.disabled = True
    snapshot = p.receipts.verify_snapshot(prepared.snapshot_ref)
    assert snapshot.completeness == "complete" and snapshot.dependencies == ()
    result = p.source.refresh(task.id, "stable")
    assert result["state"] == "ready" and result["graph_version"] == 1
    events = r.core.notifications.list_events(r.handle)
    r.now += 31
    assert p.source.refresh(task.id, "stable") == result
    assert len(p.calls) == count and r.core.notifications.list_events(r.handle) == events


def test_one_dependency_503_keeps_complete_verified_collection(provider, monkeypatch, tmp_path):
    from ai_task_manager.adapters.linear.dependencies.schema import DETAILS, METADATA

    p = provider
    task = p.task()
    original, dispatched, credential_reads = p.send, [], []
    credentials = p.client.credentials

    def current_credentials():
        credential_reads.append(True)
        return credentials()

    def send(query, variables, credentials):
        dispatched.append((query, len(credential_reads)))
        if query == METADATA and sum(q == METADATA for q, _ in dispatched) == 1:
            return WireResponse(503, {}, b"unavailable")
        return original(query, variables, credentials)

    monkeypatch.setattr(p.client, "credentials", current_credentials)
    monkeypatch.setattr(p, "send", send)
    result = p.refresh(task, "single-503")
    prepared = p.source.prepare(task.id, "single-503")
    snapshot = p.receipts.verify_snapshot(prepared.snapshot_ref)
    facts = {
        "state": result["state"],
        "completeness": snapshot.completeness,
        "metadata_dispatches": sum(q == METADATA for q, _ in dispatched),
        "prose_dispatches": sum(q == DETAILS for q, _ in dispatched),
        "mutations": sum(q.lstrip().startswith("mutation") for q, _ in dispatched),
        "credential_rechecked": [n for q, n in dispatched if q == METADATA][:2][1]
        == [n for q, n in dispatched if q == METADATA][:2][0] + 1,
    }
    (tmp_path / "dependency-503-facts.json").write_text(json.dumps(facts, indent=2) + "\n")
    assert facts == {
        "state": "ready",
        "completeness": "complete",
        "metadata_dispatches": 5,
        "prose_dispatches": 2,
        "mutations": 0,
        "credential_rechecked": True,
    }, facts
    assert snapshot.dependencies == ()


def test_one_dependency_transport_failure_keeps_complete_diagnostics(provider, monkeypatch, tmp_path):
    import hashlib
    from pathlib import Path

    from ai_task_manager.adapters.linear.dependencies import reader
    from ai_task_manager.adapters.linear.dependencies.schema import METADATA
    from ai_task_manager.adapters.linear.errors import LinearError

    p, task = provider, provider.task()
    assert Path(reader.__file__).resolve() == Path(__file__).resolve().parents[2] / (
        "src/ai_task_manager/adapters/linear/dependencies/reader.py"
    )
    original, dispatched, credential_reads = p.send, [], []
    credentials = p.client.credentials

    def current_credentials():
        credential_reads.append(True)
        return credentials()

    def send(query, variables, credential):
        dispatched.append((query, dict(variables), len(credential_reads)))
        if query == METADATA and sum(q == METADATA for q, _, _ in dispatched) == 1:
            raise LinearError("transport_failure", uncertain=True) from TimeoutError(
                "private-error-message-and-credential-sentinel"
            )
        return original(query, variables, credential)

    monkeypatch.setattr(p.client, "credentials", current_credentials)
    monkeypatch.setattr(p, "send", send)
    result = p.refresh(task, "single-transport-failure")
    assert result["state"] == "ready"
    ref = p.receipts.lookup(p.scope, task.id, "single-transport-failure")["receipt"]
    receipt = p.receipts._get(ref, PREFIX)
    assert receipt["snapshot"]["completeness"] == "complete"
    failures = receipt["raw"]["observation"]["read_failures"]
    assert len(failures) == 1
    failure = failures[0]
    metadata = [(v, n) for q, v, n in dispatched if q == METADATA]
    assert len(metadata) == 5 and metadata[1][1] == metadata[0][1] + 1
    assert failure["query_sha256"] == hashlib.sha256(METADATA.encode()).hexdigest()
    assert failure["variables_sha256"] == hashlib.sha256(
        json.dumps(metadata[0][0], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert failure["category"] == "transport_failure"
    assert failure["transport_entered"] and not failure["response_returned"]
    assert failure["response_status"] is None and failure["recovery_selected"]
    assert failure["provider_receipt"] == "unknown"
    assert [x["type"] for x in failure["exception_chain"]] == [
        "LinearError", "LinearError", "TimeoutError",
    ]
    assert 0 <= failure["elapsed_seconds"] <= 20
    assert "private-error-message-and-credential-sentinel" not in json.dumps(receipt)
    assert not any(q.lstrip().startswith("mutation") for q, _, _ in dispatched)
    (tmp_path / "dependency-transport-facts.json").write_text(json.dumps({
        "state": result["state"], "failure": failure,
        "metadata_dispatches": len(metadata), "credential_rechecked": True,
        "controlled_only": True,
    }, indent=2) + "\n")


@pytest.mark.parametrize("mode", [
    "second_transport", "transport_then_503", "503_then_transport", "deadline", "credentials",
])
def test_dependency_transport_recovery_remains_bounded(provider, monkeypatch, mode):
    from ai_task_manager.adapters.linear.dependencies import reader
    from ai_task_manager.adapters.linear.dependencies.schema import DETAILS, METADATA
    from ai_task_manager.adapters.linear.errors import LinearError
    from ai_task_manager.adapters.linear.models import Credentials

    p, task = provider, provider.task()
    original, dispatched, clock, token = p.send, [], [0.0], ["original-authored-token"]
    monkeypatch.setattr(reader.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(p.client, "credentials", lambda: Credentials(token[0]))

    def send(query, variables, credential):
        dispatched.append(query)
        if query == METADATA and dispatched.count(METADATA) == 1:
            if mode == "503_then_transport":
                return WireResponse(503, {}, b"")
            if mode == "deadline":
                clock[0] = 20
            if mode == "credentials":
                token[0] = "changed-authored-token"
            raise LinearError("transport_failure", uncertain=True) from TimeoutError("private-sentinel")
        if query == DETAILS:
            if mode == "transport_then_503":
                return WireResponse(503, {}, b"")
            raise LinearError("transport_failure", uncertain=True) from OSError(54, "private-sentinel")
        return original(query, variables, credential)

    monkeypatch.setattr(p, "send", send)
    result = p.refresh(task, "bounded-transport")
    ref = p.receipts.lookup(p.scope, task.id, "bounded-transport")["receipt"]
    receipt = p.receipts._get(ref, PREFIX)
    assert result["state"] == "unknown" and receipt["snapshot"]["completeness"] == "unavailable"
    assert dispatched.count(METADATA) == (1 if mode in {"deadline", "credentials"} else 2)
    assert dispatched.count(DETAILS) == (0 if mode in {"deadline", "credentials"} else 1)
    failures = receipt["raw"]["observation"]["read_failures"]
    assert len(failures) == 2 and sum(x["recovery_selected"] for x in failures) == 1
    assert failures[-1]["transport_entered"] is (mode not in {"deadline", "credentials"})
    assert "private-sentinel" not in json.dumps(receipt)


@pytest.mark.parametrize("mode", ["mutation", "unknown_query", "request_budget", "retry_after"])
def test_dependency_transport_never_retries_unqualified_read(provider, mode):
    from ai_task_manager.adapters.linear.dependencies.reader import _BudgetTransport, _ReadClient
    from ai_task_manager.adapters.linear.dependencies.schema import METADATA
    from ai_task_manager.adapters.linear.errors import LinearError

    dispatched = []

    class Unavailable:
        def send(self, query, variables, credentials):
            dispatched.append(query)
            raise LinearError("transport_failure", uncertain=True,
                              retry_after=0 if mode == "retry_after" else None)

    budget = _BudgetTransport(Unavailable())
    if mode == "request_budget":
        budget.requests = 2047
    client = _ReadClient(budget, provider.client.credentials, provider.expected)
    query = "query UnknownRead { unavailable }" if mode == "unknown_query" else METADATA
    with pytest.raises(LinearError) as failed:
        client.request(query, {}, mutation=mode == "mutation")
    assert failed.value.category == (
        "dependency_observation_limit" if mode == "request_budget" else "transport_failure"
    )
    assert len(dispatched) == 1 and budget.requests == (2048 if mode == "request_budget" else 1)


@pytest.mark.parametrize("mode", ["second_503", "retry_after", "deadline", "credentials"])
def test_dependency_503_recovery_remains_bounded(provider, monkeypatch, mode):
    from ai_task_manager.adapters.linear.dependencies import reader
    from ai_task_manager.adapters.linear.dependencies.schema import DETAILS, METADATA
    from ai_task_manager.adapters.linear.models import Credentials

    p = provider
    task = p.task()
    original, dispatched, clock = p.send, [], [0.0]
    token = ["original-authored-token"]
    monkeypatch.setattr(reader.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(p.client, "credentials", lambda: Credentials(token[0]))

    def send(query, variables, credentials):
        dispatched.append(query)
        if query == METADATA and dispatched.count(METADATA) == 1:
            if mode == "deadline":
                clock[0] = 20
            elif mode == "credentials":
                token[0] = "different-authored-token"
            return WireResponse(503, {"retry-after": "0"} if mode == "retry_after" else {}, b"")
        if mode == "second_503" and query == DETAILS:
            return WireResponse(503, {}, b"")
        return original(query, variables, credentials)

    monkeypatch.setattr(p, "send", send)
    result = p.refresh(task, "bounded-503")
    prepared = p.source.prepare(task.id, "bounded-503")
    snapshot = p.receipts.verify_snapshot(prepared.snapshot_ref)
    assert result["state"] == "unknown" and snapshot.completeness == "unavailable"
    assert dispatched.count(METADATA) == (2 if mode == "second_503" else 1)
    assert dispatched.count(DETAILS) == (1 if mode == "second_503" else 0)


@pytest.mark.parametrize("mode", ["mutation", "unknown_query", "request_budget"])
def test_dependency_503_never_retries_an_unqualified_read(provider, mode):
    from ai_task_manager.adapters.linear.dependencies.reader import _BudgetTransport, _ReadClient
    from ai_task_manager.adapters.linear.dependencies.schema import METADATA
    from ai_task_manager.adapters.linear.errors import LinearError

    dispatched = []

    class Unavailable:
        def send(self, query, variables, credentials):
            dispatched.append(query)
            return WireResponse(503, {}, b"")

    budget = _BudgetTransport(Unavailable())
    if mode == "request_budget":
        budget.requests = 2047
    client = _ReadClient(budget, provider.client.credentials, provider.expected)
    query = "query UnknownRead { unavailable }" if mode == "unknown_query" else METADATA
    with pytest.raises(LinearError) as failed:
        client.request(query, {}, mutation=mode == "mutation")
    assert failed.value.category == (
        "dependency_observation_limit" if mode == "request_budget" else "http_failure"
    )
    assert len(dispatched) == 1
    assert budget.requests == (2048 if mode == "request_budget" else 1)


@pytest.mark.parametrize("kind", ["blocks", "related", "similar", "archived"])
def test_outgoing_and_known_nonblocking_relations_do_not_invent_predecessors(provider, kind):
    a, b = provider.task(), provider.task()
    edge = provider.edge(a, b, kind if kind != "archived" else "blocks")
    if kind == "archived":
        edge["archivedAt"] = edge["updatedAt"]
        result = provider.refresh(b)
    else:
        result = provider.refresh(a if kind == "blocks" else b)
    assert result["state"] == "ready"


def test_both_directions_complete_pagination_and_actual_predecessor_block(provider):
    a, b, root, successor = [provider.task() for _ in range(4)]
    provider.edge(a, root)
    provider.edge(b, root)
    provider.edge(root, successor)
    provider.page_size = 1
    result = provider.refresh(root)
    assert result["state"] == "blocked"
    assert {row["dependency"] for row in result["blockers"]} == {a.id, b.id}
    assert any(v.get("after") == "1" for q, v in provider.calls if "inverseRelations(" in q)
    assert any("relations(" in q for q, _ in provider.calls)


@pytest.mark.parametrize("variant", ["principal", "project", "team", "archived", "missing"])
def test_root_metadata_scope_refuses_before_prose_or_relationships(provider, variant):
    task = provider.task()
    node = provider.nodes[task.issue_id]
    if variant == "principal":
        node["assignee"]["id"] = str(uuid4())
    elif variant == "project":
        node["project"]["id"] = str(uuid4())
    elif variant == "team":
        node["team"]["id"] = str(uuid4())
    elif variant == "archived":
        node["archivedAt"] = node["updatedAt"]
    else:
        node.pop("archivedAt")
    assert provider.refresh(task)["state"] == "unknown"
    assert not any("DependencyProse" in q or "Relations(" in q for q, _ in provider.calls)


def test_cross_session_predecessor_is_unresolved_without_foreign_prose(provider):
    r = provider.rig
    foreign = r.session(replace(provider.scope, logical_session="other"))
    predecessor, task = provider.task(handle=foreign), provider.task()
    provider.edge(predecessor, task)
    assert provider.refresh(task)["state"] == "unknown"
    assert not any(
        "DependencyProse" in q and v.get("id") == predecessor.issue_id for q, v in provider.calls
    )


@pytest.mark.parametrize(
    "variant", ["prose", "status", "unknown_relation", "duplicate", "cycle", "bad_schema"]
)
def test_ambiguous_inputs_and_cycle_never_become_empty_ready(provider, variant):
    a, task = provider.task(), provider.task()
    provider.edge(a, task)
    if variant == "prose":
        provider.nodes[a.issue_id]["description"] = "changed"
    elif variant == "status":
        provider.nodes[a.issue_id]["state"] = {"id": provider.states["Done"], "name": "Done"}
    elif variant == "unknown_relation":
        provider.edges[0]["type"] = "new-kind"
    elif variant == "duplicate":
        provider.edges[0]["type"] = "duplicate"
    elif variant == "cycle":
        provider.edge(task, a)
    else:
        provider.schema["relationTypes"]["enumValues"].append({"name": "future"})
    assert provider.refresh(task)["state"] == "unknown"


@pytest.mark.parametrize(
    "variant", ["page", "repeat_cursor", "duplicate_id", "revision", "second_scan", "direction"]
)
def test_partial_pagination_and_changing_graph_block(provider, variant):
    a, b, task = provider.task(), provider.task(), provider.task()
    provider.edge(a, task)
    provider.edge(b, task)
    provider.page_size = 1
    original = provider.send
    count = 0

    def altered(query, variables, credentials):
        nonlocal count
        if (
            variant == "second_scan"
            and count == 2
            and "inverseRelations(" in query
            and variables["id"] == task.issue_id
        ):
            for edge in provider.edges:
                edge["updatedAt"] = "2026-11-02T00:00:01Z"
        response = original(query, variables, credentials)
        data = json.loads(response.body)
        if "inverseRelations(" in query and variables["id"] == task.issue_id:
            count += 1
            raw = data["data"]["issue"]
            connection = raw["inverseRelations"]
            if variant == "page":
                connection.pop("pageInfo")
            elif variant == "repeat_cursor":
                connection["pageInfo"] = {"hasNextPage": True, "endCursor": "1"}
            elif variant == "duplicate_id" and count == 2:
                connection["nodes"][0]["id"] = provider.edges[0]["id"]
            elif variant == "revision" and count == 2:
                raw["updatedAt"] = "2026-11-02T00:00:01Z"
            elif variant == "direction":
                connection["nodes"][0]["relatedIssue"]["id"] = a.issue_id
        return WireResponse(200, {}, json.dumps(data).encode())

    provider.send = altered
    assert provider.refresh(task)["state"] == "unknown"


@pytest.mark.parametrize("kind", ["Canceled", "Duplicate", "held", "failed", "Done"])
def test_provider_state_does_not_create_completion_proof(provider, kind):
    a, task = provider.task(), provider.task()
    current = a
    if kind in {"Canceled", "Duplicate", "Done"}:
        current = replace(a, observed_status=S(kind), desired_status=S(kind))
    elif kind == "held":
        current = replace(a, human_hold=True)
    else:
        current = replace(a, execution_state="failed")
    # Explicit authored stored boundary fixture; no fabricated provider completion proof.
    with provider.rig.db.transaction() as conn:
        current = work.save(conn, current, a.version)
    provider.sync(current)
    provider.edge(current, task)
    result = provider.refresh(task)
    assert result["state"] == "blocked"
    assert result["blockers"][0]["code"] != "ready"


def test_real_core_noncode_completion_refs_allow_dependency(provider):
    r = provider.rig
    a = provider.task()
    provider.refresh(a)
    a = r.transition(a, S.TODO, "enqueue")
    provider.sync(a)
    from ai_task_manager.domains.scheduling.resources import AdmissionResources

    r.resource_sources.rows[a.id] = AdmissionResources(
        provider.scope,
        a.id,
        a.prose_digest,
        a.decision_revision,
        "authored-project",
        (),
        (),
    )
    a, attempt = r.running(a)
    a = r.complete(a)
    a, _ = r.settle(a, attempt)
    a = r.transition(a, S.DONE, "non_code_complete", r.delivery(a))
    provider.sync(a)
    task = provider.task()
    provider.edge(a, task)
    assert provider.refresh(task)["state"] == "ready"


@pytest.mark.parametrize("kind", ["version", "decision", "session", "generation"])
def test_concurrent_local_or_collection_changes_refuse(provider, kind):
    task = provider.task()
    changed = False

    def callback(query, variables):
        nonlocal changed
        if changed or "DependencyMetadata" not in query:
            return
        changed = True
        if kind == "session":
            provider.source.binding = ("changed", provider.scope)
        elif kind == "generation":
            provider.receipts.begin(provider.scope, task, 0, "competing")
        else:
            with provider.rig.db.transaction() as conn:
                value = replace(task, decision_revision=1) if kind == "decision" else task
                work.save(conn, value, task.version)

    provider.callback = callback
    with pytest.raises((AccessDenied, StaleVersion)):
        provider.refresh(task)
    assert provider.rig.core.scheduling.explain(provider.rig.handle, task.id)["state"] == "unknown"


@pytest.mark.parametrize("kind", ["forged", "tampered", "expired", "wrong_purpose", "superseded"])
def test_immutable_receipts_refuse_invalid_or_stale_inputs(provider, kind):
    task = provider.task()
    prepared = provider.source.prepare(task.id, "receipt")
    ref = prepared.snapshot_ref
    if kind == "forged":
        ref = PREFIX + str(uuid4())
    elif kind == "tampered":
        with provider.receipts.transaction() as conn:
            conn.execute("UPDATE records SET data='{}' WHERE ref=?", (ref,))
    elif kind == "expired":
        provider.rig.now += 31
    elif kind == "wrong_purpose":
        raw = provider.receipts._get(ref, PREFIX)
        raw["purpose"] = "other"
        ref = provider.receipts._put(PREFIX, raw)
    else:
        provider.receipts.begin(provider.scope, task, 0, "newer")
    with pytest.raises(AccessDenied):
        provider.receipts.verify_snapshot(ref)
    assert provider.rig.core.scheduling.explain(provider.rig.handle, task.id)["state"] == "unknown"


@pytest.mark.parametrize(
    "priority,expected",
    [
        (0, "none"),
        (1, "urgent"),
        (2, "high"),
        (3, "normal"),
        (4, "low"),
        (None, None),
        (True, None),
        (5, None),
    ],
)
def test_exact_priority_mapping(provider, priority, expected):
    task = provider.task()
    provider.nodes[task.issue_id]["priority"] = priority
    result = provider.refresh(task)
    assert result["state"] == ("ready" if expected else "unknown")
    if expected:
        assert result["priority"] == expected


def service_fixture(p, root, *, fail_first=False):
    from ai_task_manager.supervisor.authority import WorkAuthorities

    r = p.rig
    journal = AuthorityJournal(root / "service.sqlite", root / "service.key")
    calls = []
    authorities = WorkAuthorities(r.core, p.source)
    r.core.scheduling.renewals = authorities

    def start(handle, task_id, host, **values):
        authorities.refresh_for_start(
            handle,
            task_id,
            values["operation_key"],
            expected_version=values["expected_version"],
            decision_revision=values["decision_revision"],
        )
        calls.append(r.core.scheduling.explain(handle, task_id))
        assert calls[-1]["state"] == "ready"
        if fail_first and len(calls) == 1:
            raise EvidenceRequired("Authored pre-reserve refusal")
        return r.core.execution.reserve(
            handle,
            task_id,
            host,
            values["expected_version"],
            values["decision_revision"],
            values["decision_ref"],
            values["operation_key"] + ":reserve",
        )

    decisions = SimpleNamespace(
        journal=journal, decision=lambda scope, task, target, action, **kw: r.decision(task, target)
    )

    def bind_dependencies(source):
        # Exact new trusted composition port; no authored readiness receipt.
        assert source is p.source
        assert source.binding == r.core.session_binding(r.handle)

    service = ApplicationService(
        r.core,
        r.handle,
        decisions=decisions,
        workers=SimpleNamespace(start=start, bind_dependencies=bind_dependencies),
        dependencies=p.source,
        grant_ref="authored",
    )
    return service, calls


@pytest.mark.parametrize("kind", ["valid_cached", "refused_retry", "stale", "foreign", "blocked"])
def test_normal_worker_start_refresh_order_and_retry(provider, tmp_path, kind):
    p, r = provider, provider.rig
    task = p.task()
    task = r.transition(task, S.TODO, "enqueue")
    p.sync(task)
    if kind == "blocked":
        p.edge(p.task(), task)
    service, calls = service_fixture(p, tmp_path / "owned", fail_first=kind == "refused_retry")
    data = {
        "task_id": task.id,
        "host": "host",
        "expected_version": task.version,
        "decision_revision": task.decision_revision,
        "operation_key": "start",
    }
    if kind == "stale":
        data["expected_version"] -= 1
    if kind == "foreign":
        other = r.session(replace(p.scope, logical_session="other"))
        data["task_id"] = p.task(handle=other).id
    before = len(p.calls)
    first = service.call("worker_start", data)
    if kind in {"stale", "foreign", "blocked"}:
        assert not first["ok"] and calls == []
        with r.db.transaction() as conn:
            assert conn.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
        if kind in {"stale", "foreign"}:
            assert len(p.calls) == before
        return
    if kind == "refused_retry":
        assert not first["ok"] and len(calls) == 1
        before = len(p.calls)
        r.now += 31
        second = service.call("worker_start", data)
        assert second["ok"] and len(p.calls) > before and len(calls) == 2
    else:
        assert first["ok"] and len(calls) == 1
        before = len(p.calls)
        p.disabled = True
        r.now += 31
        second = service.call("worker_start", data)
        assert second["ok"] and second["data"]["id"] == first["data"]["id"]
        assert len(p.calls) == before and len(calls) == 1


def test_final_workflow_map_change_is_not_a_stable_observation(provider):
    task = provider.task()
    preflights = 0

    def changed(query, variables):
        nonlocal preflights
        if "VerifyBinding" in query:
            preflights += 1
            if preflights == 2:
                provider.states["Done"] = str(uuid4())

    provider.callback = changed
    assert provider.refresh(task)["state"] == "unknown"


def test_missing_opposite_direction_is_not_complete(provider):
    from ai_task_manager.adapters.linear.dependencies.schema import DETAILS, RELATIONS

    a, task = provider.task(), provider.task()
    provider.edge(a, task)
    original = provider.send
    injected = []

    def missing(query, variables, credentials):
        response = original(query, variables, credentials)
        data = json.loads(response.body)
        if query in {RELATIONS["relations"], DETAILS} and variables["id"] == a.issue_id:
            assert data["data"]["issue"]["relations"]["nodes"]
            injected.append(variables["id"])
            data["data"]["issue"]["relations"]["nodes"] = []
        return WireResponse(200, {}, json.dumps(data).encode())

    provider.send = missing
    assert provider.refresh(task, "missing-opposite")["state"] == "unknown"
    assert injected and set(injected) == {a.issue_id}
    ref = provider.receipts.lookup(provider.scope, task.id, "missing-opposite")["receipt"]
    assert provider.receipts._get(ref, PREFIX)["raw"]["observation"]["unavailable"] == (
        "incomplete_dependency_direction"
    )


def test_foreign_predecessor_is_checked_before_its_prose(provider):
    a, task = provider.task(), provider.task()
    provider.edge(a, task)
    provider.nodes[a.issue_id]["assignee"]["id"] = str(uuid4())
    assert provider.refresh(task)["state"] == "unknown"
    assert not any("DependencyProse" in q and v.get("id") == a.issue_id for q, v in provider.calls)


def test_other_session_cannot_consume_a_valid_signed_snapshot(provider):
    p, r = provider, provider.rig
    task = p.task()
    prepared = p.source.prepare(task.id, "own")
    handle = r.session(replace(p.scope, logical_session="other"))
    other = p.task(handle=handle)
    with pytest.raises(EvidenceRequired):
        r.core.scheduling.observe(
            handle,
            other.id,
            other.version,
            other.decision_revision,
            prepared.snapshot_ref,
            "foreign",
        )
    assert r.core.scheduling.explain(handle, other.id)["state"] == "unknown"


def test_signed_receipt_does_not_authorize_changed_operation_context(provider):
    task = provider.task()
    provider.source.prepare(task.id, "own")
    with provider.receipts.transaction() as conn:
        row = conn.execute("SELECT id,context FROM dependency_operations").fetchone()
        context = json.loads(row[1])
        context["task_version"] += 1
        conn.execute(
            "UPDATE dependency_operations SET context=? WHERE id=?", (json.dumps(context), row[0])
        )
    with pytest.raises(AccessDenied, match="context changed"):
        provider.source.refresh(task.id, "own")


def test_configured_linear_service_uses_production_dependency_sources(provider, tmp_path):
    import os

    from ai_task_manager.adapters.linear.dependencies.receipts import DependencyReceipts
    from ai_task_manager.application.service.linear import LinearComponents
    from ai_task_manager.config.claim_store import enroll_claim_store
    from ai_task_manager.config.files import write_private
    from ai_task_manager.config.service import load_service
    from ai_task_manager.config.sessions import SessionFile
    from ai_task_manager.domains.work.models import ApprovalPolicy

    p, r = provider, provider.rig
    r.policy = ApprovalPolicy("a" * 64, "b" * 64, "c" * 64)
    r.core.context.approval_policy = r.policy
    task = p.task()
    root = tmp_path / "owned"
    authority = AuthorityJournal(root / "configured.sqlite", root / "configured.key")
    os.chmod(r.db.path, 0o600)
    enroll_claim_store(r.db, authority)
    SessionFile(root / "session.json").save(r.handle, p.scope)
    token = root / "credential.env"
    token.write_text("LINEAR_API_KEY=authored-token\n")
    token.chmod(0o600)
    linear = {
        "expected": asdict(p.expected),
        "agent_connection": asdict(p.connection),
        "credential_file": str(token),
        "credential_key": "LINEAR_API_KEY",
        "journal": str(root / "linear.sqlite"),
    }
    components = LinearComponents(
        linear,
        client_factory=lambda credentials, expected: type(p.client)(p, credentials, expected),
    )
    config = {
        "version": 1,
        "core_database": r.db.path,
        "authority_database": str(authority.path),
        "authority_key": str(authority.key_path),
        "session_file": str(root / "session.json"),
        "approval_policy": asdict(r.policy),
        "linear": linear,
    }
    path = root / "config.json"
    write_private(path, config)
    service = load_service(path, linear_components=components)
    assert isinstance(service.core.scheduling.sources, DependencyReceipts)
    assert service.dependencies.receipts is components.scheduling_sources
    assert service.dependencies.refresh(task.id, "configured")["state"] == "ready"


def test_actual_loopback_http_transport_dependency_reads_and_owned_stop(provider, tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from ai_task_manager.adapters.linear.client import LinearClient
    from ai_task_manager.adapters.linear.dependencies.source import DependencySource
    from ai_task_manager.adapters.linear.transport import HttpTransport
    from tests.linear.helpers import observed_quota, quota_headers

    p = provider
    dispatched = []
    quota = observed_quota(tmp_path, p.expected.viewer_id, credentials=p.client.credentials())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            size = int(self.headers["Content-Length"])
            assert size < 1048576
            payload = json.loads(self.rfile.read(size))
            dispatched.append(payload["query"])
            response = p.send(payload["query"], payload["variables"], None)
            self.send_response(response.status)
            self.send_header("Content-Length", str(len(response.body)))
            for name, value in quota_headers().items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response.body)

    entered = threading.Event()

    class Server(ThreadingHTTPServer):
        def service_actions(self):
            entered.set()

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    try:
        thread.start()
        assert entered.wait(1), "Owned HTTP loop did not enter"
        client = LinearClient(
            HttpTransport(timeout=2, _test_endpoint="http://127.0.0.1:" + str(server.server_port),
                          _quota_store=quota),
            p.client.credentials,
            p.expected,
        )
        client.verify_binding()
        binding_count = len(dispatched)
        assert binding_count == 1
        source = DependencySource(p.rig.core, p.rig.handle, client, p.receipts)
        task = p.task(code=True)
        before = len(dispatched)
        assert source.refresh(task.id, "http-empty")["state"] == "ready"
        collection_queries = dispatched[before:]
        assert len(collection_queries) == 9, collection_queries
        producer_counts = {
            "binding": sum("VerifyBinding" in q for q in collection_queries),
            "schema": sum("DependencySchema" in q for q in collection_queries),
            "metadata": sum("DependencyMetadata" in q for q in collection_queries),
            "prose_and_relations": sum("DependencyProseRelations" in q for q in collection_queries),
        }
        assert producer_counts == {"binding": 2, "schema": 1, "metadata": 4,
                                   "prose_and_relations": 2}

        # The actual DeliveryDependencies local loop consumes a fresh signed
        # observation without sending HTTP again on unchanged pre-renewal ticks.
        from ai_task_manager.installation.workflows.delivery_dependencies import (
            DeliveryDependencies,
        )
        r = p.rig
        running = r.transition(task, S.TODO, "enqueue")
        p.sync(running)
        from ai_task_manager.domains.scheduling.resources import AdmissionResources
        r.resource_sources.rows[running.id] = AdmissionResources(
            p.scope, running.id, running.prose_digest, running.decision_revision,
            "authored-project", (), (),
        )
        r.core.execution.publish_resources(running)
        running, attempt = r.running(running)
        p.sync(running)
        running, attempt = r.settle(running, attempt)
        p.sync(running)
        controller = DeliveryDependencies(r.core, r.handle, source, r.now + 120)
        ticks = []

        def local_tick(seconds):
            ticks.append((r.now, len(dispatched)))
            if len(ticks) == 5:
                controller._stop.set()
                return True
            r.now += seconds
            return False

        controller._stop.wait = local_tick
        before_start = len(dispatched)
        controller.start({"task_id": running.id, "attempt_id": attempt.id,
                          "prose_digest": running.prose_digest,
                          "decision_revision": running.decision_revision})
        controller._thread.join(2)
        controller.close()
        assert not controller._thread.is_alive() and controller._error is None
        assert len(dispatched) - before_start == 9
        assert len(ticks) == 5 and {calls for _, calls in ticks} == {len(dispatched)}
        (tmp_path / "http-counts.json").write_text(json.dumps({
            "binding": binding_count, "full_collection": len(collection_queries),
            "collection_producers": producer_counts,
            "initial_delivery_refresh": len(dispatched) - before_start,
            "unchanged_local_ticks": len(ticks), "tick_http_requests": 0,
        }))
        a = p.task()
        p.edge(a, task)
        assert source.refresh(task.id, "http-blocked")["state"] == "blocked"
    finally:
        if entered.is_set():
            server.shutdown()
        server.server_close()
        if thread.ident is not None:
            thread.join(2)
        assert not thread.is_alive() and server.socket.fileno() == -1
        (tmp_path / "http-stop.json").write_text(
            json.dumps(
                {
                    "thread_alive": thread.is_alive(),
                    "socket_fileno": server.socket.fileno(),
                    "actual_transport": "HttpTransport",
                    "live_provider": False,
                }
            )
            + "\n"
        )


def test_credential_rotation_during_collection_blocks(provider):
    from ai_task_manager.adapters.linear.models import Credentials

    task = provider.task()
    token = ["authored-token"]
    provider.client.credentials = lambda: Credentials(token[0])

    def rotate(query, variables):
        if "DependencyMetadata" in query:
            token[0] = "authored-rotated"

    provider.callback = rotate
    assert provider.refresh(task)["state"] == "unknown"


@pytest.mark.parametrize("limit", ["nodes", "relations", "pages"])
def test_finite_collection_limits_are_not_partial_empty_graphs(provider, limit):
    from ai_task_manager.adapters.linear.dependencies.reader import DependencyReader

    a, b, task = provider.task(), provider.task(), provider.task()
    provider.edge(a, task)
    provider.edge(b, task)
    if limit == "pages":
        provider.page_size = 1
        provider.client.max_pages = 1
    reader = DependencyReader(
        provider.client,
        provider.scope,
        max_nodes=1 if limit == "nodes" else 128,
        max_relations=1 if limit == "relations" else 2048,
    )
    observation = reader.observe(
        task, {x.issue_id: x for x in provider.rig.core.list_tasks(provider.rig.handle)}
    )
    assert not observation.complete and "limit" in observation.raw["unavailable"]


def test_provider_error_body_is_not_retained_in_scheduling_receipts(provider):
    task = provider.task()
    original = provider.send

    def failure(query, variables, credentials):
        if "DependencyMetadata" in query:
            return WireResponse(
                200,
                {},
                json.dumps(
                    {"errors": [{"message": "foreign-private-sentinel", "extensions": {}}]}
                ).encode(),
            )
        return original(query, variables, credentials)

    provider.send = failure
    assert provider.refresh(task, "privacy")["state"] == "unknown"
    ref = provider.receipts.lookup(provider.scope, task.id, "privacy")["receipt"]
    assert "foreign-private-sentinel" not in json.dumps(provider.receipts._get(ref, PREFIX))


@pytest.mark.parametrize("timing", ["before_expiry", "after_expiry"])
def test_active_attempt_refresh_preserves_expiry_fence(provider, timing):
    from ai_task_manager.domains.scheduling.models import SchedulingBlocked

    p, r = provider, provider.rig
    task = p.task()
    p.refresh(task)
    task = r.transition(task, S.TODO, "enqueue")
    p.sync(task)
    task, attempt = r.running(task)
    p.sync(task)
    before = r.core.scheduling.explain(r.handle, task.id)
    r.now += 29 if timing == "before_expiry" else 31
    current = p.refresh(task, "active-refresh")
    assert current["graph_version"] == before["graph_version"]
    if timing == "before_expiry":
        assert current["state"] == "ready"
        assert (
            r.core.execution.validate_work_lease(r.handle, attempt.id, attempt.version) == attempt
        )
    else:
        assert current["state"] == "blocked"
        assert current["blockers"][0]["code"] == "attempt_dependencies_invalidated"
        with pytest.raises(SchedulingBlocked):
            r.core.execution.validate_work_lease(r.handle, attempt.id, attempt.version)
    with r.db.transaction() as conn:
        row = conn.execute(
            "SELECT invalidated_by FROM scheduling_attempts WHERE attempt_id=?", (attempt.id,)
        ).fetchone()
        assert bool(row[0]) == (timing == "after_expiry")
        assert (
            conn.execute(
                "SELECT count(*) FROM resource_leases WHERE attempt_id=?", (attempt.id,)
            ).fetchone()[0]
            == 2
        )
        assert (
            conn.execute("SELECT active FROM attempts WHERE id=?", (attempt.id,)).fetchone()[0] == 1
        )


@pytest.mark.parametrize("variant", ["current", "held", "expired-proof", "wrong-subject"])
def test_configured_milestone_releases_only_current_observed_merge(provider, tmp_path, variant):
    from ai_task_manager.application.service.linear import LinearComponents

    p, r = provider, provider.rig
    predecessor = p.task(code=True, title="Owned code predecessor")
    dependent = p.task(code=variant.startswith("cold-"), title="Explicit merge dependent")
    p.edge(predecessor, dependent)
    declarations = [
        {
            "task_prose_digest": dependent.prose_digest,
            "predecessor_prose_digest": predecessor.prose_digest,
            "condition": "code_merged",
        }
    ]
    config = {
        "expected": asdict(p.expected),
        "agent_connection": asdict(p.connection),
        "credential_environment": "AITM_CREDENTIAL_AUTHORED",
        "journal": str(tmp_path / "owned" / "milestone.sqlite"),
        "dependency_milestones": declarations,
    }
    components = LinearComponents(
        config,
        client_factory=lambda credentials, expected: p.client,
    )
    components.scheduling_sources.clock = lambda: r.now
    source = components.dependencies(r.core, r.handle)
    r.core.scheduling.sources = components.scheduling_sources
    assert source.refresh(dependent.id, "before-merge")["state"] == "blocked"
    source.refresh(predecessor.id, "before-start")
    predecessor = r.transition(predecessor, S.TODO, "enqueue")
    p.sync(predecessor)
    from ai_task_manager.domains.scheduling.resources import AdmissionResources

    r.resource_sources.rows[predecessor.id] = AdmissionResources(
        p.scope,
        predecessor.id,
        predecessor.prose_digest,
        predecessor.decision_revision,
        "authored-project",
        (),
        (),
    )
    predecessor, attempt = r.running(predecessor)
    predecessor = r.complete(predecessor)
    predecessor, _ = r.settle(predecessor, attempt)
    merge = r.proof(
        "merge", predecessor.id, {"commit": "a" * 40, "prose_digest": predecessor.prose_digest}
    )
    predecessor = r.transition(predecessor, S.IN_DEV, "merge", {"merge": merge})
    p.sync(predecessor)
    preparations = []

    def prepare_current(subject, reference):
        assert p.depth == 0
        assert subject == predecessor.id and reference == merge
        preparations.append((subject, reference))
        if variant.startswith("cold-"):
            r.proofs.evidence[reference] = replace(
                r.proofs.evidence[reference], expires_at=r.now + 5
            )
        return reference

    r.core.context.evidence.prepare_merge_milestone = prepare_current
    if variant == "held":
        with r.db.transaction() as conn:
            predecessor = work.save(
                conn, replace(predecessor, human_hold=True), predecessor.version
            )
        p.sync(predecessor)
    elif variant == "expired-proof":
        r.proofs.evidence[merge] = replace(r.proofs.evidence[merge], expires_at=r.now)
    elif variant == "wrong-subject":
        r.proofs.evidence[merge] = replace(r.proofs.evidence[merge], subject=dependent.id)
    view = source.refresh(dependent.id, "after-merge")
    expected = "ready" if variant == "current" or variant.startswith("cold-") else "blocked"
    assert view["state"] == expected, view
    snapshot = components.scheduling_sources.verify_snapshot(
        source.prepare(dependent.id, "after-merge").snapshot_ref
    )
    assert len(snapshot.dependencies) == 1
    assert snapshot.dependencies[0].condition == "code_merged"
    assert preparations == [(predecessor.id, merge)]
    assert dict(snapshot.dependencies[0].evidence_refs)["merge"] == merge
    if variant.startswith("cold-"):
        from ai_task_manager.domains.work.errors import WorkError
        from ai_task_manager.installation.workflows.delivery_dependencies import (
            DeliveryDependencies,
        )

        dependent = r.transition(dependent, S.TODO, "enqueue")
        r.resource_sources.rows[dependent.id] = AdmissionResources(
            p.scope, dependent.id, dependent.prose_digest, dependent.decision_revision,
            "authored-project", (), (),
        )
        p.sync(dependent)
        source.refresh(dependent.id, "cold-queued")
        dependent, dependent_attempt = r.running(dependent)
        dependent, dependent_attempt = r.settle(dependent, dependent_attempt)
        p.sync(dependent)
        original_view = source.refresh(dependent.id, "cold-stopped")
        deadline = r.now + 300
        row = {"task_id": dependent.id, "attempt_id": dependent_attempt.id,
               "prose_digest": dependent.prose_digest,
               "decision_revision": dependent.decision_revision}
        r.now += 167
        controller = DeliveryDependencies(r.core, r.handle, source, deadline)
        calls = len(p.calls)
        fresh = None
        if variant == "cold-refresh-before-start":
            fresh = source.refresh(dependent.id, "cold-fresh-original-authority")
            assert fresh["state"] == "ready"
            assert fresh["graph_version"] == original_view["graph_version"]
        try:
            with pytest.raises(WorkError) as refusal:
                controller.start(row)
            if fresh is None:
                assert len(p.calls) == calls
            after = r.core.scheduling.explain(r.handle, dependent.id)
            if fresh is None:
                assert after["state"] == "unknown"
                assert after["source_revision"] == original_view["source_revision"]
            else:
                assert "guarded stop/resume" in str(refusal.value)
            (tmp_path / "cold-expired-dependency.json").write_text(json.dumps({
                "age_seconds": 167, "original_deadline": deadline,
                "before": original_view, "after": after,
                "refusal_type": type(refusal.value).__name__,
                "refusal": str(refusal.value),
                "fresh_before_first_start": fresh,
            }, indent=2) + "\n")
        finally:
            controller.close()
        return
    # Unconfigured provider edges retain their stronger complete-work meaning.
    default = type(source)(r.core, r.handle, p.client, components.scheduling_sources)
    ordinary = p.task(title="Unconfigured dependent")
    p.edge(predecessor, ordinary)
    assert default.refresh(ordinary.id, "unconfigured")["state"] == "blocked"
    with pytest.raises(EvidenceRequired, match="different source"):
        default.refresh(dependent.id, "changed-policy")
    with pytest.raises(AccessDenied, match="another milestone policy"):
        default.prepare(dependent.id, "after-merge")


@pytest.mark.parametrize("refresh_before_start", [False, True])
def test_cold_delivery_refuses_expired_declared_milestone_before_collection(
    provider, tmp_path, refresh_before_start
):
    test_configured_milestone_releases_only_current_observed_merge(
        provider, tmp_path, "cold-refresh-before-start" if refresh_before_start else "cold-expired"
    )


@pytest.mark.parametrize("variant", ["unknown-field", "digest", "self", "duplicate", "condition"])
def test_milestone_declaration_schema_is_closed(variant):
    from ai_task_manager.adapters.linear.dependencies.source import milestone_declarations
    from ai_task_manager.domains.work.errors import ValidationError

    value = {
        "task_prose_digest": "a" * 64,
        "predecessor_prose_digest": "b" * 64,
        "condition": "code_merged",
    }
    if variant == "unknown-field":
        value["proof"] = "untrusted"
    elif variant == "digest":
        value["task_prose_digest"] = "not-a-digest"
    elif variant == "self":
        value["predecessor_prose_digest"] = value["task_prose_digest"]
    elif variant == "condition":
        value["condition"] = "unit_verified"
    with pytest.raises(ValidationError):
        milestone_declarations([value, value] if variant == "duplicate" else [value])


@pytest.mark.parametrize(
    "variant", ["missing-edge", "noncode", "ambiguous-predecessor", "ambiguous-dependent"]
)
def test_explicit_milestone_cannot_invent_or_ambiguously_bind_provider_edge(provider, variant):
    from ai_task_manager.adapters.linear.dependencies.source import (
        DependencySource,
        milestone_declarations,
    )

    p, r = provider, provider.rig
    a = p.task(code=variant != "noncode", title="Predecessor")
    task = p.task(title="Dependent")
    if variant != "missing-edge":
        p.edge(a, task)
    if variant.startswith("ambiguous"):
        p.task(
            code=variant.endswith("predecessor"),
            title="Predecessor" if variant.endswith("predecessor") else "Dependent",
        )
    declared = milestone_declarations(
        [
            {
                "task_prose_digest": task.prose_digest,
                "predecessor_prose_digest": a.prose_digest,
                "condition": "code_merged",
            }
        ]
    )
    source = DependencySource(r.core, r.handle, p.client, p.receipts, milestones=declared)
    with pytest.raises(EvidenceRequired, match="one current observed code edge"):
        source.refresh(task.id, "refuse-edge")
    assert r.core.scheduling.explain(r.handle, task.id)["state"] == "unknown"
