"""V2 component controls; authored protocol is not genuine native acceptance."""

import hashlib
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.conformance import (
    CompletedCapture,
    ConformanceCapture,
    NormalConformance,
    ProbeTurn,
    ProfileSources,
    prepare_profile,
)
from ai_task_manager.adapters.hosts.bridge.conformance.verification import verify_capture
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied, StaleVersion
from tests.hosts.conformance.test_capture import (
    FixtureEnrollment,
    certificate,
    native_frame,
)
from tests.hosts.conformance.test_capture import (
    rig as rig,
)


def test_same_profile_reuses_historical_capture_for_new_task_without_old_permission(rig, tmp_path):
    normal = certificate(rig, valid_until=None)
    original = rig.journal._read(normal.reference, fresh=False)
    rig.now[0] = rig.context.deadline + 50
    with pytest.raises(AccessDenied):
        rig.authority.require_current(rig.context, phase="observe")
    checked = []
    current = SimpleNamespace(**vars(rig.manifest))
    current.data = {
        **rig.manifest.data,
        "authorization_ref": "new-permission",
        "task_id": "new-task",
    }
    current.digest = digest("new task config and deadline")
    current.check = lambda: checked.append("fresh manifest")
    assert normal.require_installation(current) == normal.reference
    assert checked
    assert rig.journal._read(normal.reference, fresh=False) == original
    _, _, old_context = verify_capture(rig.journal, normal.reference)
    assert old_context.deadline == 1100
    # The capability result itself has no current-task permission/lease methods.
    assert not hasattr(normal, "authorize_initialization")


def test_runtime_cwd_and_task_seals_do_not_change_capability_recipe(rig, tmp_path):
    from ai_task_manager.adapters.hosts.bridge.conformance.profile import _projection_policy

    policy = _projection_policy(rig.manifest)
    worktree = tmp_path / "new-owned-task"
    worktree.mkdir(mode=0o700)
    changed = replace(policy, cwd=worktree, configuration_digest=digest("new-task"))
    assert prepare_profile(rig.manifest, rig.pins, "2.1.301", policy=changed) == rig.profile


@pytest.mark.parametrize("field", ["account_hash", "model", "effort", "environment"])
def test_each_relevant_launch_fact_changes_profile_or_refuses(rig, field):
    if field == "environment":
        rig.manifest.environment = {**rig.manifest.environment, "CLAUDE_CODE_ENTRYPOINT": "other"}
    else:
        rig.manifest.data[field] = {
            "account_hash": digest("other"),
            "model": "claude-sonnet-5-5",
            "effort": "medium",
        }[field]
    try:
        actual = prepare_profile(rig.manifest, rig.pins, "2.1.301")
    except AccessDenied:
        return
    assert actual != rig.profile


def test_logical_hashes_survive_authorized_relocation_but_old_physical_seal_does_not(rig, tmp_path):
    logical, path, sha = rig.pins.files[0]
    relocated = tmp_path / "same-code.py"
    relocated.write_bytes(path.read_bytes())
    current = ProfileSources({logical: (relocated, sha)}, dict(rig.pins.versions))
    assert prepare_profile(rig.manifest, current, "2.1.301") == rig.profile
    backup = path.with_suffix(".replacement")
    backup.write_bytes(path.read_bytes())
    backup.replace(path)
    with pytest.raises(AccessDenied):
        rig.pins.check()


def test_fixed_instruction_selection_is_part_of_profile(rig):
    logical, path, sha = rig.pins.files[0]
    role = ProfileSources(
        {logical: (path, sha)}, dict(rig.pins.versions), instruction_ids=(logical,)
    )
    assert prepare_profile(rig.manifest, role, "2.1.301") != rig.profile


def test_v1_signature_and_deadline_cannot_be_promoted_to_v2(rig):
    normal = certificate(rig)
    p = rig.journal._read(normal.reference)["payload"]
    p["version"] = 1
    ref = rig.journal._append("trace", p)
    consumer = NormalConformance(
        rig.manifest,
        rig.pins,
        rig.context,
        rig.journal,
        ref,
        enrollment=FixtureEnrollment(rig.journal, ref, rig.profile),
    )
    with pytest.raises(AccessDenied):
        consumer.require_installation(rig.manifest)


def test_enrollment_cannot_be_replaced_by_only_signed_report(rig):
    normal = certificate(rig)

    class Missing:
        def require(self, *args):
            raise AccessDenied("No actual completion custody")

    consumer = NormalConformance(
        rig.manifest, rig.pins, rig.context, rig.journal, normal.reference, enrollment=Missing()
    )
    with pytest.raises(AccessDenied):
        consumer.require_installation(rig.manifest)


def test_completion_constructor_does_not_accept_arbitrary_export_callbacks():
    with pytest.raises(AccessDenied):
        CompletedCapture(SimpleNamespace(_completion_token=object()), object())
    with pytest.raises(TypeError):
        type("ForgedCompletion", (CompletedCapture,), {})


