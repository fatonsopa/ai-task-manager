"""Authored loopback peers exercise actual upload failures and response bytes."""

import http.client
import json
import os
import socket
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from . import client

REPLIES = {
    "200": b"HTTP/1.0 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
    "503": b"HTTP/1.0 503 Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
    "500": b"HTTP/1.0 500 Error\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
    "missing": b"",
    "partial_header": b"HTTP/1.0 503 Unavailable\r\nContent-Length: 0\r\n",
    "partial_body": b"HTTP/1.0 503 Unavailable\r\nContent-Length: 5\r\n\r\nab",
    "conflicting_framing": (
        b"HTTP/1.1 503 Unavailable\r\nTransfer-Encoding: chunked\r\n"
        b"Content-Length: 0\r\nConnection: close\r\n\r\n2\r\nab\r\n0\r\n\r\n"
    ),
}


@contextmanager
def peer(wire, observation_path, *, early):
    listener = socket.socket()
    listener.settimeout(2)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    closed, release = threading.Event(), threading.Event()
    observations = {"errors": [], "body_bytes": 0, "closed": closed, "upload_errors": []}

    def serve():
        connection = None
        try:
            connection, _ = listener.accept()
            connection.settimeout(2)
            if early:
                prefix = connection.recv(1)
                assert prefix == b"P"
                unread = connection.recv(1, socket.MSG_PEEK)
                assert unread
                observations["request_bytes_consumed"] = len(prefix)
                observations["unread_request_bytes_at_least"] = len(unread)
                observations["unread_observed_at"] = time.monotonic()
                body = b""
            else:
                request = bytearray()
                while b"\r\n\r\n" not in request:
                    value = connection.recv(4096)
                    assert value and len(request) + len(value) <= 16384
                    request.extend(value)
                head, body = bytes(request).split(b"\r\n\r\n", 1)
                observations["headers"] = [
                    line.decode("latin1").split(":", 1) for line in head.split(b"\r\n")[1:]
                ]
                while len(body) < 565:
                    value = connection.recv(565 - len(body))
                    assert value
                    body += value
            observations["body_bytes"] = len(body)
            connection.sendall(wire)
        except BaseException as error:
            observations["errors"].append(type(error).__name__)
        finally:
            if connection is not None:
                connection.close()
                observations["peer_descriptor_after_close"] = connection.fileno()
                observations["peer_closed_at"] = time.monotonic()
            closed.set()
            release.wait(2)

    thread = threading.Thread(target=serve, name="owned-client-contract-peer")
    thread.start()
    server = SimpleNamespace(address=listener.getsockname(), route="/owned-client-contract")
    try:
        yield server, observations
    finally:
        release.set()
        listener.close()
        thread.join(3)
        record = {
            **{key: value for key, value in observations.items() if key != "closed"},
            "peer_closed_observed": closed.is_set(),
            "thread_alive_after_join": thread.is_alive(),
            "listener_descriptor_after_close": listener.fileno(),
            "pid": os.getpid(),
        }
        observation_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        assert not thread.is_alive()
        assert closed.is_set() and not observations["errors"]


def pace_actual_upload(monkeypatch, observations):
    original = http.client.HTTPConnection
    errors = []

    class PacedConnection(original):
        sends = 0

        def send(self, value):
            self.sends += 1
            if self.sends == 2:
                assert observations["closed"].wait(1)
                assert observations["unread_request_bytes_at_least"] >= 1
                time.sleep(0.05)
                observations["body_send_started_at"] = time.monotonic()
                assert observations["body_send_started_at"] >= observations["peer_closed_at"]
            try:
                return super().send(value)
            except BrokenPipeError as error:
                errors.append(error)
                observations["upload_errors"].append(
                    {"type": type(error).__name__, "errno": error.errno}
                )
                raise

    monkeypatch.setattr(client.http.client, "HTTPConnection", PacedConnection)
    return errors


