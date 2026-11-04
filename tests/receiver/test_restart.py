"""Fixed-port ownership and shutdown races; no real provider or live LP09 claim."""

import signal
import socket
import threading

import pytest

from ai_task_manager.adapters.linear.listener import ListenerStopping, LoopbackListener
from ai_task_manager.adapters.linear.models import HttpResponse
from ai_task_manager.application.linear_sync.listener import InstalledReceiver, serve

from .helpers import ROUTE, Fixture, request


def test_rr01_fixed_port_restarts_after_http_connection():
    first = LoopbackListener(
        lambda *_: HttpResponse(200), route=ROUTE, port=0, health=lambda: b"{}"
    )
    first.start()
    address = first.address
    try:
        assert first.reservation.getsockname()[0] == "0.0.0.0"
        assert first.socket.getsockname()[0] == "127.0.0.1"
        assert request(address, ROUTE)[0] == 200
    finally:
        assert first.close(2)["stopped"]
        assert first.socket.fileno() == first.reservation.fileno() == -1
    second = LoopbackListener(
        lambda *_: HttpResponse(200), route=ROUTE, port=address[1], health=lambda: b"{}"
    )
    try:
        second.start()
        assert second.address == address
        assert request(address, ROUTE)[0] == 200
    finally:
        assert second.close(2)["stopped"]


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0"])
def test_rr02_foreign_listener_remains_exclusive(host):
    with socket.socket() as foreign:
        foreign.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        foreign.bind((host, 0))
        foreign.listen(1)
        address = ("127.0.0.1", foreign.getsockname()[1])
        candidate = LoopbackListener(
            lambda *_: HttpResponse(200), route=ROUTE, port=address[1], health=lambda: b"{}"
        )
        try:
            with pytest.raises(OSError):
                candidate.start()
        finally:
            assert candidate.close(2)["stopped"]
            assert candidate.socket.fileno() == candidate.reservation.fileno() == -1
        with socket.create_connection(address, timeout=2):
            foreign.settimeout(2)
            accepted, _ = foreign.accept()
            accepted.close()


def test_rr02_reservation_excludes_wildcard_before_and_after_http_listen(monkeypatch):
    listener = LoopbackListener(
        lambda *_: HttpResponse(200), route=ROUTE, port=0, health=lambda: b"{}"
    )
    actual = listener.socket
    contender = socket.socket()
    contender.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    foreign_won = False

    def refuse_foreign():
        with socket.socket() as foreign:
            foreign.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(OSError):
                foreign.bind(("0.0.0.0", listener.address[1]))
                foreign.listen(1)

    class BeforeListen:
        def __getattr__(self, name):
            return getattr(actual, name)

        def listen(self, capacity):
            nonlocal foreign_won
            try:
                contender.bind(("0.0.0.0", listener.address[1]))
                contender.listen(1)
                foreign_won = True
            except OSError:
                contender.close()
            return actual.listen(capacity)

    monkeypatch.setattr(listener, "socket", BeforeListen())
    try:
        try:
            listener.start()
        except OSError:
            # Some kernels allow two bound sockets until one begins listening.
            # A contender that wins this race must make product startup refuse.
            assert foreign_won
        else:
            assert not foreign_won
            refuse_foreign()
            assert request(listener.address, ROUTE)[0] == 200
    finally:
        assert listener.close(2)["stopped"]
        assert actual.fileno() == listener.reservation.fileno() == -1
        contender.close()


@pytest.mark.parametrize("stage", ["bind", "listen"])
def test_rr02_partial_start_closes_both_owned_sockets(monkeypatch, stage):
    listener = LoopbackListener(
        lambda *_: HttpResponse(200), route=ROUTE, port=0, health=lambda: b"{}"
    )
    actual = listener.socket

    class FailStart:
        def __getattr__(self, name):
            if name == stage:
                raise OSError("authored HTTP socket failure")
            return getattr(actual, name)

    monkeypatch.setattr(listener, "socket", FailStart())
    with pytest.raises(OSError):
        listener.start()
    assert actual.fileno() == listener.reservation.fileno() == -1


