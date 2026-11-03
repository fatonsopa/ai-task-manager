"""Normal controller mechanics; authored restrictions never prove live conformance."""

import json
import os
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest

from ai_task_manager.adapters.hosts.bridge.invocations import InvocationBinding, InvocationPermits
from ai_task_manager.application.service.models import Unavailable
from ai_task_manager.config.files import write_private
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import Scope
from ai_task_manager.installation.native import NativeManifest, resolve
from ai_task_manager.installation.native.normal import NormalAuthoritySource, enroll
from ai_task_manager.installation.native.store import NativeStore
from tests.native_installation.helpers import bundle, refresh_digest


class AuthoredRestrictions:
    """Local control only: never accepted as a genuine normal release observation."""

    def require_installation(self, manifest):
        manifest.check()

    def require_native_conformance(self, *args):
        raise AccessDenied("Authored controls cannot establish actual native conformance")


@pytest.fixture
def normal(tmp_path):
    b = bundle(tmp_path)
    store = NativeStore.create(tmp_path / "authority.sqlite")
    installation = json.loads(b.installation.read_text())
    installation["scope"] = asdict(Scope("linear", "workspace", "principal", "project", "session"))
    write_private(b.installation, installation, replace=True)
    from ai_task_manager.installation.native.manifest import installation_intent

    b.data.update(authority_store=store.pointer, installation=installation_intent(b.installation))
    write_private(b.manifest, b.data, replace=True)
    refresh_digest(b.installation, b.manifest, b.data)
    manifest = NativeManifest(b.manifest, b.config, b.installation)
    enrolled = enroll(
        store, manifest, {"host": 30, "relay": 30, "auth": 5, "neutral": 8, "assignment": 8}
    )
    source = NormalAuthoritySource(manifest, conformance=AuthoredRestrictions())
    permission = source.resolve(enrolled["reference"], manifest)
    return b, store, manifest, source, permission


def reservation(permission, purpose="host", parent=None, **scope_changes):
    p = permission.plan
    return {
        "label": "Authored " + purpose,
        "paid": False,
        "max_cost_usd": None,
        "uses_worker": purpose == "host",
        "batch_id": p.batch_id,
        "timeout_seconds": 3,
        "metadata": {
            "parent_call_id": parent,
            "billing_basis_ref": p.billing_basis_ref,
            "invocation_binding": {
                "attempt_id": "attempt",
                "host": p.host,
                "configuration_digest": "a" * 64,
                "model": p.model,
                "effort": p.effort,
                "purpose": purpose,
                **scope_changes,
            },
        },
    }


def claim(c, key):
    return c.claim_launch(
        key, worker_identity="worker", not_after=c.clock() + timedelta(seconds=10)
    )


def start(c, key):
    with c.launch_boundary(key, worker_identity="worker") as report:
        # A trusted authored callback records no physical process in this local control.
        report(process_pid=None, process_start_identity=None)


def test_N201_N202_real_store_restart_and_default_release_refusal(normal):
    b, store, manifest, _, permission = normal
    permission.check(manifest, "task", permission.plan.deadline)
    assert (
        NativeStore(store.path, store.pointer).permission(permission.reference)
        == permission.controller.permission
    )
    with pytest.raises(Unavailable, match="conformance capture is not implemented"):
        resolve(b.config, b.installation)
    # A missing real policy is rejected before the independent conformance port.
    with pytest.raises(AccessDenied, match="policy, scope"):
        permission.admissions.require_native_conformance(None, None, None)


def test_N205_existing_capability_consumer_uses_actual_controller(normal):
    _, _, _, _, p = normal
    c = p.controller
    row = c.reserve("host", **reservation(p))
    assert row == c.reserve("host", **reservation(p))
    claimed = claim(c, "host")
    observed = InvocationBinding.observed(
        {"run_id": c.snapshot()["run_id"], "policy_revision": 1, "call": claimed}
    )
    permits = InvocationPermits(c, {"host": observed})
    launch = permits.prepare_launch("host")
    with launch.launch_boundary() as report:
        report(process_pid=None, process_start_identity=None)
    with launch.running().effect_boundary():
        pass
    with pytest.raises(AccessDenied):
        claim(c, "host")
    with pytest.raises(AccessDenied):
        launch.launch_boundary().__enter__()
    assert c.snapshot()["calls"][0]["status"] == "running"


