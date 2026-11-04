"""Authored readiness transitions; no claim about a past public endpoint failure."""

from .endpoint_authority import EndpointAuthority
from .endpoint_probe import EndpointProbe
from .test_endpoint_authority import authored_probe

pytest_plugins = ["tests.linear.live.test_endpoint_authority"]


def test_er01_opt_in_wait_retains_404_before_owned_success(endpoint, monkeypatch, tmp_path):
    original, receiver, tunnel, _, ledger, _, _, _ = endpoint
    authority = EndpointAuthority(
        receiver, tunnel, ledger, original.webhook_id, tmp_path / "readiness", readiness_attempts=3
    )
    from .endpoint_probe import EndpointNotReady

    calls = []

    def probe(self, selected, origin):
        calls.append(origin)
        actual = authored_probe(self, selected, origin)
        if len(calls) == 1:
            actual = pending_observation()
            raise EndpointNotReady(actual)
        return actual

    monkeypatch.setattr(EndpointProbe, "__call__", probe)
    reference = authority.admit()
    assert authority.verify(reference).url == calls[0] + receiver.route
    assert len(calls) == 2
    assert len(list(authority.root.glob("readiness-attempt-*.json"))) == 2


def pending_observation():
    return {
        "result": {
            "passed": False,
            "error_type": None,
            "observations": [
                {"label": "local_health", "status": 200, "run_matches": True},
                {"label": "public_health", "status": 404, "run_matches": False},
                {"label": "public_route", "status": 404, "body_empty": False},
            ],
        }
    }


def test_er02_default_stays_single_attempt_and_opt_in_exhausts(endpoint, monkeypatch, tmp_path):
    import json

    import pytest

    from .endpoint_probe import EndpointNotReady

    original, receiver, tunnel, _, ledger, _, _, _ = endpoint
    calls = []

    def pending(self, selected, origin):
        calls.append(origin)
        raise EndpointNotReady(pending_observation())

    monkeypatch.setattr(EndpointProbe, "__call__", pending)
    for maximum in [1, 3]:
        gate = EndpointAuthority(
            receiver,
            tunnel,
            ledger,
            original.webhook_id,
            tmp_path / str(maximum),
            readiness_attempts=maximum,
        )
        with pytest.raises(EndpointNotReady):
            gate.admit()
        assert not gate.receipts
        records = [json.loads(p.read_text()) for p in gate.root.glob("readiness-attempt-*.json")]
        assert sorted(r["attempt"] for r in records) == list(range(1, maximum + 1))
        assert all(r["not_ready"] for r in records)
    assert len(calls) == 4


def test_er03_invalid_attempt_bounds_refuse_before_probe(endpoint, tmp_path):
    import pytest

    from .ledger import DriverRefused

    original, receiver, tunnel, _, ledger, _, _, _ = endpoint
    for bound in [True, False, 0, -1, 4, 1.5, "3", None]:
        with pytest.raises(DriverRefused, match="readiness_bound"):
            EndpointAuthority(
                receiver,
                tunnel,
                ledger,
                original.webhook_id,
                tmp_path / "invalid",
                readiness_attempts=bound,
            )


def test_er04_only_public_404_with_owned_local_health_is_waitable():
    import copy

    from .endpoint_probe import public_not_ready

    good = pending_observation()["result"]
    assert public_not_ready(good)
    for index, field, value in [
        (0, "run_matches", False),
        (0, "status", 404),
        (1, "status", 200),
        (1, "status", 503),
        (2, "status", 405),
        (1, "label", "foreign_health"),
    ]:
        changed = copy.deepcopy(good)
        changed["observations"][index][field] = value
        assert not public_not_ready(changed)
    for field, value in [("error_type", "TimeoutError"), ("passed", True)]:
        changed = copy.deepcopy(good)
        changed[field] = value
        assert not public_not_ready(changed)


def test_er05_changed_identity_or_source_after_404_refuses_without_retry(
    endpoint, monkeypatch, tmp_path
):
    import pytest

    from .endpoint_probe import EndpointNotReady
    from .ledger import DriverRefused

    original, receiver, tunnel, _, ledger, _, _, _ = endpoint
    for target in ["source", "tunnel", "route"]:
        gate = EndpointAuthority(
            receiver,
            tunnel,
            ledger,
            original.webhook_id,
            tmp_path / target,
            readiness_attempts=3,
        )
        calls = []
        argv, route = tunnel.argv, receiver.route

        def changed(self, selected, origin):
            calls.append(origin)
            if target == "source":
                gate.source_pins[next(iter(gate.source_pins))] = "0" * 64
            elif target == "tunnel":
                tunnel.argv += ("foreign",)
            else:
                receiver.route += "/foreign"
            raise EndpointNotReady(pending_observation())

        monkeypatch.setattr(EndpointProbe, "__call__", changed)
        try:
            with pytest.raises(DriverRefused):
                gate.admit()
            assert len(calls) == 1 and not gate.receipts
            assert len(list(gate.root.glob("readiness-attempt-*.json"))) == 1
        finally:
            tunnel.argv, receiver.route = argv, route


