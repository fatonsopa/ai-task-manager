"""Authored lead boundary controls; these do not establish native AI behavior."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from jsonschema import ValidationError

from ai_task_manager.adapters.hosts.bridge.lead import LeadToolGateway
from ai_task_manager.adapters.hosts.bridge.admission import (
    MANAGED_TOOLS, launch_template, managed_schemas,
)
from ai_task_manager.adapters.hosts.bridge.controller import WaitingGateway
from ai_task_manager.adapters.workflow_control.identity import file_binding
from ai_task_manager.config.files import read_private, write_private
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.native import NativeManifest
from ai_task_manager.installation.native.lead_startup import LeadDelegate
from ai_task_manager.installation.native.manifest import installation_intent
from ai_task_manager.installation.native.resolver import policy_builder
from tests.application.helpers import ServiceRig
from tests.native_installation.helpers import bundle, refresh_digest


class BoundSlot:
    """Declared transport seam around the real scoped application service."""

    generation = "authored-lead-generation"

    def __init__(self, service):
        self.service, self.calls = service, []

    def bind_notifications(self, notifications, enrollment):
        self.notifications, self.enrollment = notifications, enrollment

    def call(self, generation, operation, arguments):
        assert generation == self.generation
        self.calls.append(operation)
        return self.service.call(operation, arguments)


@pytest.mark.parametrize("report_only", [False, True])
def test_DP06L01_existing_read_surface_cannot_grant_worker_or_foreign_authority(
    tmp_path, report_only,
):
    env = ServiceRig(tmp_path)
    own = env.rig.task()
    foreign = env.rig.session(replace(env.rig.scope, logical_session="other"))
    hidden = env.rig.task(handle=foreign, confirmed=False)
    slot = BoundSlot(env.service)
    # These are the two existing connect_lead surfaces. The unchanged reporting
    # installer/service controls separately verify reporting_ref enrollment.
    operations = ("session_show", "event_list") if report_only else (
        "session_show", "event_list", "task_list", "task_show",
    )
    gateway = LeadToolGateway(
        slot, SimpleNamespace(), allowed_operations=operations,
        lead_enrollment_ref="authored-current-lead",
    )
    checks = []
    native = SimpleNamespace(
        policy=object(), binding=object(),
        verify_active=lambda: checks.append("active"),
        startup=SimpleNamespace(model_guard=SimpleNamespace(
            prepare_effect=lambda: checks.append("model"),
        )),
    )
    permits = SimpleNamespace(_owner=lambda *_: checks.append("owner"))
    delegate = LeadDelegate(native, SimpleNamespace(gateway=gateway), permits)
    if report_only:
        for name, arguments in [("task_list", {}), ("task_show", {"task_id": own.id})]:
            with pytest.raises(AccessDenied, match="configured lead surface"):
                delegate.call(name, arguments)
        assert slot.calls == []
    else:
        assert [x["id"] for x in delegate.call("task_list", {})["data"]] == [own.id]
        assert delegate.call("task_show", {"task_id": own.id})["data"]["id"] == own.id
        assert delegate.call("task_show", {"task_id": hidden.id})["outcome"] == "denied"
        before = len(slot.calls)
        with pytest.raises(ValidationError):
            delegate.call("task_show", {"task_id": own.id, "human": True})
        assert len(slot.calls) == before
    for name in ("worker_start", "read_file", "run_command"):
        with pytest.raises(AccessDenied, match="no worker file or command authority"):
            delegate.call(name, {})
    assert checks and checks[:3] == ["owner", "active", "model"]
    with env.rig.db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_DP06L02_current_native_guard_precedes_read_dispatch(tmp_path):
    calls = []

    def refused(*_):
        raise AccessDenied("Authored expired lead permission")

    delegate = LeadDelegate(
        SimpleNamespace(policy=None, binding=None),
        SimpleNamespace(gateway=SimpleNamespace(call=lambda *_: calls.append("read"))),
        SimpleNamespace(_owner=refused),
    )
    with pytest.raises(AccessDenied, match="expired lead permission"):
        delegate.call("task_list", {})
    assert calls == []


@pytest.mark.parametrize("role,report_only", [
    ("worker", False), ("reviewer", False), ("lead", False), ("lead", True),
])
def test_DP06L03_sealed_role_binds_negotiated_reads_before_startup(
    tmp_path, role, report_only,
):
    b = bundle(tmp_path, "claude", "claude-sonnet-5-5")
    config = read_private(b.config)
    if report_only:
        config["reporting_ref"] = "authored-event-only-reporting"
    write_private(b.config, config, replace=True)
    spec = read_private(b.installation)
    spec["role"] = role
    write_private(b.installation, spec, replace=True)
    b.data.update(config=file_binding(b.config), installation=installation_intent(b.installation))
    write_private(b.manifest, b.data, replace=True)
    refresh_digest(b.installation, b.manifest, b.data)
    manifest = NativeManifest(b.manifest, b.config, b.installation)
    policy = policy_builder(manifest)("claude", SimpleNamespace(worktree=tmp_path))
    expected_reads = ("task_list", "task_show") if role == "lead" and not report_only else ()
    assert policy.service_operations == expected_reads
    schemas = managed_schemas(policy)
    gateway = WaitingGateway(schemas)
    assert gateway.schemas == schemas
    base = {"mcp__aitm__acknowledge_event"} if role == "lead" else MANAGED_TOOLS
    assert set(policy.allowed_tools) == base | {
        "mcp__aitm__" + name for name in expected_reads
    }
    assert {"task_list", "task_show"} & schemas.keys() == set(expected_reads)
    launch_template(policy)
    with pytest.raises(AccessDenied, match="exactly its registered tools"):
        launch_template(replace(policy, service_operations=("worker_start",)))
