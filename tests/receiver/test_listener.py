"""Finite local HTTP/lifecycle controls with existing real Core and validator."""

import json
import socket
import threading
import time
from pathlib import Path

import pytest

from ai_task_manager.adapters.linear.listener import LoopbackListener
from ai_task_manager.adapters.linear.models import HttpResponse
from ai_task_manager.application.linear_sync.listener import InstalledReceiver
from ai_task_manager.config.files import write_private
from ai_task_manager.domains.work.errors import AccessDenied
from tests.linear.helpers import STATES, uid

from .helpers import ROUTE, Fixture, request, wait_for


@pytest.fixture
def fixture(tmp_path):
    value = Fixture(tmp_path)
    try:
        yield value
    finally:
        value.close()


@pytest.fixture
def running(fixture):
    service = fixture.load()
    receiver = InstalledReceiver(service, fixture.config)
    receiver.start()
    try:
        yield fixture, service, receiver
    finally:
        result = receiver.stop()
        (fixture.root / "cleanup.json").write_text(json.dumps(result))
        assert result["stopped"], result


def test_ir02_authenticated_event_applies_through_existing_core(running):
    fixture, service, receiver = running
    fixture.provider.edit(fixture.task.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
    headers, body = fixture.event()
    assert request(receiver.http.address, ROUTE, headers, body)[0] == 200
    wait_for(
        lambda: service.core.get_task(service.handle, fixture.task.id).observed_status == "Todo"
    )
    before = service.core.get_task(service.handle, fixture.task.id)
    assert request(receiver.http.address, ROUTE, headers, body)[0] == 200
    time.sleep(0.1)
    after = service.core.get_task(service.handle, fixture.task.id)
    assert after.version == before.version
    assert len(service.inbound.journal.deliveries(service.scope)) == 1
    assert request(receiver.http.address, receiver.http.health_route, method="GET")[0] == 200


@pytest.mark.parametrize(
    "fault, status",
    [
        ("signature", 401),
        ("scope", 403),
        ("route", 404),
        ("method", 405),
        ("body", 401),
        ("duplicate", 400),
    ],
)
def test_ir03_invalid_delivery_has_no_effect(running, fault, status):
    fixture, service, receiver = running
    headers, body = fixture.event(organizationId=uid(444)) if fault == "scope" else fixture.event()
    route, method = ROUTE, "POST"
    if fault == "signature":
        headers = [(k, "0" * 64 if k == "Linear-Signature" else v) for k, v in headers]
    if fault == "route":
        route += "/foreign"
    if fault == "method":
        method = "GET"
    if fault == "body":
        body = b"not JSON"
    if fault == "duplicate":
        headers.append(headers[0])
    assert request(receiver.http.address, route, headers, body, method)[0] == status
    assert not service.inbound.journal.deliveries(service.scope)
    assert service.core.get_task(service.handle, fixture.task.id).observed_status == "Backlog"


def test_ir03_unrelated_delivery_retains_only_minimal_metadata(running):
    fixture, service, receiver = running
    payload = fixture.provider.event(fixture.task.issue_id, timestamp=int(time.time() * 1000))
    payload["data"]["id"] = uid(999)
    payload["data"]["private_text"] = "foreign body must not persist"
    headers, body = fixture.rig.signed(payload)
    assert request(receiver.http.address, ROUTE, headers, body)[0] == 200
    assert not service.inbound.journal.deliveries(service.scope)
    assert b"foreign body must not persist" not in Path(service.inbound.journal.path).read_bytes()


@pytest.mark.parametrize("partial_core, lease", [(False, False), (True, False), (False, True)])
def test_ir05_restart_drains_durable_work_without_new_delivery(fixture, partial_core, lease):
    service = fixture.load()
    first = InstalledReceiver(service, fixture.config)
    fixture.provider.edit(fixture.task.issue_id, state={"id": STATES["Todo"], "name": "Todo"})
    headers, body = fixture.event()
    if partial_core:
        payload = json.loads(body)
        service.inbound.journal.accept_delivery(
            service.scope, dict(headers)["Linear-Delivery"], body, payload
        )
    else:
        assert first.receive("POST", headers, body).status == 200
    if lease:
        service.inbound.journal.claim_delivery(
            service.scope, "dead-fixture-owner", time.time(), lease_seconds=0.2
        )
    assert first.stop()["stopped"]
    second = InstalledReceiver(fixture.load(), fixture.config)
    second.start()
    try:
        wait_for(
            lambda: (
                second.service.core.get_task(second.service.handle, fixture.task.id).observed_status
                == "Todo"
                and second.service.inbound.journal.deliveries(service.scope)[0]["state"]
                == "applied"
            )
        )
        assert second.service.inbound.journal.deliveries(service.scope)[0]["state"] == "applied"
    finally:
        assert second.stop()["stopped"]


def test_ir06_second_owner_and_port_collision_preserve_first(running):
    fixture, service, first = running
    second = InstalledReceiver(fixture.load(), fixture.config)
    with pytest.raises(BlockingIOError):
        second.start()
    assert request(first.http.address, first.http.health_route, method="GET")[0] == 200
    other = LoopbackListener(
        lambda *_: HttpResponse(200), route=ROUTE, port=first.http.address[1], health=lambda: b"{}"
    )
    with pytest.raises(OSError):
        other.start()
    assert request(first.http.address, first.http.health_route, method="GET")[0] == 200


@pytest.mark.parametrize(
    "fault", ["secret", "config", "session", "credentials", "owner", "revoked"]
)
def test_ir07_changed_authority_refuses_intake(running, fault):
    fixture, service, receiver = running
    if fault == "revoked":
        service.core.revoke_session(service.handle)
    elif fault == "owner":
        receiver.owner.path.rename(fixture.root / "old-owner")
        receiver.owner.path.write_text("")
        receiver.owner.path.chmod(0o600)
    else:
        path = {
            "secret": fixture.root / "webhook-secret.json",
            "config": fixture.config,
            "session": fixture.root / "session.json",
            "credentials": fixture.root / "credentials.env",
        }[fault]
        path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(AccessDenied):
        receiver.check()
    headers, body = fixture.event()
    assert request(receiver.http.address, ROUTE, headers, body)[0] == 503
    assert not service.inbound.journal.deliveries(service.scope)


@pytest.mark.parametrize(
    "change",
    [
        {"port": "0"},
        {"route": "/"},
        {"lifetime_seconds": 0},
        {"unexpected": True},
        {"webhook_id": "bad"},
    ],
)
def test_ir01_config_refuses_before_listener(fixture, change):
    write_private(fixture.receiver, {**fixture.values, **change}, replace=True)
    with pytest.raises(Exception):
        InstalledReceiver(fixture.load(), fixture.config)
    assert not list(fixture.root.glob(".linear-receiver*"))


def test_ir04_bounded_headers_body_reads_and_late_intake(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def receive(*_):
        entered.set()
        release.wait(8)
        return HttpResponse(200)

    listener = LoopbackListener(receive, route=ROUTE, port=0, health=lambda: b"{}")
    listener.REQUEST_SECONDS = 0.4
    listener.start()
    try:
        with socket.create_connection(listener.address) as stream:
            stream.sendall(b"POST " + ROUTE.encode() + b" HTTP/1.0\r\nX: " + b"a" * 17000)
            assert b"431" in stream.recv(1024)
        with socket.create_connection(listener.address) as stream:
            stream.sendall(
                b"POST " + ROUTE.encode() + b" HTTP/1.0\r\nContent-Length: 1048577\r\n\r\n"
            )
            assert b"413" in stream.recv(1024)
        started = time.monotonic()
        with socket.create_connection(listener.address) as stream:
            stream.sendall(b"POST ")
            stream.settimeout(2)
            stream.recv(1024)
        assert time.monotonic() - started < 1.5
        assert request(listener.address, ROUTE)[0] == 503
        assert entered.is_set()
        assert not listener.close(0.1)["stopped"]
        release.set()
        assert listener.close(2)["stopped"]
    finally:
        release.set()
        assert listener.close(2)["stopped"]


def test_ir04_capacity_stays_owned_until_late_intake_finishes():
    release = threading.Event()
    listener = LoopbackListener(
        lambda *_: (release.wait(8), HttpResponse(200))[1],
        route=ROUTE,
        port=0,
        health=lambda: b"{}",
    )
    listener.REQUEST_SECONDS = 0.2
    listener.start()
    try:
        for _ in range(4):
            assert request(listener.address, ROUTE)[0] == 503
        before = time.monotonic()
        assert request(listener.address, ROUTE)[0] == 503
        assert time.monotonic() - before < 0.15
        assert not listener.close(0.05)["stopped"]
    finally:
        release.set()
        assert listener.close(2)["stopped"]


def test_ir07_drain_failure_is_visible_and_owned(fixture, monkeypatch):
    service = fixture.load()

    def failed(*_, **__):
        raise RuntimeError("private fixture body")

    monkeypatch.setattr(service.inbound, "run_once", failed)
    receiver = InstalledReceiver(service, fixture.config)
    receiver.start()
    try:
        wait_for(lambda: receiver.failed)
        assert receiver.failed == "receiver_drain_failed"
        headers, body = fixture.event()
        assert request(receiver.http.address, ROUTE, headers, body)[0] == 503
        assert not service.inbound.journal.deliveries(service.scope)
        assert "private fixture body" not in json.dumps(receiver.stop())
    finally:
        assert receiver.stop()["stopped"]


def test_ir07_listener_start_failure_releases_own_lock(fixture, monkeypatch):
    service = fixture.load()
    receiver = InstalledReceiver(service, fixture.config)
    with monkeypatch.context() as patch:

        def failed(*_):
            raise RuntimeError("authored thread start failure")

        patch.setattr(threading.Thread, "start", failed)
        with pytest.raises(RuntimeError):
            receiver.start()
    assert receiver.stop()["stopped"]
    successor = InstalledReceiver(fixture.load(), fixture.config)
    successor.start()
    assert successor.stop()["stopped"]


def test_ir07_expired_enrollment_refuses_even_while_core_session_is_valid(running, monkeypatch):
    fixture, service, receiver = running
    assert service.core.session_binding(service.handle)
    monkeypatch.setattr(receiver.observations.journal, "clock", lambda: time.time() + 3600)
    with pytest.raises(AccessDenied):
        receiver.check()
    assert service.core.session_binding(service.handle)
    headers, body = fixture.event()
    assert request(receiver.http.address, ROUTE, headers, body)[0] == 503
    assert not service.inbound.journal.deliveries(service.scope)


def test_ir03_authenticated_malformed_json_refuses_before_intake(running):
    import hashlib
    import hmac

    from .helpers import SECRET

    fixture, service, receiver = running
    headers, _ = fixture.event()
    body = b'{"ambiguous":'
    signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = [(k, signature if k == "Linear-Signature" else v) for k, v in headers]
    assert request(receiver.http.address, ROUTE, headers, body)[0] == 400
    assert not service.inbound.journal.deliveries(service.scope)


def test_ir07_revoked_enrollment_stops_current_listener(running):
    from ai_task_manager.adapters.decisions.observations import bind_record
    from ai_task_manager.config.files import digest

    fixture, service, receiver = running
    bind_record(
        receiver.observations.journal,
        "session-revoked:" + digest(service.grant_ref),
        {"scope": {}, "reason": "authored revocation"},
    )
    with pytest.raises(AccessDenied):
        receiver.check()