@pytest.mark.parametrize("fault", ["paid", "timeout", "model", "effort", "batch", "parent"])
def test_N213_invalid_normal_reservations_or_child_claims_refuse(normal, fault):
    _, _, _, _, p = normal
    c = p.controller
    kwargs = reservation(p)
    if fault == "paid":
        kwargs["paid"] = True
    elif fault == "timeout":
        kwargs["timeout_seconds"] = float("inf")
    elif fault in {"model", "effort"}:
        kwargs["metadata"]["invocation_binding"][fault] = "other"
    elif fault == "batch":
        kwargs["batch_id"] = "foreign"
    else:
        kwargs = reservation(p, "relay", "missing-parent")
    with pytest.raises(AccessDenied):
        c.reserve("bad", **kwargs)
        claim(c, "bad")
    assert all(row["status"] != "running" for row in c.snapshot()["calls"])


def test_N206_parent_identity_and_original_deadline_bound(normal):
    _, _, _, _, p = normal
    c = p.controller
    c.reserve("host", **reservation(p))
    parent = claim(c, "host")
    c.reserve("relay", **reservation(p, "relay", "host"))
    child = claim(c, "relay")
    assert (
        child["invocation_bound"]["execution_deadline"]
        <= parent["invocation_bound"]["execution_deadline"]
    )
    c.reserve("foreign", **reservation(p, "auth", "host", attempt_id="foreign"))
    with pytest.raises(AccessDenied, match="another or stopped parent"):
        claim(c, "foreign")


@pytest.mark.parametrize("report_then_raise", [False, True])
def test_N208_uncertain_launch_is_not_replayed_or_released(normal, report_then_raise):
    _, _, _, _, p = normal
    c = p.controller
    c.reserve("host", **reservation(p))
    claim(c, "host")
    with pytest.raises((AccessDenied, OSError)):
        with c.launch_boundary("host", worker_identity="worker") as report:
            if report_then_raise:
                report(process_pid=None, process_start_identity=None)
                raise OSError("authored reply lost after effect")
    row = c.snapshot()["calls"][0]
    assert row["status"] == "launching" and not row["process_stopped"]
    assert row["launch_token"] is not None
    with pytest.raises(AccessDenied, match="never replay"):
        with c.launch_boundary("host", worker_identity="worker"):
            pytest.fail("Uncertain launch was repeated")
    with pytest.raises(AccessDenied, match="occupied"):
        c.reserve("another", **reservation(p))


def test_N209_unknown_stop_keeps_slot_and_child_obligation(normal):
    _, _, _, _, p = normal
    c = p.controller
    c.reserve("host", **reservation(p))
    claim(c, "host")
    start(c, "host")
    c.reserve("relay", **reservation(p, "relay", "host"))
    claim(c, "relay")
    c.record_outcome(
        "host",
        outcome="unknown",
        process_stopped=False,
        evidence_ref="host-record:authored-observation",
    )
    with pytest.raises(AccessDenied, match="occupied"):
        c.reserve("other", **reservation(p))
    c.record_outcome(
        "host", outcome="unknown", process_stopped=True, evidence_ref="host-record:authored-stop"
    )
    with pytest.raises(AccessDenied, match="occupied"):
        c.reserve("other", **reservation(p))
    c.record_outcome(
        "relay",
        outcome="failed",
        process_stopped=True,
        evidence_ref="host-record:authored-relay-stop",
    )
    assert c.reserve("other", **reservation(p))["status"] == "reserved"


def test_N210_producer_abort_rejects_started_or_changed_claim(normal):
    _, _, _, _, p = normal
    c = p.controller
    c.reserve("host", **reservation(p))
    claimed = claim(c, "host")
    start(c, "host")
    with pytest.raises(AccessDenied, match="Started or changed"):
        c._abort_unreturned_claims(
            [claimed], worker_identity="worker", evidence_ref="host-record:authored-no-return"
        )
    assert not c.snapshot()["calls"][0]["process_stopped"]


