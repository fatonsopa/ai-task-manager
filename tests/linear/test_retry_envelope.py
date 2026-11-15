"""Authenticated authored retries; these controls do not establish live Linear acceptance."""

import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from ai_task_manager.adapters.linear.receipts import ProviderJournal
from ai_task_manager.adapters.linear.webhooks import WebhookReceiver
from ai_task_manager.application.linear_sync.inbound import InboundDrain
from ai_task_manager.config.files import digest
from tests.linear.helpers import EXPECTED, STATES, WEBHOOK, LinearRig, ProviderFixture, uid

# Exact timestamp values from the retained provider retry diagnosis; all other data is authored.
ORIGINAL_STAMP = 1793763226900
RETRY_STAMP = 1793763285910
DELIVERY = uid(178)


@pytest.fixture
def rig(tmp_path):
    provider = ProviderFixture()
    try:
        value = LinearRig(tmp_path, provider)
        value.now = ORIGINAL_STAMP / 1000 + 0.061
        yield value
    finally:
        provider.close()
        assert not provider.thread.is_alive()


def changed_event(rig):
    task = rig.task()
    rig.provider.edit(task.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
    return task, rig.provider.event(task.issue_id, timestamp=ORIGINAL_STAMP)


def receive(rig, payload, delivery=DELIVERY):
    return rig.receiver.receive("POST", *rig.signed(payload, delivery)).status


def refreshed(rig, payload):
    rig.now = RETRY_STAMP / 1000 + 0.061
    return {**payload, "webhookTimestamp": RETRY_STAMP}


def snapshot(rig):
    return (
        rig.journal.deliveries(rig.scope),
        rig.core.sync.inbound(rig.handle),
        rig.core.notifications.list_events(rig.handle),
        rig.core.list_tasks(rig.handle),
    )


def test_re01_observed_timestamp_variation_preserves_original_and_applies_once(rig):
    task, event = changed_event(rig)
    assert receive(rig, event) == 200
    original = rig.journal.deliveries(rig.scope)[0]
    assert rig.inbound.run_once(rig.handle)["state"] == "applied"
    settled = snapshot(rig)
    requests = len(rig.provider.requests)
    retry = refreshed(rig, event)
    assert receive(rig, retry) == 200
    assert rig.inbound.run_once(rig.handle) is None
    assert snapshot(rig) == settled
    assert len(rig.provider.requests) == requests
    row = rig.journal.deliveries(rig.scope)[0]
    assert row["raw_digest"] == hashlib.sha256(rig.signed(event, DELIVERY)[1]).hexdigest()
    assert row["payload"] == original["payload"] and row["ingress_ref"] == original["ingress_ref"]
    assert json.loads(row["payload"])["webhookTimestamp"] == ORIGINAL_STAMP
    assert rig.current(task).decision_revision == 1
    assert sum(e.kind == "external_change_applied" for e in settled[2]) == 1


@pytest.mark.parametrize("reverse", [False, True])
def test_re02_restart_and_reordered_fresh_attempts_keep_first_receipt(rig, reverse):
    task, event = changed_event(rig)
    retry = refreshed(rig, event)
    first, later = (retry, event) if reverse else (event, retry)
    assert receive(rig, first) == 200
    original = snapshot(rig)
    rig.journal = ProviderJournal(rig.journal.path, clock=lambda: rig.now)
    rig.core = rig.new_core()
    rig.receiver = WebhookReceiver(
        rig.core,
        rig.handle,
        rig.journal,
        rig.client.binding,
        WEBHOOK,
        lambda: b"fixture-webhook-signing-secret",
        clock=lambda: rig.now,
    )
    for payload, delivery in [(first, DELIVERY), (later, DELIVERY), (later, uid(179))]:
        assert receive(rig, payload, delivery) == 200
        assert snapshot(rig) == original
    inbound = InboundDrain(
        rig.core,
        rig.client,
        rig.journal,
        clock=lambda: rig.now,
        authorized_actors={EXPECTED.viewer_id},
    )
    assert inbound.run_once(rig.handle)["state"] == "applied"
    assert inbound.run_once(rig.handle) is None
    assert rig.current(task).decision_revision == 1
    assert len(rig.core.sync.inbound(rig.handle)) == 1


@pytest.mark.parametrize(
    "field", ["actor", "issue", "revision", "action", "state", "createdAt", "extra", "title"]
)
def test_re03_same_delivery_business_changes_conflict(rig, field):
    _, event = changed_event(rig)
    other = rig.task() if field == "issue" else None
    assert receive(rig, event) == 200
    original = snapshot(rig)
    retry = copy.deepcopy(refreshed(rig, event))
    if field == "actor":
        retry["actor"]["id"] = uid(777)
    elif field == "issue":
        retry["data"]["id"] = other.issue_id
    elif field == "revision":
        retry["data"]["updatedAt"] = "2026-11-04T03:34:00.000Z"
    elif field == "action":
        retry["action"] = "remove"
    elif field == "state":
        retry["data"]["stateId"] = STATES["On Hold"]
    elif field == "createdAt":
        retry["createdAt"] = "2026-11-04T03:34:00.000Z"
    elif field == "extra":
        retry["newBusinessField"] = {"value": "not an ignored envelope field"}
    else:
        retry["data"]["title"] = "changed title"
    assert receive(rig, retry) == 409
    assert snapshot(rig) == original


@pytest.mark.parametrize(
    "fault,status",
    [
        ("old_signature", 401),
        ("stale", 401),
        ("future", 401),
        ("header_timestamp", 400),
        ("organization", 403),
        ("webhook", 403),
        ("event", 403),
    ],
)
def test_re04_every_retry_rechecks_authentication_freshness_and_scope(rig, fault, status):
    _, event = changed_event(rig)
    first_headers, _ = rig.signed(event, DELIVERY)
    assert receive(rig, event) == 200
    original = snapshot(rig)
    retry = refreshed(rig, event)
    if fault == "stale":
        retry["webhookTimestamp"] -= 61000
    elif fault == "future":
        retry["webhookTimestamp"] += 61000
    elif fault == "organization":
        retry["organizationId"] = uid(888)
    elif fault == "webhook":
        retry["webhookId"] = uid(888)
    headers, body = rig.signed(retry, DELIVERY)
    replace_header = {
        "old_signature": ("Linear-Signature", dict(first_headers)["Linear-Signature"]),
        "header_timestamp": ("Linear-Timestamp", str(ORIGINAL_STAMP)),
        "event": ("Linear-Event", "Project"),
    }.get(fault)
    if replace_header:
        key, value = replace_header
        headers = [(k, value if k == key else v) for k, v in headers]
    assert rig.receiver.receive("POST", headers, body).status == status
    assert snapshot(rig) == original


def test_re05_retry_repairs_journal_to_core_gap_without_replacing_proof(rig, monkeypatch):
    from ai_task_manager.domains.work.errors import AccessDenied

    task, event = changed_event(rig)
    original_ingest = rig.core.sync.ingest

    def interrupted(*args, **kwargs):
        raise AccessDenied("Authored interruption after durable journal acceptance")

    monkeypatch.setattr(rig.core.sync, "ingest", interrupted)
    assert receive(rig, event) == 503
    original = rig.journal.deliveries(rig.scope)
    assert not rig.core.sync.inbound(rig.handle)
    monkeypatch.setattr(rig.core.sync, "ingest", original_ingest)
    assert receive(rig, refreshed(rig, event)) == 200
    assert rig.journal.deliveries(rig.scope) == original
    assert rig.inbound.run_once(rig.handle)["state"] == "applied"
    assert rig.inbound.run_once(rig.handle) is None
    assert rig.current(task).decision_revision == 1


@pytest.mark.parametrize("conflict", [False, True])
def test_re06_concurrent_separate_connections_admit_one_receipt(rig, conflict):
    _, event = changed_event(rig)
    retry = copy.deepcopy(refreshed(rig, event))
    if conflict:
        retry["actor"]["id"] = uid(777)
    barrier = Barrier(2)

    def submit(payload):
        journal = ProviderJournal(rig.journal.path, clock=lambda: rig.now)
        receiver = WebhookReceiver(
            rig.new_core(),
            rig.handle,
            journal,
            rig.client.binding,
            WEBHOOK,
            lambda: b"fixture-webhook-signing-secret",
            clock=lambda: rig.now,
        )
        barrier.wait(timeout=3)
        return receiver.receive("POST", *rig.signed(payload, DELIVERY)).status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, [event, retry]))
    assert sorted(results) == ([200, 409] if conflict else [200, 200])
    rows = rig.journal.deliveries(rig.scope)
    assert len(rows) == 1 and len(rig.core.sync.inbound(rig.handle)) == 1
    # Both equivalent requests acknowledge success; either may acquire the SQLite
    # transaction first. Bind all retained evidence to the actual winning raw hash.
    accepted = next(
        payload
        for payload in (event, retry)
        if hashlib.sha256(rig.signed(payload, DELIVERY)[1]).hexdigest() == rows[0]["raw_digest"]
    )
    # The raw/semantic hashes identify the winning complete delivery. Until
    # delegation is freshly read back, stored payload retains only safe facts.
    assert rows[0]["semantic_digest"] == digest(
        {key: value for key, value in accepted.items() if key != "webhookTimestamp"}
    )
    retained = json.loads(rows[0]["payload"])
    assert retained == {
        "action": accepted["action"],
        "type": "Issue",
        "organizationId": EXPECTED.workspace_id,
        "webhookId": WEBHOOK,
        "webhookTimestamp": accepted["webhookTimestamp"],
        "actor": accepted["actor"],
        "data": {
            "id": accepted["data"]["id"],
            "teamId": EXPECTED.team_id,
            "projectId": EXPECTED.project_id,
            "assigneeId": EXPECTED.viewer_id,
            "stateId": STATES["Todo"],
            "updatedAt": accepted["data"]["updatedAt"],
        },
    }
    assert "createdAt" not in retained and "description" not in retained["data"]
    if not conflict:
        assert rig.inbound.run_once(rig.handle)["state"] == "applied"
        assert rig.inbound.run_once(rig.handle) is None
        assert (
            sum(
                e.kind == "external_change_applied"
                for e in rig.core.notifications.list_events(rig.handle)
            )
            == 1
        )


