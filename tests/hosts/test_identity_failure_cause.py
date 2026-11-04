"""Owned redacted helper causes survive cancellation and retention failure."""

import sqlite3
from threading import Event
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.controller import ManagedNativeHost
from ai_task_manager.adapters.hosts.bridge.identity import NativeIdentityObservations
from ai_task_manager.adapters.hosts.bridge.identity_refresh import IdentityRefresh
from ai_task_manager.domains.work.errors import AccessDenied


@pytest.mark.parametrize("retention_fails", [False, True])
@pytest.mark.parametrize(
    "error_type,code",
    [(AccessDenied, "access_denied"), (sqlite3.OperationalError, "sqlite_operational_error")],
)
def test_helper_retains_original_cause_before_stop(
    journal, tmp_path, retention_fails, error_type, code
):
    host = object.__new__(ManagedNativeHost)
    host.journal, host.binding = journal, SimpleNamespace(attempt_id="authored-helper")
    host.failure_diagnostics, host.diagnostics_truncated = [], False
    stopped = Event()

    def stop():
        assert len(host.failure_diagnostics) == 1
        stopped.set()

    host.stop = stop
    if retention_fails:

        def fail(*args, **kwargs):
            raise OSError("private retention detail")

        journal._append = fail
    observations = object.__new__(NativeIdentityObservations)
    observations.journal, observations.native = journal, host
    observations.execution_not_after = journal.clock() + 30
    observations.revoke = lambda: None
    calls = []
    original = error_type("private helper detail must not be retained")

    def prepare(**kwargs):
        calls.append(1)
        if len(calls) > 1:
            raise original
        return SimpleNamespace(observation=SimpleNamespace(expires_at=journal.clock() + 60))

    observations.prepare = prepare
    helper = IdentityRefresh(observations, tmp_path / "helper", 1, on_failure=host.identity_failed)
    helper.start()
    assert stopped.wait(2)
    assert helper.close()
    item = host.failure_diagnostics[0]
    assert item["failure"]["stage"] == "identity_refresh"
    assert item["failure"]["causes"][0]["code"] == code
    assert "private" not in str(item)
    if retention_fails:
        assert not item["retained"]
        assert item["retention_failure"]["stage"] == "diagnostic_retention"
    else:
        assert item["retained"]
        assert journal._read(item["reference"], "trace")["payload"]["failure"] == item["failure"]
    with pytest.raises(AccessDenied, match="stopped, failed"):
        helper.ensure_current()
