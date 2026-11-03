"""Versioned authored controls; these do not count as installed or live acceptance."""

import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from ai_task_manager.config.files import read_private, write_private
from ai_task_manager.domains.work.errors import WorkError
from tests.linear.helpers import WEBHOOK

from . import endpoint_probe
from .endpoint_authority import EndpointAuthority
from .installed_endpoint import InstalledReceiverProcess
from .installed_endpoint_fixture import InstalledEndpointFixture
from .ledger import DriverRefused
from .test_actual_adapter import ready

pytest_plugins = ["tests.linear.live.test_actual_adapter"]


class AuthoredTunnel:
    """Declared proxy observation port: no actual tunnel or public TLS claim."""

    def observe(self, receiver):
        return {"origin": "https://authored.invalid", "identity": "authored-tunnel"}


def local_proxy(probe, receiver, origin):
    observed = receiver.observe()
    rows = [
        endpoint_probe._request(
            f"http://127.0.0.1:{receiver.address[1]}",
            route,
            probe.ledger,
            label,
            {"instance_id": observed["instance_id"]},
        )
        for route, label in (
            (receiver.health_route, "local_health"),
            (receiver.health_route, "public_health"),
            (receiver.route, "public_route"),
        )
    ]
    return {
        "result": {
            "passed": all(r["status"] == 200 and r["run_matches"] for r in rows[:2])
            and rows[2]["status"] == 405
            and rows[2]["body_empty"],
            "observations": rows,
        },
        "kind": "authored_loopback_proxy_no_public_tls",
    }


@pytest.fixture
def installed(adapter, tmp_path, monkeypatch):
    ready(adapter)
    admin, _, ledger, *_ = adapter
    fixture = InstalledEndpointFixture(tmp_path / "installed-protocol", ledger)
    monkeypatch.setattr(endpoint_probe.EndpointProbe, "__call__", local_proxy)
    try:
        yield fixture, admin, ledger
    finally:
        fixture.close()


def authority(fixture, ledger, server):
    return EndpointAuthority(server, AuthoredTunnel(), ledger, WEBHOOK, fixture.root / "admission")


def test_ip01_owned_readiness_and_existing_authority_admit_exact_installed_route(installed):
    fixture, admin, ledger = installed
    server = fixture.start()
    gate = authority(fixture, ledger, server)
    reference = gate.admit()
    receipt = gate.verify(reference)
    assert receipt.receiver_kind == "installed_cli"
    assert receipt.url == "https://authored.invalid/hooks/" + ledger.run_id
    admin.endpoint_authority = gate
    admin.graphql.actions.reserve_receiver(WEBHOOK)
    admin.create_webhook("installed-hook", WEBHOOK, reference, lambda: "x" * 32)
    observed = admin.read("webhook", WEBHOOK, expected_url=receipt.url)
    assert observed.state == "disabled"
    assert not fixture.service.core.list_tasks(fixture.service.handle)


@pytest.mark.parametrize(
    "target",
    [
        "argv",
        "source",
        "config",
        "session",
        "secret",
        "credential",
        "instance",
        "enrollment",
        "core",
        "stopped",
        "package_addition",
    ],
)
def test_ip02_changed_binding_refuses_before_probe_without_effects(installed, target):
    fixture, _, ledger = installed
    server = fixture.start()
    gate = authority(fixture, ledger, server)
    if target == "argv":
        fixture.child.args = [*fixture.child.args, "wrong"]
    elif target == "source":
        fixture.entrypoint.write_text(fixture.entrypoint.read_text() + "\n# changed\n")
    elif target in {"config", "session", "secret", "credential"}:
        path = {
            "config": fixture.config,
            "session": fixture.root / "session.json",
            "secret": fixture.root / "secret.json",
            "credential": fixture.root / "credential.env",
        }[target]
        path.write_text(path.read_text() + "\n")
    elif target == "instance":
        server.ready["instance_id"] = str(uuid4())
    elif target == "enrollment":
        fixture.service.decisions.observations.revoke()
    elif target == "core":
        fixture.service.inbound.core = object()
    elif target == "stopped":
        assert fixture.group.stop().confirmed
    else:
        (Path(fixture.package["runtime_root"]) / "unreviewed.py").write_text("# addition")
    before = ledger.summary()
    with pytest.raises((DriverRefused, WorkError, OSError)):
        gate.admit()
    assert ledger.summary() == before and not gate.receipts
    assert not fixture.service.core.list_tasks(fixture.service.handle)