@pytest.mark.parametrize("change", [None, "actor", "foreign_prose", "revision", "project"])
def test_re07_scope_removal_hashes_complete_event_but_keeps_only_projection(rig, change):
    _, event = changed_event(rig)
    event["data"]["projectId"] = uid(700)
    event["data"]["description"] = "PRIVATE FOREIGN SENTINEL ORIGINAL"
    assert receive(rig, event) == 200
    original = snapshot(rig)
    retry = copy.deepcopy(refreshed(rig, event))
    if change == "actor":
        retry["actor"]["id"] = uid(701)
    elif change == "foreign_prose":
        retry["data"]["description"] = "PRIVATE FOREIGN SENTINEL CHANGED"
    elif change == "revision":
        retry["data"]["updatedAt"] = "2026-11-04T03:34:00.000Z"
    elif change == "project":
        retry["data"]["projectId"] = uid(702)
    assert receive(rig, retry) == (200 if change is None else 409)
    assert snapshot(rig) == original
    retained = json.loads(original[0][0]["payload"])
    assert retained["data"]["scopeRemoval"] is True
    assert "actor" not in retained and "description" not in retained["data"]
    assert b"PRIVATE FOREIGN SENTINEL" not in Path(rig.journal.path).read_bytes()


def test_re08_legacy_projected_receipt_accepts_exact_bytes_but_refuses_changed_raw(rig):
    _, event = changed_event(rig)
    event["data"]["projectId"] = uid(700)
    # Reconstruct the existing legacy representation, without changing a live journal.
    projected = {
        key: event[key]
        for key in ("action", "type", "organizationId", "webhookId", "webhookTimestamp")
    }
    projected["data"] = {
        "id": event["data"]["id"],
        "updatedAt": event["data"]["updatedAt"],
        "scopeRemoval": True,
        "teamChanged": False,
        "projectChanged": True,
        "scopeDigest": hashlib.sha256(
            json.dumps(
                {
                    "team": event["data"]["teamId"],
                    "project": event["data"]["projectId"],
                },
                sort_keys=True,
            ).encode()
        ).hexdigest(),
    }
    _, body = rig.signed(event, DELIVERY)
    old, created = rig.journal.accept_delivery(rig.scope, DELIVERY, body, projected)
    assert created
    assert receive(rig, event) == 200
    original = snapshot(rig)
    assert receive(rig, refreshed(rig, event)) == 409
    assert snapshot(rig) == original and original[0][0] == old


