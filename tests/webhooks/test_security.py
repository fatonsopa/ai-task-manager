import hashlib
import hmac
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from tests.linear.helpers import LinearRig, ProviderFixture, uid


@pytest.fixture
def rig(tmp_path):
    provider = ProviderFixture()
    try:
        rig = LinearRig(tmp_path, provider)
        rig.task()
        yield rig
    finally:
        provider.close()


def event(rig):
    return rig.provider.event(rig.core.list_tasks(rig.handle)[0].issue_id)


@pytest.mark.parametrize(
    "change", ["raw_whitespace", "bad_secret", "nonhex", "short", "missing", "tampered"]
)
def test_l11_raw_signature_rejects_alteration(rig, change):
    headers, body = rig.signed(event(rig))
    if change == "raw_whitespace":
        body = json.dumps(json.loads(body), indent=2).encode()
    elif change == "bad_secret":
        headers, body = rig.signed(event(rig), secret=b"other-secret")
    elif change == "nonhex":
        headers = [
            (key, "z" * 64 if key == "Linear-Signature" else value) for key, value in headers
        ]
    elif change == "short":
        headers = [(key, "ab" if key == "Linear-Signature" else value) for key, value in headers]
    elif change == "missing":
        headers = [(key, value) for key, value in headers if key != "Linear-Signature"]
    else:
        body += b" "
    assert rig.receiver.receive("POST", headers, body).status == 401
    assert rig.journal.deliveries(rig.scope) == []


@pytest.mark.parametrize("stamp", [1793520000000 - 61000, 1793520000000 + 61000, True, 1.5, None])
def test_l13_signed_timestamp_freshness_and_type(rig, stamp):
    payload = event(rig)
    payload["webhookTimestamp"] = stamp
    headers, body = rig.signed(payload)
    assert rig.receiver.receive("POST", headers, body).status in {400, 401}


@pytest.mark.parametrize(
    "change,status",
    [
        ("method", 405),
        ("content_type", 415),
        ("duplicate_header", 400),
        ("timestamp_header", 400),
        ("delivery_id", 400),
        ("oversize", 413),
    ],
)
def test_l12_http_bounds_and_ambiguity(rig, change, status):
    headers, body = rig.signed(event(rig))
    method = "POST"
    if change == "method":
        method = "GET"
    elif change == "content_type":
        headers = [
            (key, "text/plain" if key == "Content-Type" else value) for key, value in headers
        ]
    elif change == "duplicate_header":
        headers.append(("linear-signature", "0" * 64))
    elif change == "timestamp_header":
        headers = [(key, "1" if key == "Linear-Timestamp" else value) for key, value in headers]
    elif change == "delivery_id":
        headers = [(key, "guess" if key == "Linear-Delivery" else value) for key, value in headers]
    else:
        body = b"x" * (1048576 + 1)
    assert rig.receiver.receive(method, headers, body).status == status


@pytest.mark.parametrize(
    "body",
    [b'{"webhookTimestamp":1,"webhookTimestamp":2}', b"\xff", b"{", b'{"webhookTimestamp":NaN}'],
)
def test_l12_signed_ambiguous_or_malformed_json_refused(rig, body):
    headers, _ = rig.signed(event(rig))
    signature = hmac.new(b"fixture-webhook-signing-secret", body, hashlib.sha256).hexdigest()
    headers = [(key, signature if key == "Linear-Signature" else value) for key, value in headers]
    assert rig.receiver.receive("POST", headers, body).status == 400