def test_er06_authored_http_404_then_exact_installed_protocol_is_recorded(
    adapter, tmp_path, monkeypatch
):
    """Real loopback HTTP; public TLS and the historical ngrok cause are not exercised."""
    import json
    import threading
    from dataclasses import asdict
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from uuid import uuid4

    from . import endpoint_probe

    _, _, ledger, *_ = adapter
    instance = str(uuid4())
    rounds = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/local" or (rounds and self.path == "/public-health"):
                status, body = 200, json.dumps({"instance_id": instance}).encode()
            elif rounds and self.path == "/hook":
                status, body = 405, b""
            else:
                status, body = 404, b"authored endpoint not ready"
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    original = endpoint_probe._request

    def local_request(selected_origin, route, bound, label, expected):
        selected_route = {
            "local_health": "/local",
            "public_health": "/public-health",
            "public_route": "/hook",
        }[label]
        return original(origin, selected_route, bound, label, expected)

    monkeypatch.setattr(endpoint_probe, "_request", local_request)
    try:
        for attempt in [1, 2]:
            folder = tmp_path / str(attempt)
            folder.mkdir(mode=0o700)
            endpoint_probe.private_json(
                folder / "job.json",
                {
                    "ledger": str(ledger.path),
                    "run_id": ledger.run_id,
                    "scope": asdict(ledger.scope),
                    "limits": asdict(ledger.limits),
                    "origin": "https://authored.invalid",
                    "local_origin": origin,
                    "route": "/hook",
                    "health_route": "/health",
                    "receiver_kind": "installed_cli",
                    "instance_id": instance,
                },
            )
            code = endpoint_probe.worker(folder / "job.json")
            result = json.loads((folder / "result.json").read_text())
            assert code == (1 if attempt == 1 else 0)
            assert result["passed"] is (attempt == 2)
            assert endpoint_probe.public_not_ready(result) is (attempt == 1)
            assert [row["status"] for row in result["observations"]] == (
                [200, 404, 404] if attempt == 1 else [200, 200, 405]
            )
            rounds.append(result)
        assert len(ledger.summary()["requests"]) == 6
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()


def test_er07_failed_owned_probe_child_is_retained_and_classified(endpoint, monkeypatch, tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    import pytest

    from . import endpoint_probe
    from .endpoint_probe import EndpointNotReady

    _, receiver, _, _, ledger, _, _, _ = endpoint
    actual_popen = subprocess.Popen
    # This child writes declared protocol evidence, not provider observations.
    code = (
        "import json,pathlib,sys; p=pathlib.Path(sys.argv[1]).parent; "
        "(p/'result.json').write_text(sys.argv[2]); sys.exit(1)"
    )

    def authored_child(argv, **kwargs):
        if "tests.linear.live.endpoint_probe" not in argv:
            return actual_popen(argv, **kwargs)
        return actual_popen(
            [
                sys.executable,
                "-B",
                "-c",
                code,
                argv[-1],
                json.dumps(pending_observation()["result"]),
            ],
            **kwargs,
        )

    from .test_endpoint_authority import ACTUAL_PROBE

    monkeypatch.setattr(EndpointProbe, "__call__", ACTUAL_PROBE)
    monkeypatch.setattr(endpoint_probe.subprocess, "Popen", authored_child)
    probe = EndpointProbe(ledger, tmp_path / "owned-child")
    with pytest.raises(EndpointNotReady) as caught:
        probe(receiver, "https://authored.invalid")
    proof = caught.value.observation
    assert proof["ownership"]["reaped"]
    assert proof["ownership"]["returncode"] == 1
    assert proof["ownership"]["group"]["confirmed_stop"]
    assert proof["result"] == pending_observation()["result"]
    assert json.loads((Path(proof["evidence"]) / "result.json").read_text()) == proof["result"]


def test_er08_original_ledger_expiry_stops_wait_without_receipt(endpoint, monkeypatch, tmp_path):
    import pytest

    from .endpoint_probe import EndpointNotReady
    from .ledger import DriverRefused

    original, receiver, tunnel, _, ledger, _, _, _ = endpoint
    gate = EndpointAuthority(
        receiver,
        tunnel,
        ledger,
        original.webhook_id,
        tmp_path / "expired-wait",
        readiness_attempts=3,
    )
    calls = []
    original_clock = ledger.clock

    def probe(self, selected, origin):
        self.ledger.request("read")
        calls.append(origin)
        ledger.clock = lambda: ledger.limits.deadline
        raise EndpointNotReady(pending_observation())

    monkeypatch.setattr(EndpointProbe, "__call__", probe)
    try:
        with pytest.raises(DriverRefused, match="deadline_expired"):
            gate.admit()
        assert len(calls) == 1 and not gate.receipts
    finally:
        ledger.clock = original_clock


def test_er09_success_after_original_deadline_cannot_issue_receipt(endpoint, monkeypatch, tmp_path):
    import pytest

    from .ledger import DriverRefused

    original, receiver, tunnel, _, ledger, _, _, _ = endpoint
    gate = EndpointAuthority(
        receiver,
        tunnel,
        ledger,
        original.webhook_id,
        tmp_path / "late-success",
        readiness_attempts=3,
    )
    original_clock = ledger.clock

    def late(self, selected, origin):
        observed = authored_probe(self, selected, origin)
        ledger.clock = lambda: ledger.limits.deadline
        return observed

    monkeypatch.setattr(EndpointProbe, "__call__", late)
    try:
        with pytest.raises(DriverRefused, match="deadline_expired"):
            gate.admit()
        assert not gate.receipts
        assert len(list(gate.root.glob("readiness-attempt-*.json"))) == 1
    finally:
        ledger.clock = original_clock