class AuthoredFactory:
    """In-process host double using real contracts; no actual host is executed."""

    def __init__(self, r):
        self.r = r

    def start(self, context, gateway):
        from contextlib import nullcontext

        from ai_task_manager.adapters.hosts.bridge.conformance.profile import _projection_policy
        from ai_task_manager.adapters.hosts.bridge.controller import (
            ManagedNativeHost,
            WaitingGateway,
        )
        from ai_task_manager.adapters.hosts.bridge.records import HostBinding, ProcessIdentity
        from ai_task_manager.adapters.hosts.transports.claude import sdk_command, sdk_options

        r = self.r
        policy = replace(_projection_policy(r.manifest), cwd=r.root)
        binding = HostBinding(
            context.scope,
            "probe-session",
            "a",
            "w",
            "claude",
            "probe",
            policy.configuration_digest,
            context.deadline,
        )
        identity = ProcessIdentity(123, "fixture-generation", 120)
        r.owner = {"binding": asdict(binding), "process": asdict(identity)}
        host = object.__new__(ManagedNativeHost)
        host.binding, host.policy, host.journal = binding, policy, r.journal
        host.core = host.resources = None
        host.identity = host._identity_bound = None
        host.expected_account = context.account_hash
        host.process = SimpleNamespace(
            identity=identity,
            _closed=False,
            command=sdk_command(policy, sdk_options(policy, gateway), 1),
            check_current_liveness=lambda: None,
        )
        host.session = object()
        host.gateway = WaitingGateway()
        host.verify_active = lambda: identity
        host.current_invocation = lambda: SimpleNamespace(effect_boundary=nullcontext)
        auth = r.journal._append(
            "trace",
            {
                "auth_method": "claude.ai",
                "account_hash": context.account_hash,
                "configuration_digest": binding.configuration_digest,
            },
        )
        from ai_task_manager.adapters.hosts.bridge.admission import MANAGED_TOOLS

        init = native_frame(
            r,
            {
                "type": "system",
                "subtype": "init",
                "apiKeySource": "none",
                "claude_code_version": "2.1.301",
                "model": context.model,
                "session_id": r.session,
                "tools": sorted(MANAGED_TOOLS),
                "mcp_servers": [{"name": "aitm", "source": "sdk", "status": "connected"}],
            },
        )
        ready = r.journal._append(
            "startup",
            {
                **asdict(binding),
                "process_id": 123,
                "process_start_identity": "fixture-generation",
                "session_id": r.session,
                "model": context.model,
                "requested_effort": "high",
            },
            sources=(auth, init),
            expires_at=r.journal.clock() + 60,
        )
        host.startup = SimpleNamespace(ready_ref=ready)
        return host

    def execute(self, context, native, case):
        r = self.r
        invocation = "authored-call:" + case.case_id
        request = r.journal._append(
            "trace",
            {
                "effect": "native_probe_submission",
                "case_id": case.case_id,
                "prompt_sha256": hashlib.sha256(case.prompt.encode()).hexdigest(),
                "session_id": r.session,
                "invocation_ref": invocation,
                **r.owner,
            },
        )
        if case.tool:
            native_frame(
                r,
                {
                    "type": "assistant",
                    "session_id": r.session,
                    "message": {
                        "model": context.model,
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "mcp__aitm__" + case.tool,
                                "input": case.arguments,
                            }
                        ],
                    },
                },
            )
            try:
                native.gateway.call(case.tool, case.arguments)
            except (AccessDenied, StaleVersion):
                assert case.kind == "deny"
        terminal = native_frame(
            r, {"type": "result", "subtype": "success", "is_error": False, "session_id": r.session}
        )
        return ProbeTurn(request, terminal, invocation)

    def stop(self, context, native):
        from ai_task_manager.adapters.process.groups import GroupObservation, GroupStop

        native.process._closed = True
        return GroupStop(True, GroupObservation(True))


def test_unmodified_capture_producer_issues_opaque_completion_only_after_confirmed_stop(rig):
    # The host adapter is authored; this checks production orchestration and
    # custody, not authentic native execution or normal release acceptance.
    (rig.root / "input.txt").unlink()
    capture = ConformanceCapture(
        rig.manifest, rig.pins, rig.context, rig.authority, rig.journal, profile=rig.profile
    )
    with pytest.raises(AccessDenied):
        capture.completed()
    ref = capture.run(AuthoredFactory(rig))
    completed = capture.completed()
    export = completed.export()
    assert export["reference"] == ref and export["profile_digest"] == rig.profile.fingerprint
    assert export["provenance"]["probe_deadline"] == 1100
    rig.now[0] = 1150
    assert completed.export() == export
    completed._token = object()
    with pytest.raises(AccessDenied):
        completed.export()
