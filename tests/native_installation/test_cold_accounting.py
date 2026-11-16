"""Cold process-stop accounting preserves revoked grants and uncertain outcomes."""

import json
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.domains.work.errors import EvidenceRequired
from ai_task_manager.installation.workflows.cold import ColdRecovery
from tests.native_installation.test_normal import claim, reservation, start
from tests.native_installation.test_normal import normal as normal


@pytest.mark.parametrize("auth_unknown", [False, True])
def test_cold_accounting_requires_all_auth_stops_before_releasing_host(normal, auth_unknown):
    bundle, store, _, _, permission = normal
    controller = permission.controller
    controller.reserve("host", **reservation(permission))
    claim(controller, "host")
    start(controller, "host")
    if auth_unknown:
        controller.reserve("auth", **reservation(permission, "auth", "host"))
        claim(controller, "auth")
        start(controller, "auth")
    store.revoke(permission.reference)
    original_permission = store.permission(permission.reference)
    spec = SimpleNamespace(
        data=json.loads(bundle.installation.read_text()),
        seal=SimpleNamespace(path=bundle.installation),
    )
    recovery = ColdRecovery.__new__(ColdRecovery)
    recovery.spec, recovery.config_file = spec, bundle.config
    recovery.reference = "authored-separate-current-stop"
    recovery.check = lambda: None
    journal = HostJournal(bundle.root / "stop.sqlite", bundle.root / "stop.key")
    if auth_unknown:
        with pytest.raises(EvidenceRequired, match="authentication process identity"):
            recovery._native_accounting_stop(spec, journal, [], "attempt")
        assert all(not row["process_stopped"] for row in controller.snapshot()["calls"])
        assert not journal._history("trace")
    else:
        recovery._native_accounting_stop(spec, journal, [], "attempt")
        host = controller.snapshot()["calls"][0]
        assert host["process_stopped"] is True and host["outcome"] == "interrupted"
        assert host["worker_state"] == "released"
        assert journal._history("trace")[-1]["payload"]["model_success"] == "not_established"
    assert store.permission(permission.reference) == original_permission


def test_current_stop_guard_is_rechecked_before_native_commit(normal):
    from ai_task_manager.domains.work.errors import AccessDenied

    _, _, _, _, permission = normal
    controller = permission.controller
    controller.reserve("host", **reservation(permission))
    claim(controller, "host")
    start(controller, "host")
    calls = []

    def current():
        calls.append(True)
        if len(calls) == 2:
            raise AccessDenied("Current stop authority expired")

    with pytest.raises(AccessDenied, match="Current stop authority expired"):
        controller.record_outcome(
            "host",
            outcome="interrupted",
            process_stopped=True,
            evidence_ref="host-record:authored-stop",
            settlement_check=current,
        )
    assert len(calls) == 2
    row = controller.snapshot()["calls"][0]
    assert row["status"] == "running" and row["process_stopped"] is False
