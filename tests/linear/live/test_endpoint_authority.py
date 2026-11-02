"""Owned local process/socket controls; public TLS is explicitly authored here."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from ai_task_manager.adapters.process.groups import OwnedProcessGroup
from tests.linear.helpers import WEBHOOK

from .client import send_request
from .endpoint_authority import EndpointAuthority, OwnedTunnel
from .endpoint_probe import EndpointProbe
from .ledger import DriverRefused
from .receiver import BoundedReceiver
from .test_actual_adapter import ready

pytest_plugins = ["tests.linear.live.test_actual_adapter", "tests.linear.live.test_driver_contract"]
ACTUAL_PROBE = EndpointProbe.__call__


def authored_probe(probe, receiver, origin):
    facts = []
    for label, route in (
        ("local_health", receiver.health_route),
        ("public_health", receiver.health_route),
        ("public_route", receiver.route),
    ):
        probe.ledger.request("read")
        status, body = send_request(receiver, method="GET", route=route)
        facts.append(
            {"label": label, "status": status, "body_sha256": hashlib.sha256(body).hexdigest()}
        )
        assert status == (404 if label == "public_route" else 200)
    return {"result": {"passed": True, "observations": facts}, "kind": "authored_local_proxy"}


@pytest.fixture
def endpoint(adapter, rig, monkeypatch, tmp_path, request):
    admin, _, ledger, *_ = adapter
    if getattr(request, "param", True):
        ready(adapter)
    server = BoundedReceiver(rig.receiver, ledger)
    server.start()
    assert server.loop_entered.wait(1)
    log = tmp_path / "owned-tunnel.log"
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    argv = [
        sys.executable,
        "-u",
        "-c",
        "import time; print('https://authored-owned.trycloudflare.com', flush=True); "
        "time.sleep(15)",
        "--url",
        f"http://127.0.0.1:{server.address[1]}",
    ]
    child = group = None
    closed = {}
    try:
        with os.fdopen(descriptor, "wb") as output:
            child = subprocess.Popen(argv, stdout=output, stderr=output, start_new_session=True)
        group = OwnedProcessGroup(child)
        group.capture()
        (tmp_path / "endpoint-owned-child.json").write_text(
            json.dumps({"pid": child.pid, "group": child.pid, "argv": argv})
        )
        until = time.monotonic() + 1
        while not log.read_bytes():
            assert time.monotonic() < until
            time.sleep(0.01)
        tunnel = OwnedTunnel(
            group,
            argv,
            hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest(),
            log,
        )
        monkeypatch.setattr(EndpointProbe, "__call__", authored_probe)
        now = [time.time()]
        authority = EndpointAuthority(
            server, tunnel, ledger, WEBHOOK, tmp_path / "endpoint", clock=lambda: now[0]
        )
        yield authority, server, tunnel, admin, ledger, log, now, closed
    finally:
        closed["receiver"] = server.stop(2)
        if group is not None and child.returncode is None:
            observation = group.stop()
            closed["group"] = group.evidence(observation, orphaned=False)
            if observation.confirmed:
                group.reap_exited_leader()
        closed["pid"] = child.pid if child else None
        closed["returncode"] = child.returncode if child else None
        closed["reaped"] = child is not None and child.returncode is not None
        (tmp_path / "endpoint-owned-stop.json").write_text(
            json.dumps(closed, indent=2, sort_keys=True) + "\n"
        )
        assert closed["receiver"]["stopped"] and closed["reaped"]
        assert closed["group"]["confirmed_stop"]


def test_ep01_owned_objects_admit_only_endpoint_reference(endpoint):
    authority, server, _, admin, ledger, _, _, _ = endpoint
    reference = authority.admit()
    receipt = authority.verify(reference)
    assert receipt.url == "https://authored-owned.trycloudflare.com" + server.route
    assert receipt.run_id == ledger.run_id and receipt.webhook_id == WEBHOOK
    admin.endpoint_authority = authority
    admin.graphql.actions.reserve_receiver(WEBHOOK)
    admin.create_webhook("hook", WEBHOOK, reference, lambda: "x" * 32)
    assert ledger.resource(WEBHOOK)["state"] == "disabled"


@pytest.mark.parametrize("target", ["stopped", "route", "source"])
def test_ep02_missing_receiver_condition_refuses_before_probe(endpoint, target):
    authority, server, _, _, ledger, _, _, _ = endpoint
    if target == "stopped":
        assert server.stop(2)["stopped"]
    elif target == "route":
        server.route += "/other"
    else:
        authority.source_pins[next(iter(authority.source_pins))] = "0" * 64
    before = ledger.summary()
    with pytest.raises(DriverRefused, match="receiver_not_running"):
        authority.observe_transport()
    assert ledger.summary() == before and not authority.receipts


@pytest.mark.parametrize(
    "target", ["argv", "source", "origin_missing", "ambiguous", "log_mode", "log_missing"]
)
def test_ep03_tunnel_identity_or_origin_refuses_before_probe(endpoint, target):
    authority, _, tunnel, _, ledger, log, _, _ = endpoint
    if target == "argv":
        tunnel.argv += ("other",)
    elif target == "source":
        tunnel.executable_sha256 = "0" * 64
    elif target == "origin_missing":
        log.write_text("no public origin\n")
    elif target == "ambiguous":
        log.write_text("https://one.trycloudflare.com\nhttps://two.trycloudflare.com\n")
    elif target == "log_mode":
        log.chmod(0o644)
    else:
        log.unlink()
    before = ledger.summary()
    with pytest.raises(DriverRefused):
        authority.observe_transport()
    assert ledger.summary() == before and not authority.receipts


@pytest.mark.parametrize("target", ["failed_probe", "adapter_during_probe", "tunnel_during_probe"])
def test_ep04_probe_or_owner_change_cannot_issue_reference(endpoint, monkeypatch, target):
    authority, server, tunnel, _, _, _, _, _ = endpoint

    def changed(probe, receiver, origin):
        result = authored_probe(probe, receiver, origin)
        if target == "failed_probe":
            result["result"]["passed"] = False
        elif target == "adapter_during_probe":
            server.adapter = object()
        else:
            tunnel.argv += ("other",)
        return result

    monkeypatch.setattr(EndpointProbe, "__call__", changed)
    with pytest.raises(DriverRefused):
        authority.admit()
    assert not authority.receipts


@pytest.mark.parametrize("target", ["expired", "unknown", "adapter", "webhook", "tunnel"])
def test_ep05_changed_context_invalidates_existing_reference(endpoint, target):
    authority, server, tunnel, _, _, _, now, _ = endpoint
    reference = authority.admit()
    if target == "expired":
        now[0] += 31
    elif target == "unknown":
        reference = "endpoint:" + str(uuid4())
    elif target == "adapter":
        server.adapter = object()
    elif target == "webhook":
        authority.webhook_id = str(uuid4())
    else:
        tunnel.argv += ("other",)
    with pytest.raises(DriverRefused):
        authority.verify(reference)


@pytest.mark.parametrize("endpoint", [False], indirect=True)
def test_ep06_preproject_transport_observation_grants_no_webhook_authority(endpoint):
    authority, _, _, _, ledger, _, _, _ = endpoint
    observed = authority.observe_transport()
    assert observed["authority"] == "transport_observation_only"
    assert not ledger.summary()["resources"] and not authority.receipts
    with pytest.raises(DriverRefused):
        authority.admit()


def test_ep07_actual_probe_child_timeout_is_stopped_and_accounted(endpoint, monkeypatch, tmp_path):
    authority, server, _, _, ledger, _, _, _ = endpoint
    monkeypatch.setattr(EndpointProbe, "__call__", ACTUAL_PROBE)
    probe = EndpointProbe(ledger, tmp_path / "actual-probe", seconds=1)
    before = ledger.summary()
    connection = sqlite3.connect(ledger.path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(DriverRefused, match="probe_not_confirmed"):
            probe(server, "https://authored-owned.trycloudflare.com")
    finally:
        connection.rollback()
        connection.close()
    assert ledger.summary() == before
    records = list((tmp_path / "actual-probe").glob("*/ownership.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    stage = json.loads(records[0].with_name("stage.json").read_text())
    assert stage == {"stage": "ledger_open_begin", "pid": record["pid"]}
    assert record["reaped"] and record["group"]["confirmed_stop"]
    assert record["supervision"]["outcome"] == "interrupted"
    assert record["supervision"]["reason"] == "timeout"


def test_ep08_constructor_creates_private_directory_under_public_umask(endpoint, tmp_path):
    _, server, tunnel, _, ledger, _, _, _ = endpoint
    previous = os.umask(0o022)
    try:
        authority = EndpointAuthority(server, tunnel, ledger, WEBHOOK, tmp_path / "new-private")
    finally:
        os.umask(previous)
    assert authority.root.stat().st_mode & 0o777 == 0o700
    assert authority.probe.root.stat().st_mode & 0o777 == 0o700
