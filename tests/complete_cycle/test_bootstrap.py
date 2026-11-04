"""Explicit bootstrap authority and admission order through real local contracts."""

import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.config.capacity import policies
from ai_task_manager.config.files import write_private
from ai_task_manager.config.service import load_service
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired, ValidationError
from ai_task_manager.installation.sessions.recovery import setup_lock
from ai_task_manager.installation.workflows.bootstrap import prepare
from tests.complete_cycle.provider import CycleProvider
from tests.module_core.fixture import Demonstration


@pytest.fixture
def boot(tmp_path, monkeypatch):
    root = tmp_path / "scenario"
    root.mkdir(mode=0o700)
    db = tmp_path / "core.sqlite"
    db.touch(mode=0o600)
    sockets = tempfile.TemporaryDirectory(prefix="bp-", dir="/tmp")
    control = {
        "record": str(Path(sockets.name).resolve() / "owner.json"),
        "endpoint": str(Path(sockets.name).resolve() / "owner.sock"),
        "instance": str(uuid4()),
    }
    demo = Demonstration(
        root,
        db,
        Path("/Users/faton/Documents/ChatGPT/technical-writer"),
        workflow_control=control,
        provider_factory=CycleProvider,
        capacity_policies=[{"host": "claude", "host_limit": 2, "project_limit": 2}],
    )
    monkeypatch.setattr(HttpTransport, "ENDPOINT", demo.endpoint)
    try:
        yield demo, control
    finally:
        demo.close()
        sockets.cleanup()


def test_BP01_explicit_prepare_cold_repeat_and_finite_capacity(boot):
    demo, _ = boot
    req = {"operation": "task_list", "data": {}}
    assert prepare(demo.config, req)["data"] == []
    assert prepare(demo.config, req)["data"] == []
    service = load_service(demo.config)
    with service.core.context.db.transaction() as conn:
        assert [
            tuple(r) for r in conn.execute("SELECT host,host_limit,project_limit FROM capacity")
        ] == [("claude", 2, 2)]
    # Ordinary callers never fall back when the owner is absent.
    assert service.call("task_list", {})["outcome"] == "denied"


@pytest.mark.parametrize(
    "operation_case",
    [
        {"operation": "worker_start", "data": {}},
        {"operation": "workflow_run", "data": {}},
        {"operation": "event_acknowledge", "data": {}},
        {"operation": "task_action", "data": {"action": "non_code_complete"}},
        {"operation": "task_submit", "data": {"parent_task_id": "old"}},
    ],
)
def test_BP02_initial_scope_cannot_expand_or_create_execution(boot, operation_case):
    demo, _ = boot
    before = len(demo.provider.requests)
    with pytest.raises(AccessDenied):
        prepare(demo.config, operation_case)
    assert len(demo.provider.requests) == before and demo.provider.issues == {}


def test_BP03_competing_activation_lock_refuses_without_wait_or_fallback(boot):
    from ai_task_manager.adapters.workflow_control.owner import WorkflowOwner

    demo, _ = boot
    reached = []
    owner = object.__new__(WorkflowOwner)
    owner.config_file = demo.config
    owner._serve = lambda *a: reached.append(True)
    with setup_lock(demo.config):
        with pytest.raises(EvidenceRequired, match="Another trusted session setup"):
            prepare(demo.config, {"operation": "task_list", "data": {}})
        with pytest.raises(EvidenceRequired, match="Another trusted session setup"):
            owner.serve()
    assert reached == []


@pytest.mark.parametrize("state", ["record", "endpoint", "revoked", "foreign"])
def test_BP04_existing_unknown_owner_and_lost_authority_refuse(boot, state):
    demo, control = boot
    if state in {"record", "endpoint"}:
        write_private(control[state], {"unknown_owner": True})
    elif state == "revoked":
        load_service(demo.config).decisions.observations.revoke()
    else:
        demo.provider.viewer_id = str(uuid4())
    with pytest.raises((AccessDenied, EvidenceRequired, LinearError)):
        prepare(demo.config, {"operation": "task_list", "data": {}})
    assert demo.provider.issues == {}


@pytest.mark.parametrize(
    "value",
    [
        [],
        [{"host": "claude", "host_limit": True, "project_limit": 1}],
        [{"host": "claude", "host_limit": 0, "project_limit": 1}],
        [{"host": "other", "host_limit": 1, "project_limit": 1}],
        [{"host": "claude", "host_limit": 1, "project_limit": 1}] * 2,
        [
            {"host": "claude", "host_limit": 1, "project_limit": 1},
            {"host": "codex", "host_limit": 1, "project_limit": 2},
        ],
    ],
)
def test_BP05_capacity_rejects_invalid_or_inconsistent_limits(value):
    with pytest.raises(ValidationError):
        policies({"capacity_policies": value})


def test_BP06_activation_history_closes_preparation_after_descriptor_loss(boot):
    from ai_task_manager.adapters.decisions.observations import bind_record
    from ai_task_manager.installation.workflows.bootstrap import activation_reference

    demo, control = boot
    service = load_service(demo.config)
    bind_record(
        service.decisions.journal,
        activation_reference(demo.config, control),
        {"declared_prior_activation": True},
    )
    assert not Path(control["record"]).exists()
    with pytest.raises(AccessDenied, match="activation history"):
        prepare(demo.config, {"operation": "task_list", "data": {}})