def test_N210_actual_pre_return_producer_abort_preserves_history(normal):
    _, _, _, _, p = normal
    c = p.controller
    c.reserve("host", **reservation(p))
    claimed = claim(c, "host")
    c._abort_unreturned_claims(
        [claimed], worker_identity="worker", evidence_ref="host-record:authored-no-return"
    )
    row = c.snapshot()["calls"][0]
    assert row["status"] == "failed" and row["process_stopped"]
    with c.store.transaction() as db:
        assert db.execute("select kind from native_events order by sequence").fetchall()[-1] == (
            "unreturned_abort",
        )


def test_N203_revoke_denies_new_effect_but_retains_stop_settlement(normal):
    _, store, _, _, p = normal
    c = p.controller
    c.reserve("host", **reservation(p))
    claim(c, "host")
    start(c, "host")
    store.revoke(p.reference)
    with pytest.raises(AccessDenied):
        with c.effect_boundary("host", worker_identity="worker"):
            pytest.fail("Revoked permission entered effect")
    c.record_outcome(
        "host", outcome="failed", process_stopped=True, evidence_ref="host-record:authored-stop"
    )
    assert c.snapshot()["calls"][0]["process_stopped"]


def test_N203_expiry_denies_new_invocation_without_erasing_history(normal):
    _, _, _, _, p = normal
    c = p.controller
    c.reserve("host", **reservation(p))
    c.clock = lambda: datetime.fromtimestamp(p.plan.deadline + 1, UTC)
    with pytest.raises(AccessDenied):
        claim(c, "host")
    # Replacing an owner clock also invalidates effects, independently of expiry.
    assert p.controller.store.permission(p.reference)["revoked"] is False


def test_N204_native_store_same_bytes_replacement_refuses(normal, tmp_path):
    _, store, _, _, _ = normal
    copy = tmp_path / "copied.sqlite"
    copy.write_bytes(store.path.read_bytes())
    copy.chmod(0o600)
    os.replace(copy, store.path)
    with pytest.raises(AccessDenied):
        store.permission("operator-permission")


@pytest.fixture
def actual_accounting_graph(tmp_path):
    from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
    from ai_task_manager.adapters.hosts.bridge.records import HostBinding
    from ai_task_manager.domains.work.models import CapacityPolicy
    from ai_task_manager.installation.workflows.composition import NativeContext
    from ai_task_manager.supervisor.journal import RuntimeJournal
    from tests.domains.helpers import Rig

    root = tmp_path / "graph"
    root.mkdir(mode=0o700)
    core = Rig(root / "core.sqlite")
    core.now = datetime.now(UTC).timestamp()
    core.capacity = CapacityPolicy("claude", 2, 2)
    core.core = core.new_core()
    task = core.queued()
    attempt = core.core.execution.reserve(
        core.handle,
        task.id,
        "claude",
        task.version,
        task.decision_revision,
        core.decision(task, "In Progress"),
        "normal-accounting-reserve",
    )
    b = bundle(root / "native", "claude", "claude-opus-5-5", task_id=task.id)
    store = NativeStore.create(root / "authority.sqlite")
    installation = json.loads(b.installation.read_text())
    installation["scope"] = asdict(core.scope)
    write_private(b.installation, installation, replace=True)
    from ai_task_manager.installation.native.manifest import installation_intent

    b.data.update(authority_store=store.pointer, installation=installation_intent(b.installation))
    write_private(b.manifest, b.data, replace=True)
    refresh_digest(b.installation, b.manifest, b.data)
    manifest = NativeManifest(b.manifest, b.config, b.installation)
    enroll(store, manifest, {"host": 30, "relay": 30, "auth": 5, "neutral": 8, "assignment": 8})
    source = NormalAuthoritySource(manifest, conformance=AuthoredRestrictions())
    registry = resolve(b.config, b.installation, authority_source=source)
    journal = HostJournal(root / "host.sqlite", root / "host.key")
    runtime = RuntimeJournal(root / "runtime.sqlite")
    deadline = core.now + 25
    owner = runtime.claim(attempt, asdict(core.scope), deadline=deadline)
    context = NativeContext(
        core.core, core.handle, task, runtime, journal, None, None, installation["deadline"]
    )
    authority = registry.require("normal-native").prepare(context)
    # A real policy uses a disposable empty directory; it is never launched.
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700)
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Pair:
        worktree: object

    policy = authority.policy_builder("claude", Pair(workspace))
    session, scope = core.core.session_binding(core.handle)
    binding = HostBinding(
        scope,
        session,
        attempt.id,
        owner["worker_id"],
        "claude",
        "principal",
        policy.configuration_digest,
        deadline,
    )
    return authority, policy, binding, journal, store, manifest


