"""Declared native/operator/HTTP contracts; never live account or human acceptance."""

import hashlib
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from ai_task_manager.adapters.hosts.bridge.auth_probe import claude_account_hash
from ai_task_manager.adapters.hosts.bridge.human import OperatorChannel
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.adapters.workflow_control.identity import file_binding
from ai_task_manager.config.files import digest, write_private
from ai_task_manager.config.service import load_service
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.native import NativeManifest, lead_startup, resolve
from ai_task_manager.installation.native.manifest import installation_intent
from ai_task_manager.installation.native.normal import NormalAuthoritySource, enroll
from ai_task_manager.installation.native.store import NativeStore
from ai_task_manager.installation.sessions import setup
from ai_task_manager.presentation.cli.main import main
from tests.native_installation.helpers import bundle, refresh_digest
from tests.native_installation.test_normal import AuthoredRestrictions
from tests.session_observations.helpers import OwnedProvider
from tests.session_observations.test_observations import _confirmed_task_for_retry


class DeclaredRestrictions(AuthoredRestrictions):
    def require_native_conformance(self, manifest, policy, binding, journal):
        # This only admits the declared executable protocol fixture below.
        policy.check()
        return "declared-not-live"


@pytest.fixture
def lead(tmp_path, monkeypatch):
    p = OwnedProvider(tmp_path / "provider")
    master, slave = os.openpty()
    try:
        setup(
            p.config,
            logical_session="lead-session",
            actions=("enqueue",),
            deadline=p.deadline,
            operation_key="lead-setup",
            components=p.components(),
        )
        monkeypatch.setattr(HttpTransport, "ENDPOINT", p.endpoint)
        service = load_service(p.config)
        _confirmed_task_for_retry(service)
        event = service.core.notifications.list_events(service.handle)[0]
        b = bundle(tmp_path / "native", "claude", "claude-opus-5-5", task_id=event.task_id)
        executable = b.root / "declared-native"
        executable.write_text(
            "#!"
            + sys.executable
            + "\n"
            + Path(__file__).with_name("fixture_lead_native.py").read_text()
        )
        executable.chmod(0o700)
        store = NativeStore.create(b.root / "normal.sqlite")
        spec = json.loads(b.installation.read_text())
        spec.update(
            role="lead",
            scope=asdict(service.scope),
            deadline=time.time() + 45,
            lead={
                "session_id": service.session_id,
                "event_id": event.id,
                "journal": str(b.root / "host.sqlite"),
                "key": str(b.root / "host.key"),
                "directory": str(b.root / "process"),
                "cwd": str(b.root),
            },
        )
        write_private(b.installation, spec, replace=True)
        b.data.update(
            authority_store=store.pointer,
            installation=installation_intent(b.installation),
            config=file_binding(p.config),
            executable=str(executable),
            executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
            account_hash=claude_account_hash(
                {
                    "email": "fixture@example.invalid",
                    "orgId": "authored-org",
                    "userId": "authored-user",
                }
            ),
        )
        write_private(b.manifest, b.data, replace=True)
        refresh_digest(b.installation, b.manifest, b.data)
        manifest = NativeManifest(b.manifest, p.config, b.installation)
        enroll(
            store,
            manifest,
            {"host": 35, "relay": 35, "auth": 5, "neutral": 8, "assignment": 8},
            identity_refreshes=(),
            conformance_ref="declared-contract",
            role="lead",
        )
        original_source = NormalAuthoritySource
        monkeypatch.setattr(
            lead_startup,
            "NormalAuthoritySource",
            lambda m, **kw: original_source(m, conformance=DeclaredRestrictions(), **kw),
        )

        def reply(self, request, timeout):
            return {
                "request_id": request["request_id"],
                "challenge": request["challenge"],
                "accepted": True,
                "instruction_hash": digest(request["instruction"]),
            }, os.getpid()

        monkeypatch.setattr(OperatorChannel, "_exchange", reply)
        original_open = os.open
        monkeypatch.setattr(
            lead_startup.os,
            "open",
            lambda path, *a, **kw: (
                os.dup(slave) if path == "/dev/tty" else original_open(path, *a, **kw)
            ),
        )
        yield p, b, store, manifest, service, event
    finally:
        os.close(master)
        os.close(slave)
        p.close()