def test_rr02_reservation_never_listens_or_transfers_data(monkeypatch):
    listener = LoopbackListener(
        lambda *_: HttpResponse(200), route=ROUTE, port=0, health=lambda: b"{}"
    )
    constructor = socket.socket
    created = []

    class Reservation:
        def __init__(self, actual):
            self.actual = actual

        def __getattr__(self, name):
            assert name not in {"listen", "accept", "send", "sendall", "recv", "recvfrom"}
            return getattr(self.actual, name)

    def sockets(*args, **kwargs):
        actual = constructor(*args, **kwargs)
        result = Reservation(actual) if not created else actual
        created.append(result)
        return result

    monkeypatch.setattr(socket, "socket", sockets)
    try:
        listener.start()
        assert listener.reservation is created[0]
        assert request(listener.address, ROUTE)[0] == 200
    finally:
        assert listener.close(2)["stopped"]
        assert listener.reservation.fileno() == -1


@pytest.mark.parametrize("surface", ["intake", "health"])
def test_rr03_expected_stop_refuses_http_without_failure(surface):
    def stopping(*_):
        raise ListenerStopping()

    listener = LoopbackListener(stopping, route=ROUTE, port=0, health=stopping)
    listener.start()
    try:
        route = ROUTE if surface == "intake" else listener.health_route
        method = "POST" if surface == "intake" else "GET"
        assert request(listener.address, route, method=method)[0] == 503
        assert listener.failure is False
    finally:
        assert listener.close(2)["stopped"]


@pytest.mark.parametrize("fault", ["stop", "runtime", "revoked", "config"])
def test_rr04_rr05_stop_during_drain_preserves_real_failures(tmp_path, monkeypatch, fault):
    fixture = Fixture(tmp_path)
    receiver = None
    entered = threading.Event()
    records = []
    try:
        service = fixture.load()
        receiver = InstalledReceiver(service, fixture.config)

        def in_flight(*_, check_authority):
            entered.set()
            assert receiver.closing.wait(3)
            if fault == "runtime":
                raise RuntimeError("private late failure")
            if fault == "revoked":
                service.core.revoke_session(service.handle)
            if fault == "config":
                fixture.config.write_bytes(fixture.config.read_bytes() + b"\n")
            check_authority()
            pytest.fail("Stopped work must not continue")

        monkeypatch.setattr(service.inbound, "run_once", in_flight)

        def emit(record):
            records.append(record)
            if record["event"] == "receiver_ready":
                assert entered.wait(3)
                signal.raise_signal(signal.SIGTERM)

        code = serve(service, fixture.config, emit, receiver=receiver)
        final = records[-1]
        assert final["cleanup"]["stopped"] and final["cleanup"]["ownership_released"]
        assert not receiver.worker.is_alive()
        if fault == "stop":
            assert code == 0 and final["ok"]
            assert final["failure"] is final["cleanup"]["failure"] is None
        else:
            assert code == 1 and not final["ok"]
            assert final["failure"] == final["cleanup"]["failure"] == "receiver_drain_failed"
        assert service.inbound.journal.deliveries(service.scope) == []
    finally:
        if receiver is not None:
            assert receiver.stop()["stopped"]
        fixture.close()


def test_rr06_late_http_failure_is_reported_after_owner_stop(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path)
    receiver = None
    entered, release = threading.Event(), threading.Event()
    try:
        service = fixture.load()
        receiver = InstalledReceiver(service, fixture.config)

        def fail(*_):
            entered.set()
            assert release.wait(3)
            raise RuntimeError("private late intake failure")

        monkeypatch.setattr(receiver, "receive", fail)
        receiver.start()
        client = threading.Thread(target=request, args=(receiver.http.address, ROUTE))
        client.start()
        assert entered.wait(3)
        receiver.closing.set()
        release.set()
        client.join(3)
        assert not client.is_alive()
        result = receiver.stop()
        assert result["stopped"] and result["ownership_released"]
        assert result["failure"] == "receiver_listener_failed"
    finally:
        release.set()
        if receiver is not None:
            assert receiver.stop()["stopped"]
        fixture.close()