def test_N212_actual_core_and_runtime_feed_existing_host_accounting(actual_accounting_graph):
    from ai_task_manager.adapters.hosts.bridge.accounting import HostInvocationAccounting

    a, policy, binding, _, _, _ = actual_accounting_graph
    assert type(a.launch_permits) is HostInvocationAccounting
    initialization = a.launch_permits.authorize_initialization(policy, binding)
    assert initialization.native.binding.attempt_id == binding.attempt_id
    assert initialization.relay.binding.deadline <= initialization.native.binding.deadline
    rows = a.launch_permits.control.snapshot()["calls"]
    assert len(rows) == 2 and all(r["status"] == "launching" for r in rows)
    assert rows[0]["uses_worker"] and not rows[1]["uses_worker"]


@pytest.mark.parametrize("fault", [None, "account", "source", "binding", "stale", "manifest-flag"])
def test_N214_N215_authentication_requires_exact_owned_source_chain(actual_accounting_graph, fault):
    a, policy, binding, journal, _, manifest = actual_accounting_graph
    admissions = a.launch_permits.admissions
    # Authored producer records exercise the actual signed journal consumer; these
    # records are not genuine model/account/conformance acceptance evidence.
    group = {"confirmed_stop": True, "observation": {"known": True, "live_pids": []}}
    auth = journal._append(
        "trace",
        {
            "auth_method": "claude.ai",
            "account_hash": "b" * 64 if fault == "account" else manifest.data["account_hash"],
            "configuration_digest": binding.configuration_digest,
            "owned_group": group,
        },
        expires_at=journal.clock() + 30,
    )
    native = journal._append(
        "trace",
        {
            "attempt_id": binding.attempt_id,
            "worker_id": binding.worker_id,
            "configuration_digest": binding.configuration_digest,
            "process": {"pid": 123456, "start_identity": "authored identity"},
            "frame": {"type": "system", "subtype": "init", "apiKeySource": "none"},
        },
    )
    data = {**asdict(binding), "process_id": 123456, "process_start_identity": "authored identity"}
    if fault == "binding":
        data["attempt_id"] = "foreign"
    sources = (auth,) if fault == "source" else (auth, native)
    if fault != "manifest-flag":
        journal._append("startup", data, sources=sources, expires_at=journal.clock() + 30)
    if fault == "stale":
        now = journal.clock()
        journal.clock = lambda: now + 61
    if fault is None:
        admissions.verify_authenticated(policy, binding, journal)
    else:
        with pytest.raises(AccessDenied):
            admissions.verify_authenticated(policy, binding, journal)


def test_N217_same_sealed_manifest_reread_allowed_but_replaced_source_refused(normal, tmp_path):
    b, _, original, source, _ = normal
    current = NativeManifest(b.manifest, b.config, b.installation)
    assert (
        current is not original
        and source.resolve("operator-permission", current).manifest_digest == original.digest
    )
    copy = tmp_path / "same-bytes.json"
    copy.write_bytes(b.manifest.read_bytes())
    copy.chmod(0o600)
    os.replace(copy, b.manifest)
    with pytest.raises(AccessDenied):
        source.resolve("operator-permission", current)


def test_N203_unchanged_clock_advance_expires_admission_without_losing_history(normal):
    from ai_task_manager.installation.native.controller import NormalInvocationController

    _, store, _, _, p = normal

    class Clock:
        value = datetime.now(UTC)

        def __call__(self):
            return self.value

    clock = Clock()
    c = NormalInvocationController(store, p.controller.permission, clock=clock)
    c.reserve("host", **reservation(p))
    clock.value = datetime.fromtimestamp(p.plan.deadline + 1, UTC)
    with pytest.raises(AccessDenied, match="expired"):
        claim(c, "host")
    assert c.snapshot()["calls"][0]["status"] == "reserved"