def test_LC01_installed_entrypoint_native_operator_receipts_and_restart(lead, capsys, monkeypatch):
    p, b, store, manifest, service, event = lead
    from ai_task_manager.application.service.facade import ApplicationService

    original_call = ApplicationService.call

    def observed_call(self, operation, data):
        result = original_call(self, operation, data)
        if not result.get("ok"):
            failures.append(repr((operation, result)))
        return result

    monkeypatch.setattr(ApplicationService, "call", observed_call)
    original_report = lead_startup.report
    failures = []

    def observed(*args):
        try:
            return original_report(*args)
        except BaseException:
            import traceback

            failures.append(traceback.format_exc())
            raise

    monkeypatch.setattr(lead_startup, "report", observed)
    result = main(
        [
            "--config",
            str(p.config),
            "--installation",
            str(b.installation),
            "--json",
            "lead",
            "report",
        ]
    )
    output = capsys.readouterr()
    assert result == 0, "\n".join([output.err, *failures])
    assert json.loads(output.out)["data"]["event_id"] == event.id
    with service.core.context.db.transaction() as conn:
        receipts = conn.execute(
            "SELECT stage FROM receipts WHERE event_id=?", (event.id,)
        ).fetchall()
    assert {r[0] for r in receipts} == {"queued", "delivered", "ai_acknowledged", "user_reported"}
    # A fresh default service consumes only exact already-completed proofs after stop.
    from ai_task_manager.adapters.hosts.bridge.journal import HostJournal

    spec = json.loads(b.installation.read_text())
    journal = HostJournal.open_existing(spec["lead"]["journal"], spec["lead"]["key"])
    cold = load_service(p.config)
    for row in journal._history("notification"):
        payload = row["payload"]
        assert cold.call(
            "event_acknowledge",
            {
                "event_id": event.id,
                "stage": payload["stage"],
                "observation_ref": row["ref"],
                "operation_key": f"host:{event.id}:{payload['stage']}",
            },
        )["ok"]
    with service.core.context.db.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
        assert (
            conn.execute("SELECT count(*) FROM receipts WHERE event_id=?", (event.id,)).fetchone()[
                0
            ]
            == 4
        )
    before = store.permission(manifest.data["authorization_ref"])
    assert (
        main(
            [
                "--config",
                str(p.config),
                "--installation",
                str(b.installation),
                "--json",
                "lead",
                "report",
            ]
        )
        != 0
    )
    assert store.permission(manifest.data["authorization_ref"]) == before
    from ai_task_manager.installation.native.controller import NormalInvocationController

    calls = NormalInvocationController(store, before).snapshot()["calls"]
    assert len(calls) == 5
    assert all(row["process_stopped"] for row in calls)


def test_LC02_worker_resolver_cannot_consume_lead_permission(lead):
    p, b, store, manifest, service, event = lead
    source = NormalAuthoritySource(manifest, role="lead", conformance=DeclaredRestrictions())
    with pytest.raises(AccessDenied, match="Worker workflow"):
        resolve(p.config, b.installation, authority_source=source)
    with pytest.raises(AccessDenied, match="permission changed"):
        NormalAuthoritySource(manifest, conformance=DeclaredRestrictions()).resolve(
            manifest.data["authorization_ref"], manifest
        )


