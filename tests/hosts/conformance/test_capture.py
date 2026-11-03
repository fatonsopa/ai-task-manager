"""Authored protocol controls and real private files; never fresh native acceptance."""

import hashlib
from contextlib import nullcontext
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.admission import MANAGED_TOOLS
from ai_task_manager.adapters.hosts.bridge.conformance import (
    CASE_IDS,
    CanaryGateway,
    ConformanceCapture,
    NormalConformance,
    ProbeContext,
    ProbeThreadGate,
    cases,
    profile_digest,
)
from ai_task_manager.adapters.hosts.bridge.conformance.verification import verify_case
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.config.files import digest, write_private
from ai_task_manager.domains.work.errors import AccessDenied, StaleVersion
from ai_task_manager.domains.work.models import Scope


class FixtureAuthority:
    def __init__(self, clock):
        self.clock, self.context, self.revoked = clock, None, False

    def require_current(self, context, *, policy=None, binding=None, phase):
        if context is not self.context or self.revoked or self.clock() >= context.deadline:
            raise AccessDenied("Authored probe permission refused")


@pytest.fixture
def rig(tmp_path):
    now = [1000.0]
    root = tmp_path / "canary"
    root.mkdir(mode=0o700)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    journal = HostJournal(state / "host.sqlite", state / "key", clock=lambda: now[0])
    installation = state / "installation.json"
    write_private(installation, {"deadline": 1200.0})
    manifest = SimpleNamespace(
        digest=digest("manifest"),
        check=lambda: None,
        installation=SimpleNamespace(path=installation),
        data={
            "host": "claude",
            "model": "claude-opus-5-5",
            "effort": "high",
            "account_hash": digest("authored-account"),
            "executable_sha256": digest("fixture"),
        },
    )
    pins = SimpleNamespace(fingerprint=digest("source"), check=lambda: None)
    scope = Scope("linear", "workspace", "principal", "project", "session")
    info = root.stat()
    context = ProbeContext(
        "authored-probe",
        manifest.digest,
        profile_digest(manifest, pins),
        scope,
        "claude",
        manifest.data["model"],
        "high",
        manifest.data["account_hash"],
        1100.0,
        root,
        (info.st_dev, info.st_ino, info.st_uid, 0o700),
        CASE_IDS,
    )
    authority = FixtureAuthority(journal.clock)
    authority.context = context
    gateway = CanaryGateway(context, authority, journal)
    binding = SimpleNamespace(
        host="claude", model=context.model, effort="high", scope=scope, deadline=1100.0
    )
    native = SimpleNamespace(
        policy=binding,
        binding=binding,
        verify_active=lambda: None,
        process=SimpleNamespace(check_current_liveness=lambda: None),
        current_invocation=lambda: SimpleNamespace(effect_boundary=nullcontext),
    )
    gateway.bind(native)
    owner = {
        "binding": {
            "attempt_id": "a",
            "worker_id": "w",
            "configuration_digest": "c",
            "scope": asdict(scope),
            "host": "claude",
        },
        "process": {"pid": 123, "group_id": 120, "start_identity": "fixture-generation"},
    }
    value = SimpleNamespace(
        now=now,
        root=root,
        journal=journal,
        manifest=manifest,
        pins=pins,
        context=context,
        authority=authority,
        gateway=gateway,
        native=native,
        owner=owner,
        session="native-session",
        cases=cases(context),
    )

    value.ready_ref = identity_record(value)
    return value


def native_frame(r, value, *, owner=None):
    owner = owner or r.owner
    return r.journal._append(
        "trace",
        {
            **{k: owner["binding"][k] for k in ("attempt_id", "worker_id", "configuration_digest")},
            "process": owner["process"],
            "frame_kind": "native_frame",
            "frame": value,
            "frame_hash": digest(value),
            "raw_frame_hash": digest(value),
        },
    )


def identity_record(r):
    return r.journal._append(
        "startup",
        {
            **r.owner["binding"],
            "process_id": 123,
            "process_start_identity": "fixture-generation",
            "model": r.context.model,
            "requested_effort": "high",
            "session_id": r.session,
        },
        expires_at=1060,
    )