def test_client_normal_complete_response(tmp_path):
    with peer(REPLIES["200"], tmp_path / "client-observation.json", early=False) as (
        server,
        observations,
    ):
        result = client.send_request(server, body=b"x" * 565)
        observations["actual_returned_status"] = result[0]
        assert result == (200, b"")
    assert observations["body_bytes"] == 565


def test_captured_headers_do_not_gain_implicit_duplicates(tmp_path):
    signed = "a" * 64
    headers = [
        ("Host", "captured.example"),
        ("Accept-Encoding", "gzip"),
        ("Linear-Signature", signed),
    ]
    with peer(REPLIES["200"], tmp_path / "captured-headers.json", early=False) as (server, seen):
        assert client.send_request(server, headers, b"x" * 565) == (200, b"")
    actual = [(name.lower(), value.strip()) for name, value in seen["headers"]]
    assert [v for k, v in actual if k == "host"] == ["captured.example"]
    assert [v for k, v in actual if k == "accept-encoding"] == ["gzip"]
    assert [v for k, v in actual if k == "linear-signature"] == [signed]
    assert seen["body_bytes"] == 565


def test_explicit_duplicate_headers_remain_visible_to_receiver(tmp_path):
    headers = [("Accept-Encoding", "gzip"), ("accept-encoding", "br")]
    with peer(REPLIES["200"], tmp_path / "explicit-duplicates.json", early=False) as (server, seen):
        assert client.send_request(server, headers, b"x" * 565) == (200, b"")
    actual = [(name.lower(), value.strip()) for name, value in seen["headers"]]
    assert [v for k, v in actual if k == "accept-encoding"] == ["gzip", "br"]


@pytest.mark.parametrize("status", [503, 200, 500])
def test_client_early_complete_response_recovers_actual_write_failure(
    monkeypatch, tmp_path, status
):
    with peer(REPLIES[str(status)], tmp_path / "client-observation.json", early=True) as (
        server,
        observations,
    ):
        errors = pace_actual_upload(monkeypatch, observations)
        actual, body = client.send_request(server, body=b"x" * 565)
        observations["actual_returned_status"] = actual
        assert errors and type(errors[0]) is BrokenPipeError
        assert actual == status and body == b""
        assert (actual == 503) is (status == 503)
    assert observations["body_bytes"] == 0


@pytest.mark.parametrize("kind", ["missing", "partial_header", "partial_body"])
def test_client_failed_response_preserves_actual_write_failure(monkeypatch, tmp_path, kind):
    with peer(REPLIES[kind], tmp_path / "client-observation.json", early=True) as (
        server,
        observations,
    ):
        errors = pace_actual_upload(monkeypatch, observations)
        with pytest.raises(BrokenPipeError) as caught:
            client.send_request(server, body=b"x" * 565)
        observations["original_upload_error_preserved"] = (
            len(errors) == 1 and caught.value is errors[0]
        )
        observations["response_error_type"] = type(caught.value.__cause__).__name__
        assert len(errors) == 1 and caught.value is errors[0]
        assert caught.value.__cause__ is not None
    assert observations["body_bytes"] == 0


@pytest.mark.parametrize("kind", ["partial_header", "partial_body"])
def test_client_incomplete_response_without_upload_failure(tmp_path, kind):
    with peer(REPLIES[kind], tmp_path / "client-observation.json", early=False) as (
        server,
        observations,
    ):
        with pytest.raises((client.IncompleteResponse, http.client.IncompleteRead)) as caught:
            client.send_request(server, body=b"x" * 565)
        observations["response_error_type"] = type(caught.value).__name__
    assert observations["body_bytes"] == 565


def test_client_unsupported_transfer_framing_refused(tmp_path):
    with peer(
        REPLIES["conflicting_framing"], tmp_path / "client-observation.json", early=False
    ) as (server, observations):
        with pytest.raises(
            client.IncompleteResponse, match="unsupported_transfer_encoding"
        ) as caught:
            client.send_request(server, body=b"x" * 565)
        observations["response_error_type"] = type(caught.value).__name__
        observations["unsupported_transfer_encoding_refused"] = True
    assert observations["body_bytes"] == 565
