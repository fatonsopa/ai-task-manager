"""Current Core and normal accounting over an actual owned authored CLI only."""

import hashlib
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

from ai_task_manager.adapters.hosts.bridge.auth_probe import claude_account_hash
from ai_task_manager.adapters.hosts.bridge.controller import ManagedHostFactory
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.hosts.bridge.policy import NativeProfile
from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.adapters.proofs.store import RUNTIME_KINDS, RuntimeProofs
from ai_task_manager.config.files import write_private
from ai_task_manager.domains.work.models import CapacityPolicy
from ai_task_manager.installation.native import NativeManifest, resolve
from ai_task_manager.installation.native.manifest import installation_intent
from ai_task_manager.installation.native.normal import NormalAuthoritySource, enroll
from ai_task_manager.installation.native.store import NativeStore
from ai_task_manager.installation.workflows.composition import NativeContext
from ai_task_manager.supervisor.managed import ManagedWorkers
from tests.hosts.test_continuity import AuthoredDependencyCollection
from tests.native_installation.helpers import bundle, refresh_digest
from tests.native_installation.test_normal import AuthoredRestrictions
from tests.runtime.helpers import ProviderFixture, RuntimeRig


def test_N326_current_core5_normal_worker_uses_finite_same_native_identity(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    runtime = RuntimeRig(root)
    r = runtime.rig
    r.now = time.time()
    r.capacity = CapacityPolicy("claude", 2, 2)
    r.core = r.new_core()
    task = r.queued(code=False)
    b = bundle(tmp_path / "native", "claude", "claude-opus-5-5", task_id=task.id)
    executable = tmp_path / "authored-cli"
    executable.write_text(
        "#!"
        + sys.executable
        + "\n"
        + Path(__file__).parents[1].joinpath("hosts/fixture_claude_bootstrap.py").read_text()
    )
    executable.chmod(0o700)
    store = NativeStore.create(tmp_path / "normal.sqlite")
    spec = json.loads(b.installation.read_text())
    spec["scope"] = asdict(r.scope)
    write_private(b.installation, spec, replace=True)
    b.data.update(
        authority_store=store.pointer,
        installation=installation_intent(b.installation),
        executable=str(executable),
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        account_hash=claude_account_hash(
            {"email": "fixture@example.invalid", "orgId": "authored-org", "userId": "authored-user"}
        ),
    )
    write_private(b.manifest, b.data, replace=True)
    refresh_digest(b.installation, b.manifest, b.data)
    manifest = NativeManifest(b.manifest, b.config, b.installation)
    enroll(
        store,
        manifest,
        {"host": 35, "relay": 35, "auth": 5, "neutral": 8, "assignment": 8},
        identity_refreshes=("identity-1",),
        conformance_ref="authored-not-live",
    )
    source = NormalAuthoritySource(manifest, conformance=AuthoredRestrictions())
    registration = resolve(b.config, b.installation, authority_source=source).require(
        "normal-native"
    )
    journal = HostJournal(tmp_path / "host.sqlite", tmp_path / "host.key")
    context = NativeContext(
        r.core,
        r.handle,
        task,
        runtime.journal,
        journal,
        runtime.resources,
        runtime.runtime,
        spec["deadline"],
    )
    authority = registration.prepare(context)
    factory = ManagedHostFactory(
        r.core,
        runtime.resources,
        journal,
        tmp_path / "hosts",
        authority.policy_builder,
        authority.launch_permits,
        {"claude": b.data["account_hash"]},
        model_profiles={"claude": NativeProfile("claude-opus-5-5", "high")},
    )
    proofs = RuntimeProofs(
        runtime.journal, tmp_path / "runtime.key", host_startups=factory.startup_verifier
    )
    r.core.context.evidence = EvidenceRouter(
        [
            EvidenceRoute("runtime-proof:", RUNTIME_KINDS, proofs),
            EvidenceRoute(
                "fixture:", frozenset({"provider_status", "decision", "human_decision"}), r.proofs
            ),
        ]
    )
    workers = ManagedWorkers(
        r.core,
        runtime.journal,
        runtime.resources,
        runtime.runtime,
        proofs,
        ProviderFixture(r),
        factory,
        deadline=time.time() + 35,
        principal_ref="authored-principal",
        dependencies=AuthoredDependencyCollection(r.core, r.handle),
    )
    factory.commands = workers
    try:
        attempt = workers.start(
            r.handle,
            task.id,
            "claude",
            expected_version=task.version,
            decision_revision=task.decision_revision,
            decision_ref=r.decision(task, "In Progress"),
            operation_key="normal-current-core-start",
        )
        native = factory.owned[attempt.id]
        assert native.identity is not None
        before = native.identity.observations.prepare()
        native.identity.observations.refresh(native.directory / "actual-authored-refresh")
        before.check()
        r.core.execution.validate_work_lease(r.handle, attempt.id, attempt.version)
        refreshed = [
            row
            for row in authority.launch_permits.control.snapshot()["calls"]
            if row["call_id"].endswith(":identity-1")
        ]
        assert len(refreshed) == 1 and refreshed[0]["process_stopped"]
        assert refreshed[0]["metadata"]["invocation_binding"]["purpose"] == "auth"
        stopped = workers.stop(r.handle, attempt.id, operation_key="normal-current-core-stop")
        assert stopped.state == "paused" and not native.identity._thread.is_alive()
    finally:
        for native in factory.owned.values():
            if (
                native.process is not None
                and native.process.process is not None
                and not native.process._closed
            ):
                assert native.stop().confirmed
        runtime.close()
        report = os.environ.get("NATIVE_INSTALL_RESOURCE_REPORT")
        if report:
            with Path(report).open("a") as out:
                out.write(
                    json.dumps(
                        {
                            "normal_native": [
                                asdict(n.process.identity)
                                for n in factory.owned.values()
                                if n.process and n.process.identity
                            ],
                            "closed": [
                                n.process._closed for n in factory.owned.values() if n.process
                            ],
                        }
                    )
                    + "\n"
                )