def test_ip03_wrong_actual_child_command_refuses_before_readiness(installed):
    fixture, _, ledger = installed
    before = ledger.summary()
    with pytest.raises(DriverRefused, match="command_changed"):
        fixture.start(suffix=("foreign-command",))
    assert ledger.summary() == before


@pytest.mark.parametrize("target", ["wrong_instance", "refused_health", "harness_route_status"])
def test_ip04_observed_protocol_refusal_cannot_issue_authority(installed, monkeypatch, target):
    fixture, _, ledger = installed
    server = fixture.start()
    gate = authority(fixture, ledger, server)
    original = endpoint_probe._request

    def changed(origin, route, bound, label, identity):
        value = original(
            origin,
            route,
            bound,
            label,
            {"instance_id": str(uuid4())} if target == "wrong_instance" else identity,
        )
        if target == "refused_health" and label.endswith("health"):
            value["status"] = 503
        if target == "harness_route_status" and label == "public_route":
            value["status"] = 404
        return value

    monkeypatch.setattr(endpoint_probe, "_request", changed)
    with pytest.raises(DriverRefused, match="observation_changed"):
        gate.admit()
    assert not gate.receipts and not ledger.summary()["cleanup_actions"]


def test_ip05_receipt_cannot_change_receiver_type(installed):
    fixture, admin, ledger = installed
    server = fixture.start()
    gate = authority(fixture, ledger, server)
    reference = gate.admit()
    admin.endpoint_authority = gate
    gate.receipts[reference] = replace(gate.verify(reference), receiver_kind="fixture")
    before = ledger.summary()
    with pytest.raises(DriverRefused, match="admission_mismatch"):
        admin._endpoint(reference, WEBHOOK)
    assert ledger.summary() == before


def test_ip06_readiness_is_owned_stream_not_caller_or_wrong_scope(installed):
    fixture, _, ledger = installed
    before = ledger.summary()
    with pytest.raises(DriverRefused, match="owned_inputs_required"):
        InstalledReceiverProcess(object(), fixture.service, fixture.config, ledger, fixture.package)
    config = read_private(fixture.receiver)
    config["route"] = "/hooks/" + str(uuid4())
    write_private(fixture.receiver, config, replace=True)
    with pytest.raises(DriverRefused, match="route_mismatch"):
        fixture.start()
    assert ledger.summary() == before


@pytest.mark.parametrize(
    "kind,health,status,passed",
    [
        ("installed_cli", "instance_id", 405, True),
        ("installed_cli", "instance_id", 404, False),
        ("installed_cli", "run_id", 405, False),
        ("fixture", "run_id", 404, True),
        ("fixture", "run_id", 405, False),
        ("unknown", "run_id", 405, False),
    ],
)
def test_ip07_worker_protocol_selection_is_exact(
    adapter, tmp_path, monkeypatch, kind, health, status, passed
):
    from dataclasses import asdict

    _, _, ledger, *_ = adapter
    instance = str(uuid4())
    job = {
        "ledger": str(ledger.path),
        "run_id": ledger.run_id,
        "scope": asdict(ledger.scope),
        "limits": asdict(ledger.limits),
        "origin": "https://authored.invalid",
        "local_origin": "http://127.0.0.1:12345",
        "route": "/hooks/" + ledger.run_id,
        "health_route": "/hooks/" + ledger.run_id + "/health",
        "receiver_kind": kind,
        "instance_id": instance,
    }
    folder = tmp_path / "probe-job"
    folder.mkdir(mode=0o700)
    endpoint_probe.private_json(folder / "job.json", job)

    def observed(origin, route, bound, label, expected):
        identity = {health: instance if health == "instance_id" else ledger.run_id}
        return {
            "status": status if label == "public_route" else 200,
            "run_matches": identity == expected,
            "body_empty": label == "public_route",
        }

    monkeypatch.setattr(endpoint_probe, "_request", observed)
    result = endpoint_probe.worker(folder / "job.json")
    assert result == (0 if passed else 1)
    assert json.loads((folder / "result.json").read_text())["passed"] is passed