def test_re09_revoked_core_session_cannot_replay_existing_delivery(rig):
    _, event = changed_event(rig)
    assert receive(rig, event) == 200
    rows = rig.journal.deliveries(rig.scope)
    rig.core.revoke_session(rig.handle)
    assert receive(rig, refreshed(rig, event)) == 403
    assert rig.journal.deliveries(rig.scope) == rows


def test_re10_same_delivery_identifier_is_scoped_to_registered_session(rig):
    _, event = changed_event(rig)
    assert receive(rig, event) == 200
    original = snapshot(rig)
    other_scope = replace(rig.scope, logical_session="other-authored-session")
    binding = replace(
        rig.client.binding,
        connection=replace(
            rig.client.binding.connection, logical_session=other_scope.logical_session
        ),
    )
    handle = rig.core.register_session(rig.journal.bind_principal(binding, other_scope))
    receiver = WebhookReceiver(
        rig.core,
        handle,
        rig.journal,
        rig.client.binding,
        WEBHOOK,
        lambda: b"fixture-webhook-signing-secret",
        clock=lambda: rig.now,
    )
    assert receiver.receive("POST", *rig.signed(refreshed(rig, event), DELIVERY)).status == 200
    assert not rig.journal.deliveries(other_scope)
    assert not rig.core.sync.inbound(handle)
    assert rig.journal.ignored(other_scope)[0]["reason"] == "unregistered_issue"
    assert snapshot(rig) == original