def test_l14_persist_before_success_and_wake_failure_is_recoverable(rig, monkeypatch):
    headers, body = rig.signed(event(rig))
    observations = []

    def wake():
        observations.append(
            (len(rig.journal.deliveries(rig.scope)), len(rig.core.sync.inbound(rig.handle)))
        )
        raise RuntimeError("fixture wake failed")

    rig.receiver.wake = wake
    assert rig.receiver.receive("POST", headers, body).status == 200
    assert observations == [(1, 1)]
    assert rig.journal.deliveries(rig.scope)[0]["state"] == "received"
    monkeypatch.setattr(
        rig.journal,
        "accept_delivery",
        lambda *a, **kw: (_ for _ in ()).throw(sqlite3.OperationalError("fixture disk failure")),
    )
    headers, body = rig.signed(event(rig), delivery_id=str(uuid4()))
    assert rig.receiver.receive("POST", headers, body).status == 503


def test_l15_identical_and_concurrent_duplicate_delivery(rig):
    headers, body = rig.signed(event(rig))
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(
            pool.map(lambda _: rig.receiver.receive("POST", headers, body).status, range(2))
        ) == [200, 200]
    assert len(rig.journal.deliveries(rig.scope)) == 1
    assert len(rig.core.sync.inbound(rig.handle)) == 1


def test_l13_unsigned_delivery_header_cannot_bypass_signed_event_dedup(rig):
    headers, body = rig.signed(event(rig))
    assert rig.receiver.receive("POST", headers, body).status == 200
    changed = [(key, str(uuid4()) if key == "Linear-Delivery" else value) for key, value in headers]
    assert rig.receiver.receive("POST", changed, body).status == 200
    assert len(rig.journal.deliveries(rig.scope)) == 1
    assert len(rig.core.sync.inbound(rig.handle)) == 1


def test_l15_changed_payload_reusing_delivery_id_conflicts(rig):
    payload = event(rig)
    delivery_id = str(uuid4())
    headers, body = rig.signed(payload, delivery_id)
    assert rig.receiver.receive("POST", headers, body).status == 200
    payload["data"]["updatedAt"] = "2026-11-01T00:00:11Z"
    headers, body = rig.signed(payload, delivery_id)
    assert rig.receiver.receive("POST", headers, body).status == 409


@pytest.mark.parametrize(
    "field", ["organization", "webhook", "team", "project", "other_owner_issue"]
)
def test_l16_foreign_scope_has_no_core_effect_or_payload_retention(rig, field):
    payload = event(rig)
    if field == "organization":
        payload["organizationId"] = uid(700)
    elif field == "webhook":
        payload["webhookId"] = uid(700)
    elif field in {"team", "project"}:
        payload["data"]["id"] = uid(700)
        payload["data"][field + "Id"] = uid(700)
    else:
        payload["data"].update(id=uid(700), assigneeId=uid(701))
    headers, body = rig.signed(payload)
    tasks = rig.core.list_tasks(rig.handle)
    expected = 403 if field in {"organization", "webhook"} else 200
    assert rig.receiver.receive("POST", headers, body).status == expected
    assert rig.core.list_tasks(rig.handle) == tasks
    assert rig.journal.deliveries(rig.scope) == []
    assert len(rig.journal.ignored(rig.scope)) == (0 if expected == 403 else 1)


def test_l16_repeated_authentic_unrelated_activity_is_200_filtered_without_content(rig):
    payload = event(rig)
    payload["data"].update(
        id=uid(900),
        projectId=uid(901),
        assigneeId=uid(902),
        title="Foreign private title",
        description="Foreign private body",
    )
    payload["actor"] = {"type": "user", "id": uid(902), "email": "private@example.invalid"}
    headers, body = rig.signed(payload)
    for _ in range(3):
        assert rig.receiver.receive("POST", headers, body).status == 200
    changed = [(key, str(uuid4()) if key == "Linear-Delivery" else value) for key, value in headers]
    assert rig.receiver.receive("POST", changed, body).status == 200
    ignored = rig.journal.ignored(rig.scope)
    assert len(ignored) == 1 and ignored[0]["reason"] == "unregistered_issue"
    assert "Foreign" not in str(ignored) and "private@example" not in str(ignored)
    assert rig.journal.deliveries(rig.scope) == [] and rig.core.sync.inbound(rig.handle) == []