def observed(r, case):
    g = r.gateway
    g.begin(case)
    before = g.snapshot()
    invocation = "authored-invocation:" + case.case_id
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
    refs = []
    if case.tool:
        refs.append(
            native_frame(
                r,
                {
                    "type": "assistant",
                    "session_id": r.session,
                    "message": {
                        "model": r.context.model,
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "mcp__aitm__" + case.tool,
                                "id": "call",
                                "input": case.arguments,
                            }
                        ],
                    },
                },
            )
        )
        try:
            g.call(case.tool, case.arguments)
        except (AccessDenied, StaleVersion):
            assert case.kind == "deny"
    terminal = native_frame(
        r, {"type": "result", "subtype": "success", "is_error": False, "session_id": r.session}
    )
    refs.append(terminal)
    return {
        "identity_before": r.ready_ref,
        "identity_after": r.ready_ref,
        "request_ref": request,
        "terminal_ref": terminal,
        "invocation_ref": invocation,
        "frame_refs": refs,
        "gateway_refs": list(g.end()),
        "before": before,
        "after": g.snapshot(),
    }


def certificate(r):
    auth = r.journal._append(
        "trace",
        {
            "auth_method": "claude.ai",
            "account_hash": r.context.account_hash,
            "configuration_digest": "c",
        },
    )
    init = native_frame(
        r,
        {
            "type": "system",
            "subtype": "init",
            "apiKeySource": "none",
            "claude_code_version": "2.1.301",
            "model": r.context.model,
            "session_id": r.session,
            "tools": sorted(MANAGED_TOOLS),
            "mcp_servers": [{"name": "aitm", "source": "sdk", "status": "connected"}],
        },
    )
    startup = r.journal._append(
        "startup",
        {
            **r.owner["binding"],
            "process_id": 123,
            "process_start_identity": "fixture-generation",
            "model": r.context.model,
            "requested_effort": "high",
            "session_id": r.session,
        },
        sources=(auth, init),
        expires_at=1060,
    )
    values = [{"case_id": c.case_id, **observed(r, c)} for c in r.cases]
    ref = r.journal._append(
        "trace",
        {
            "effect": "native_conformance_capture",
            "profile": r.context.profile_digest,
            "manifest_digest": r.manifest.digest,
            "source": r.pins.fingerprint,
            "context_ref": r.context.reference,
            "owner": r.owner,
            "startup_ref": startup,
            "cases": values,
            "confirmed_stop": True,
            "stop": {"confirmed": True, "observation": {"known": True, "members": []}},
            "not_after": 1200,
            "error_type": None,
        },
    )
    return NormalConformance(r.manifest, r.pins, r.context, r.journal, ref)


@pytest.mark.parametrize("index", range(11))
def test_fixed_canary_facts_require_actual_calls_and_results(rig, index):
    c = rig.cases[index]
    v = observed(rig, c)
    verify_case(rig.journal, c, v, rig.owner, rig.context.reference, rig.session, rig.context.model)


@pytest.mark.parametrize(
    "change", ["missing_call", "foreign_owner", "foreign_session", "native_tool", "model"]
)
def test_signed_data_does_not_replace_the_actual_case_oracle(rig, change):
    c = rig.cases[0]
    v = observed(rig, c)
    if change == "missing_call":
        v["frame_refs"] = [v["terminal_ref"]]
    else:
        raw = {
            "type": "assistant",
            "session_id": rig.session,
            "message": {
                "model": rig.context.model,
                "content": [
                    {"type": "tool_use", "name": "mcp__aitm__read_file", "input": c.arguments}
                ],
            },
        }
        owner = rig.owner
        if change == "foreign_owner":
            owner = {**owner, "process": {**owner["process"], "pid": 999}}
        elif change == "foreign_session":
            raw["session_id"] = "foreign"
        elif change == "native_tool":
            raw["message"]["content"][0]["name"] = "Bash"
        else:
            raw["message"]["model"] = "unrequested"
        v["frame_refs"][0] = native_frame(rig, raw, owner=owner)
    with pytest.raises(AccessDenied):
        verify_case(
            rig.journal, c, v, rig.owner, rig.context.reference, rig.session, rig.context.model
        )