@pytest.mark.parametrize("change", ["revoked", "foreign", "expired", "source", "provider-revoked"])
def test_LC03_current_permission_refuses_before_native_launch(lead, change):
    p, b, store, manifest, service, event = lead
    ref = manifest.data["authorization_ref"]
    if change == "revoked":
        store.revoke(ref)
    elif change == "foreign":
        p.actual = type(p.expected)(
            "foreign",
            p.expected.workspace_id,
            p.expected.team_id,
            p.expected.team_key,
            p.expected.project_id,
        )
    elif change == "provider-revoked":
        service.decisions.observations.revoke()
    elif change == "source":
        manifest.executable.write_text("changed")
    else:
        import datetime

        original = lead_startup.NormalAuthoritySource
        lead_startup.NormalAuthoritySource = lambda m, **kw: NormalAuthoritySource(
            m,
            role="lead",
            conformance=DeclaredRestrictions(),
            clock=lambda: datetime.datetime.fromtimestamp(time.time() + 100, datetime.UTC),
        )
    try:
        with pytest.raises(Exception):
            lead_startup.report(p.config, b.installation)
        assert not Path(json.loads(b.installation.read_text())["lead"]["directory"]).exists()
    finally:
        if change == "expired":
            lead_startup.NormalAuthoritySource = original


def test_LC04_role_cannot_be_inferred_by_enrollment(lead):
    p, b, store, manifest, service, event = lead
    with pytest.raises(AccessDenied, match="role differs"):
        enroll(
            store,
            manifest,
            {"host": 35, "relay": 35, "auth": 5, "neutral": 8, "assignment": 8},
            identity_refreshes=(),
            conformance_ref="declared-contract",
        )


def test_LC05_cli_rejects_model_input_and_session_override(lead, capsys):
    p, b, store, manifest, service, event = lead
    for option in (("--input", "-"), ("--session-file", str(p.root / "session.json"))):
        assert (
            main(
                [
                    "--config",
                    str(p.config),
                    "--installation",
                    str(b.installation),
                    "--json",
                    *option,
                    "lead",
                    "report",
                ]
            )
            == 2
        )
    assert not Path(json.loads(b.installation.read_text())["lead"]["directory"]).exists()


def test_LC06_unapproved_operator_stops_native_and_never_connects(lead, monkeypatch):
    p, b, store, manifest, service, event = lead

    def reject(*args, **kwargs):
        raise AccessDenied("Declared independent operator refusal")

    monkeypatch.setattr(OperatorChannel, "capture_enrollment", reject)
    with pytest.raises(AccessDenied, match="operator refusal"):
        lead_startup.report(p.config, b.installation)
    from ai_task_manager.installation.native.controller import NormalInvocationController

    calls = NormalInvocationController(
        store, store.permission(manifest.data["authorization_ref"])
    ).snapshot()["calls"]
    assert len(calls) == 4 and all(row["process_stopped"] for row in calls)
    with service.core.context.db.transaction() as conn:
        assert [
            row[0]
            for row in conn.execute("SELECT stage FROM receipts WHERE event_id=?", (event.id,))
        ] == ["queued"]


def test_LC07_default_source_has_no_authored_conformance_fallback(lead, monkeypatch):
    p, b, store, manifest, service, event = lead
    monkeypatch.setattr(lead_startup, "NormalAuthoritySource", NormalAuthoritySource)
    with pytest.raises(Exception):
        lead_startup.report(p.config, b.installation)
    assert not Path(json.loads(b.installation.read_text())["lead"]["directory"]).exists()


def test_LC08_connection_close_failure_still_stops_native(lead, monkeypatch):
    p, b, store, manifest, service, event = lead
    from ai_task_manager.installation.native.lead import LeadConnection

    original_close = LeadConnection.close

    def failed_close(self):
        original_close(self)
        raise RuntimeError("Declared close failure")

    monkeypatch.setattr(LeadConnection, "close", failed_close)
    with pytest.raises(RuntimeError, match="Declared close failure"):
        lead_startup.report(p.config, b.installation)
    from ai_task_manager.installation.native.controller import NormalInvocationController

    calls = NormalInvocationController(
        store, store.permission(manifest.data["authorization_ref"])
    ).snapshot()["calls"]
    assert len(calls) == 5 and all(row["process_stopped"] for row in calls)
