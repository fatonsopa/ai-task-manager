"""Authored protocol controls and real private files; never fresh native acceptance."""

import hashlib
import importlib.metadata
import json
import platform
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
    ProfileSources,
    cases,
    prepare_profile,
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
    home = state / "home"
    home.mkdir(mode=0o700)
    profile_dir = home / ".claude_roni"
    profile_dir.mkdir(mode=0o700)
    from ai_task_manager.adapters.hosts.bridge.policy import clean_environment

    manifest.environment = clean_environment(home, claude_profile=profile_dir)
    manifest.data.update(
        home=str(home),
        profile=str(profile_dir),
        executable="/usr/bin/true",
        executable_sha256=hashlib.sha256(Path("/usr/bin/true").read_bytes()).hexdigest(),
    )
    source = state / "source.py"
    source.write_text("fixed recipe source")
    pins = ProfileSources(
        {"product/recipe": (source, hashlib.sha256(source.read_bytes()).hexdigest())},
        {
            "python": platform.python_version(),
            "claude-agent-sdk": importlib.metadata.version("claude-agent-sdk"),
        },
    )
    stable = prepare_profile(manifest, pins, "2.1.301")
    scope = Scope("linear", "workspace", "principal", "project", "session")
    info = root.stat()
    context = ProbeContext(
        "authored-probe",
        manifest.digest,
        stable.fingerprint,
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
        profile=stable,
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


class FixtureEnrollment:
    """Authored custody control; not actual producer intake or native acceptance."""

    def __init__(self, journal, ref, profile, valid_until=1200):
        self.row = {
            "reference": ref,
            "capture_hash": digest(journal._read(ref, fresh=False)),
            "profile_digest": profile.fingerprint,
            "revoked": False,
            "valid_until": valid_until,
        }

    def require(self, ref, profile):
        if (ref, profile) != (self.row["reference"], self.row["profile_digest"]):
            raise AccessDenied("Authored enrollment mismatch")
        return dict(self.row)

    def revoke(self, ref):
        assert ref == self.row["reference"]
        self.row["revoked"] = True


def certificate(r, valid_until=1200):
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
            "version": 2,
            "profile": asdict(r.profile),
            "profile_digest": r.profile.fingerprint,
            "manifest_digest": r.manifest.digest,
            "source": r.pins.fingerprint,
            "context_ref": r.context.reference,
            "owner": r.owner,
            "startup_ref": startup,
            "cases": values,
            "confirmed_stop": True,
            "stop": {"confirmed": True, "observation": {"known": True, "members": []}},
            "provenance": {
                "manifest_digest": r.manifest.digest,
                "probe_deadline": r.context.deadline,
                "context": {**asdict(r.context), "fixture_root": str(r.context.fixture_root)},
            },
            "error_type": None,
        },
    )
    enrollment = FixtureEnrollment(r.journal, ref, r.profile, valid_until)
    return NormalConformance(r.manifest, r.pins, r.context, r.journal, ref, enrollment=enrollment)


@pytest.mark.parametrize("index", range(11))
def test_fixed_canary_facts_require_actual_calls_and_results(rig, index):
    c = rig.cases[index]
    v = observed(rig, c)
    verify_case(rig.journal, c, v, rig.owner, rig.context.reference, rig.session, rig.context.model)


def test_null_argument_is_explicit_in_prompt_and_required_at_dispatch(rig):
    from jsonschema.exceptions import ValidationError

    case = next(case for case in rig.cases if case.case_id == "allow_write")
    assert '"expected_hash": null' in case.prompt
    assert "Every displayed argument key is required" in case.prompt
    assert "including keys with explicit null values; do not omit them" in case.prompt
    rig.gateway.begin(case)
    before = rig.gateway.snapshot()
    incomplete = {key: value for key, value in case.arguments.items() if key != "expected_hash"}
    with pytest.raises(ValidationError, match="expected_hash.*required"):
        rig.gateway.call(case.tool, incomplete)
    assert rig.gateway.snapshot() == before
    assert not (rig.root / "output.txt").exists()


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
        rig.pins.files[0][1].write_text("changed source")
    elif change == "deadline":
        rig.now[0] = 1200
    elif change == "reference":
        normal.reference = "host-record:unknown"
    else:
        rig.manifest.data["host"] = "codex"
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
    original = AccessDenied("private exception message and credential sentinel")

    def start(*args):
        raise original

    capture = ConformanceCapture(
        rig.manifest, rig.pins, rig.context, rig.authority, rig.journal, profile=rig.profile
    )
    # Use a distinct fresh canary; constructor must never overwrite an old input.
    (rig.root / "input.txt").unlink()
    with pytest.raises(AccessDenied) as raised:
        capture.run(SimpleNamespace(start=start))
    assert raised.value is original
    payload = rig.journal._read(capture.report_ref)["payload"]
    assert payload["confirmed_stop"] is False and payload["stop"] is None
    failure = payload["failure"]
    assert failure["stage"] == "managed_execution"
    assert failure["causes"][0]["code"] == "access_denied"
    assert "private exception" not in json.dumps(payload)
    assert all("message" not in cause and "locals" not in cause for cause in failure["causes"])


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
        p["provenance"]["probe_deadline"] += 1
    ref = rig.journal._append("trace", p)
    altered = NormalConformance(
        rig.manifest,
        rig.pins,
        rig.context,
        rig.journal,
        ref,
        enrollment=FixtureEnrollment(rig.journal, ref, rig.profile),
    )
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

    home = Path(rig.manifest.data["home"])
    profile = Path(rig.manifest.data["profile"])
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
    normal = NormalConformance(
        rig.manifest,
        rig.pins,
        rig.context,
        rig.journal,
        ref,
        enrollment=FixtureEnrollment(rig.journal, ref, rig.profile),
    )
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


@pytest.mark.parametrize("execution_fails", [False, True])
def test_capture_retains_first_failure_when_owned_stop_also_fails(rig, execution_fails):
    from tests.hosts.conformance.test_profile import AuthoredFactory

    first = AccessDenied("private execution sentinel")
    stopping = RuntimeError("private stop sentinel")

    class FailingFactory(AuthoredFactory):
        def execute(self, context, native, case):
            if execution_fails:
                raise first
            return super().execute(context, native, case)

        def stop(self, context, native):
            super().stop(context, native)
            raise stopping

    (rig.root / "input.txt").unlink()
    capture = ConformanceCapture(
        rig.manifest, rig.pins, rig.context, rig.authority, rig.journal, profile=rig.profile
    )
    with pytest.raises((AccessDenied, RuntimeError)) as raised:
        capture.run(FailingFactory(rig))
    assert raised.value is (first if execution_fails else stopping)
    payload = rig.journal._read(capture.report_ref)["payload"]
    assert payload["failure"]["stage"] == (
        "managed_execution" if execution_fails else "managed_stop"
    )
    assert payload["failure"]["causes"][0]["code"] == (
        "access_denied" if execution_fails else "runtime_error"
    )
    assert not payload["confirmed_stop"]
    assert "private execution sentinel" not in json.dumps(payload)
    assert "private stop sentinel" not in json.dumps(payload)
    with pytest.raises(AccessDenied):
        capture.completed()