def test_stopped_profile_facts_survive_identity_expiry_but_not_revocation(rig):
    normal = certificate(rig)
    rig.now[0] = 1061
    assert normal.require_installation(rig.manifest) == normal.reference
    normal.revoke()
    with pytest.raises(AccessDenied):
        normal.require_installation(rig.manifest)


@pytest.mark.parametrize("change", ["source", "deadline", "reference", "codex"])
def test_normal_profile_changes_never_reuse_a_capture(rig, change):
    normal = certificate(rig)
    if change == "source":
        rig.pins.fingerprint = digest("changed")
    elif change == "deadline":
        rig.now[0] = 1200
    elif change == "reference":
        normal.reference = "host-record:unknown"
    else:
        rig.context = replace(rig.context, host="codex")
        normal.context = rig.context
    with pytest.raises(AccessDenied):
        normal.require_installation(rig.manifest)


@pytest.mark.parametrize("change", ["expired", "revoked", "bound", "foreign"])
def test_probe_thread_gate_cannot_grant_normal_or_foreign_work(rig, change):
    g = rig.gateway
    # Thread authorization precedes binding a live native or any active case.
    g.native = g._native = None
    gate = ProbeThreadGate(rig.context, rig.authority, g)
    if change == "expired":
        rig.now[0] = rig.context.deadline
    elif change == "revoked":
        rig.authority.revoked = True
    elif change == "bound":
        g.native = rig.native
    else:
        gate.context = replace(rig.context, reference="foreign")
    with pytest.raises(AccessDenied):
        gate.authorize(rig.native.policy, rig.native.binding)


def test_probe_thread_gate_is_distinct_from_conformance_and_needs_no_core(rig):
    rig.gateway.native = rig.gateway._native = None
    gate = ProbeThreadGate(rig.context, rig.authority, rig.gateway)
    assert gate.authorize(rig.native.policy, rig.native.binding) is None
    assert not hasattr(gate, "require_native_conformance")


def test_capture_pre_return_failure_preserves_original_and_unknown_stop(rig):
    original = AccessDenied("authored startup failure")

    def start(*args):
        raise original

    capture = ConformanceCapture(rig.manifest, rig.pins, rig.context, rig.authority, rig.journal)
    # Use a distinct fresh canary; constructor must never overwrite an old input.
    (rig.root / "input.txt").unlink()
    with pytest.raises(AccessDenied) as raised:
        capture.run(SimpleNamespace(start=start))
    assert raised.value is original
    payload = rig.journal._read(capture.report_ref)["payload"]
    assert payload["confirmed_stop"] is False and payload["stop"] is None


@pytest.mark.parametrize("change", ["missing", "expired", "foreign"])
def test_probe_identity_must_cover_each_actual_turn(rig, change):
    c = rig.cases[0]
    v = observed(rig, c)
    if change == "missing":
        v["identity_after"] = "host-record:missing"
    elif change == "expired":
        identity = rig.journal._append(
            "startup",
            {
                **rig.owner["binding"],
                "process_id": 123,
                "process_start_identity": "fixture-generation",
                "session_id": rig.session,
            },
            expires_at=1001,
        )
        rig.now[0] = 1001
        v["identity_after"] = identity
        v["terminal_ref"] = native_frame(
            rig,
            {"type": "result", "subtype": "success", "is_error": False, "session_id": rig.session},
        )
    else:
        v["identity_after"] = rig.journal._append(
            "startup",
            {
                **rig.owner["binding"],
                "process_id": 999,
                "process_start_identity": "foreign",
                "session_id": rig.session,
            },
            expires_at=1060,
        )
    with pytest.raises(AccessDenied):
        verify_case(
            rig.journal, c, v, rig.owner, rig.context.reference, rig.session, rig.context.model
        )


