"""Real capture/profile consumers over authored hosts; no genuine model evidence."""

from dataclasses import replace
import time
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.admission import managed_schemas, managed_tools
from ai_task_manager.adapters.hosts.bridge.conformance import ConformanceCapture, NormalConformance, cases, prepare_profile
from ai_task_manager.adapters.hosts.bridge.conformance.profile import _projection_policy, profile_case_ids, profile_tools
from ai_task_manager.adapters.hosts.bridge.conformance.verification import verify_capture
from ai_task_manager.adapters.hosts.bridge.controller import WaitingGateway
from ai_task_manager.adapters.hosts.transports.claude import sdk_command, sdk_options
from ai_task_manager.config.files import FileSeal, read_private, write_private
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.native.enrollment import CaptureEnrollments
from ai_task_manager.installation.native.resolver import policy_builder
from ai_task_manager.installation.native.store import NativeStore
from tests.hosts.conformance.test_capture import native_frame
from tests.hosts.conformance.test_capture import rig as rig
from tests.hosts.conformance.test_profile import AuthoredFactory


class RoleFactory(AuthoredFactory):
    """Authored actual SDK recipe and inventory, with production capture verification."""

    extra_tool = None

    def start(self, context, gateway):
        host = super().start(context, gateway)
        host.gateway = WaitingGateway(managed_schemas(host.policy))
        host.process.command = sdk_command(host.policy, sdk_options(host.policy, host.gateway), 1)
        original = self.r.journal._read(host.startup.ready_ref, "startup")
        auth, init = original["sources"]
        frame = self.r.journal._read(init)["payload"]["frame"]
        tools = sorted(managed_tools(host.policy))
        if self.extra_tool:
            tools.append(self.extra_tool)
        current_init = native_frame(self.r, {**frame, "tools": tools})
        host.startup.ready_ref = self.r.journal._append(
            "startup", original["payload"], sources=(auth, current_init), expires_at=1060,
        )
        return host


def prepare_role(rig, role, reporting):
    spec = read_private(rig.manifest.installation.path)
    write_private(rig.manifest.installation.path, {**spec, "role": role}, replace=True)
    config = rig.manifest.installation.path.with_name("config.json")
    write_private(config, {"reporting_ref": "authored-report-only"} if reporting else {})
    rig.manifest.config = FileSeal(config)
    rig.manifest.installation = FileSeal(rig.manifest.installation.path)
    manifest_file = config.with_name("test-manifest.json")
    write_private(manifest_file, rig.manifest.data)
    rig.manifest.seal = FileSeal(manifest_file)
    rig.manifest.executable = _projection_policy(rig.manifest).executable
    rig.profile = prepare_profile(rig.manifest, rig.pins, "2.1.301")
    rig.context = replace(rig.context, profile_digest=rig.profile.fingerprint, case_ids=profile_case_ids(rig.profile))
    rig.authority.context = rig.context
    rig.cases = cases(rig.context)
    return _projection_policy(rig.manifest)


def capture_role(rig, tmp_path, factory=RoleFactory):
    (rig.root / "input.txt").unlink()
    producer = ConformanceCapture(rig.manifest, rig.pins, rig.context, rig.authority, rig.journal, profile=rig.profile)
    reference = producer.run(factory(rig))
    store = NativeStore.create(tmp_path / "role-capability.sqlite")
    enrollments = CaptureEnrollments(store)
    enrollments.enroll(producer.completed(), rig.journal, rig.pins, valid_until=time.time() + 1200)
    consumer = enrollments.consumer(rig.manifest, reference)
    return producer, consumer


