"""Actual guarded native/Core graph with a credential-free authored CLI verdict."""

import hashlib
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from ai_task_manager.adapters.hosts.bridge.auth_probe import claude_account_hash
from ai_task_manager.adapters.hosts.bridge.controller import ManagedHostFactory
from ai_task_manager.adapters.hosts.bridge.executions import ModelWork, ReviewedExecutions
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.adapters.proofs.store import RUNTIME_KINDS, RuntimeProofs
from ai_task_manager.adapters.workflow_reviews.native import NativeReviewCapture, review_prompt
from ai_task_manager.config.files import write_private
from ai_task_manager.domains.work.models import CapacityPolicy, TaskProse
from ai_task_manager.installation.native import NativeManifest, resolve
from ai_task_manager.installation.native.manifest import installation_intent
from ai_task_manager.installation.native.normal import NormalAuthoritySource, enroll
from ai_task_manager.installation.native.store import NativeStore
from ai_task_manager.installation.workflows.composition import NativeContext, NoCommands
from ai_task_manager.supervisor.managed import ManagedWorkers
from tests.hosts.test_continuity import AuthoredDependencyCollection
from tests.native_installation.helpers import bundle, refresh_digest
from tests.native_installation.test_normal import AuthoredRestrictions
from tests.runtime.helpers import ProviderFixture
from tests.scheduling.helpers import known_declaration


def reviewer(author, root, verdict=None):
    root.mkdir(mode=0o700)
    r, runtime = author.r, author.runtime
    r.now = time.time()
    r.capacity = CapacityPolicy("claude", 2, 2)
    r.core = r.new_core()
    inspection = author.artifacts.prepare(author.scope, author.task, author.saved["digest"])
    prompt = review_prompt(inspection)
    prose = TaskProse("Independent artifact review", prompt, False)
    task = r.core.intake.submit(r.handle, prose, r.approval(prose), r.key())
    task = r.confirm(task)
    task = r.core.transitions.transition(
        r.handle,
        task.id,
        "Todo",
        "enqueue",
        task.version,
        task.decision_revision,
        r.decision(task, "Todo"),
        {},
        r.key(),
    )
    task = r.confirm(task)
    r.known_dependencies[task.id] = ((), "normal")
    known_declaration(r, task)
    b = bundle(root / "native", "claude", "claude-opus-5-5", task_id=task.id)
    executable = root / "authored-review-cli"
    data = verdict or {
        "context_digest": inspection.digest,
        "criteria": [
            {"id": "request", "judgment": "accept", "reason": "Authored review observation"}
        ],
        "reason": "Authored protocol fixture, no actual model judgment",
    }
    source = Path(__file__).parents[1].joinpath("hosts/fixture_claude_bootstrap.py").read_text()
    source = source.replace(
        '"result": "READY",',
        '"result": (json.dumps('
        + repr(data)
        + ') if "Independently review" in json.dumps(value) else "READY"),',
    )
    executable.write_text("#!" + sys.executable + "\n" + source)
    executable.chmod(0o700)
    store = NativeStore.create(root / "native.sqlite")
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
        identity_refreshes=(),
        conformance_ref="authored-not-live",
    )
    source = NormalAuthoritySource(manifest, conformance=AuthoredRestrictions())
    registration = resolve(b.config, b.installation, authority_source=source).require(
        "normal-native"
    )
    journal = HostJournal(root / "host.sqlite", root / "host.key")
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
        root / "hosts",
        authority.policy_builder,
        authority.launch_permits,
        {"claude": b.data["account_hash"]},
        model_profiles={"claude": registration.profile},
    )
    proofs = RuntimeProofs(
        runtime.journal, root / "runtime.key", host_startups=factory.startup_verifier
    )
    r.core.context.evidence = EvidenceRouter(
        [
            EvidenceRoute("runtime-proof:", RUNTIME_KINDS, proofs),
            EvidenceRoute(
                "fixture:", frozenset({"provider_status", "decision", "human_decision"}), r.proofs
            ),
        ]
    )
    executions = ReviewedExecutions(
        journal,
        {"review": ModelWork(task.id, "claude", prompt, "assignment")},
        authority.model_accounting,
        NoCommands(),
    )
    workers = ManagedWorkers(
        r.core,
        runtime.journal,
        runtime.resources,
        runtime.runtime,
        proofs,
        ProviderFixture(r),
        factory,
        deadline=spec["deadline"],
        principal_ref="authored-principal",
        dependencies=AuthoredDependencyCollection(r.core, r.handle),
        executions=executions,
    )
    factory.commands = workers

    def decision(scope, current, target, action):
        return r.decision(current, target)

    producer = NativeReviewCapture(workers, r.handle, task.id, registration, "review", decision)
    return inspection, producer