@pytest.mark.parametrize("change", ["matrix", "unknown_stop", "extended_lifetime"])
def test_signed_capture_flags_cannot_waive_missing_facts(rig, change):
    normal = certificate(rig)
    p = rig.journal._read(normal.reference)["payload"]
    if change == "matrix":
        p["cases"] = p["cases"][:-1]
    elif change == "unknown_stop":
        p["stop"]["observation"]["known"] = False
    else:
        p["not_after"] += 1
    ref = rig.journal._append("trace", p)
    altered = NormalConformance(rig.manifest, rig.pins, rig.context, rig.journal, ref)
    with pytest.raises(AccessDenied):
        altered.require_installation(rig.manifest)


@pytest.mark.parametrize(
    "tool,args",
    [
        ("read_file", {"path": "controller.key"}),
        (
            "write_file",
            {"path": "controller.json", "text": "no", "expected_hash": None, "operation_key": "x"},
        ),
    ],
)
def test_canary_gateway_cannot_read_or_write_factory_state(rig, tool, args):
    rig.gateway.begin(rig.cases[0])
    before = rig.gateway.snapshot()
    with pytest.raises(AccessDenied):
        rig.gateway.call(tool, args)
    assert rig.gateway.snapshot() == before
    assert not (rig.root / "controller.json").exists()


@pytest.mark.parametrize("extra_tool", [False, True])
def test_normal_consumer_rechecks_actual_sdk_launch_template(rig, tmp_path, extra_tool):
    from ai_task_manager.adapters.hosts.bridge.admission import launch_template
    from ai_task_manager.adapters.hosts.bridge.policy import (
        NativePolicy,
        clean_environment,
        policy_digest,
    )

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    profile = home / ".claude_roni"
    profile.mkdir(mode=0o700)
    executable = Path("/usr/bin/true")
    sha = hashlib.sha256(executable.read_bytes()).hexdigest()
    environment = clean_environment(home, claude_profile=profile)
    policy = NativePolicy(
        "claude",
        executable,
        sha,
        environment,
        rig.root,
        policy_digest(
            "claude", sha, environment, rig.root, {}, model=rig.context.model, effort="high"
        ),
        (),
        tuple(sorted(MANAGED_TOOLS)),
        rig.context.model,
        "high",
    )
    rig.manifest.data["executable_sha256"] = sha
    rig.manifest.environment = environment
    normal = certificate(rig)
    payload = rig.journal._read(normal.reference)["payload"]
    from ai_task_manager.adapters.hosts.bridge.conformance.capture import check_command
    from ai_task_manager.supervisor.models import Command

    compiled = launch_template(policy)
    command = Command(tuple(compiled["argv"]), rig.root, environment, 10, "claude")
    assert check_command(policy, command, rig.root) == digest(compiled)
    for changed in (
        replace(command, environment={**environment, "EXTRA": "unreviewed"}),
        replace(command, argv=(*command.argv, "--unreviewed")),
        replace(command, cwd=tmp_path),
    ):
        with pytest.raises(AccessDenied):
            check_command(policy, changed, rig.root)
    payload["launch_fingerprint"] = digest(compiled)
    ref = rig.journal._append("trace", payload)
    normal = NormalConformance(rig.manifest, rig.pins, rig.context, rig.journal, ref)
    if extra_tool:
        policy = replace(policy, allowed_tools=(*policy.allowed_tools, "Bash"))
        with pytest.raises(AccessDenied):
            normal.require_native_conformance(rig.manifest, policy, rig.native.binding, rig.journal)
    else:
        assert (
            normal.require_native_conformance(rig.manifest, policy, rig.native.binding, rig.journal)
            == ref
        )


def test_probe_context_rejects_nonfinite_deadline_and_relative_root(rig):
    with pytest.raises(AccessDenied):
        replace(rig.context, deadline=float("nan"))
    with pytest.raises(AccessDenied):
        replace(rig.context, fixture_root=Path("relative"))