def receive_in_child(root, handle, binding, payload, now, result):
    """A fresh process opens only the existing authored provider/Core stores."""
    from ai_task_manager.application.core import Core
    from ai_task_manager.domains.work.models import ApprovalPolicy, CapacityPolicy
    from ai_task_manager.storage.database import Database

    journal = ProviderJournal(root / "provider.sqlite", clock=lambda: now)
    core = Core(
        Database(root / "core.sqlite"),
        ApprovalPolicy("writer-frozen", "issue-reference-frozen", "style-frozen"),
        (CapacityPolicy("host", 2, 2),),
        principals=journal,
        evidence=journal,
        clock=lambda: now,
    )
    receiver = WebhookReceiver(
        core,
        handle,
        journal,
        binding,
        WEBHOOK,
        lambda: b"fixture-webhook-signing-secret",
        clock=lambda: now,
    )
    import hmac

    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = [
        ("Content-Type", "application/json"),
        ("Linear-Delivery", DELIVERY),
        ("Linear-Event", "Issue"),
        ("Linear-Timestamp", str(payload["webhookTimestamp"])),
        (
            "Linear-Signature",
            hmac.new(
                b"fixture-webhook-signing-secret",
                body,
                hashlib.sha256,
            ).hexdigest(),
        ),
    ]
    result.put(receiver.receive("POST", headers, body).status)


def test_re11_fresh_process_reopens_pending_then_applied_receipt(rig, tmp_path):
    import multiprocessing

    task, event = changed_event(rig)
    assert receive(rig, event) == 200
    original = rig.journal.deliveries(rig.scope)[0]
    retry = refreshed(rig, event)
    context = multiprocessing.get_context("spawn")
    process_records = []
    for applied in (False, True):
        before = snapshot(rig)
        result = context.Queue()
        child = context.Process(
            target=receive_in_child,
            args=(tmp_path, rig.handle, rig.client.binding, retry, rig.now, result),
        )
        child.start()
        try:
            assert result.get(timeout=10) == 200
            child.join(timeout=5)
            assert not child.is_alive() and child.exitcode == 0
            process_records.append({"pid": child.pid, "exitcode": child.exitcode, "reaped": True})
        finally:
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
            assert not child.is_alive()
            result.close()
            result.join_thread()
            child.close()
        assert snapshot(rig) == before
        if not applied:
            assert rig.inbound.run_once(rig.handle)["state"] == "applied"
    assert rig.inbound.run_once(rig.handle) is None
    row = rig.journal.deliveries(rig.scope)[0]
    assert row["id"] == original["id"] and row["raw_digest"] == original["raw_digest"]
    assert rig.current(task).decision_revision == 1
    (tmp_path / "child-cleanup.json").write_text(json.dumps(process_records))