@pytest.mark.parametrize("role,reporting", [("worker", False), ("reviewer", False), ("lead", False), ("lead", True)])
def test_role_recipe_matches_sealed_projection_and_real_capture_consumer(rig, tmp_path, role, reporting):
    projected = prepare_role(rig, role, reporting)
    runtime = policy_builder(rig.manifest)("claude", SimpleNamespace(worktree=rig.root))
    assert managed_schemas(runtime) == managed_schemas(projected)
    assert prepare_profile(rig.manifest, rig.pins, "2.1.301", policy=runtime) == rig.profile
    assert profile_tools(rig.profile) == managed_tools(runtime)
    if role == "lead":
        expected = {"acknowledge_event"} if reporting else {"acknowledge_event", "task_list", "task_show"}
        assert set(managed_schemas(runtime)) == expected
        assert len(profile_case_ids(rig.profile)) == 5
    else:
        assert set(managed_schemas(runtime)) == {"read_file", "write_file", "run_command", "acknowledge_event"}
        assert len(profile_case_ids(rig.profile)) == 11
    producer, consumer = capture_role(rig, tmp_path)
    assert consumer.require_native_conformance(rig.manifest, runtime, producer._native_owner.binding, rig.journal) == producer.report_ref
    _, profile, context = verify_capture(rig.journal, producer.report_ref)
    assert profile == rig.profile and context.case_ids == profile_case_ids(profile)
    for tool in ("Bash", "mcp__foreign__read", "mcp__aitm__worker_start"):
        with pytest.raises(AccessDenied, match="Managed tool surface changed"):
            consumer.require_native_conformance(rig.manifest, replace(runtime, allowed_tools=(*runtime.allowed_tools, tool)), producer._native_owner.binding, rig.journal)
    if role == "lead":
        with pytest.raises(AccessDenied):
            consumer.require_native_conformance(rig.manifest, replace(runtime, allowed_tools=(*runtime.allowed_tools, "mcp__aitm__write_file")), producer._native_owner.binding, rig.journal)
    for changed in (replace(runtime, role="lead" if role != "lead" else "worker"), replace(runtime, service_operations=("worker_start",))):
        with pytest.raises(AccessDenied, match="sealed role tools"):
            prepare_profile(rig.manifest, rig.pins, "2.1.301", policy=changed)


@pytest.mark.parametrize("reporting", [False, True])
def test_worker_capture_cannot_admit_a_lead_surface(rig, tmp_path, reporting):
    _, worker = capture_role(rig, tmp_path)
    prepare_role(rig, "lead", reporting)
    with pytest.raises(AccessDenied, match="role recipe differs"):
        worker.require_installation(rig.manifest)


@pytest.mark.parametrize("extra", ["mcp__aitm__write_file", "Bash", "mcp__foreign__read"])
def test_lead_capture_producer_rejects_broader_observed_inventory(rig, tmp_path, extra):
    prepare_role(rig, "lead", False)
    factory = type("ExpandedInventory", (RoleFactory,), {"extra_tool": extra})
    with pytest.raises(AccessDenied, match="complete restricted native inventory"):
        capture_role(rig, tmp_path, factory)


def test_exact_lead_matrix_required_at_producer_and_cold_consumer(rig, tmp_path):
    prepare_role(rig, "lead", False)
    original = rig.context
    rig.context = replace(original, case_ids=original.case_ids[:-1])
    rig.authority.context = rig.context
    producer = ConformanceCapture(rig.manifest, rig.pins, rig.context, rig.authority, rig.journal, profile=rig.profile)
    with pytest.raises(AccessDenied, match="complete frozen role profile matrix"):
        producer.run(RoleFactory(rig))
    rig.context = original
    rig.authority.context = original
    complete, consumer = capture_role(rig, tmp_path)
    payload = rig.journal._read(complete.report_ref)["payload"]
    payload["cases"] = payload["cases"][:-1]
    ref = rig.journal._append("trace", payload)
    forged = NormalConformance(rig.manifest, rig.pins, rig.context, rig.journal, ref, enrollment=consumer.enrollment)
    with pytest.raises(AccessDenied, match="frozen capability matrix is incomplete"):
        forged.require_installation(rig.manifest)
