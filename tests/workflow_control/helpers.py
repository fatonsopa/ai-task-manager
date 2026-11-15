"""Actual owned local lifecycle; provider observations are authored test controls."""

import os
import signal
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4


def owner_main(root, socket_root, *, drop_first=False, max_steps=16, barrier=False, recovery=False):
    from ai_task_manager.adapters.decisions.authority import HostGrant, HumanInstruction
    from ai_task_manager.adapters.decisions.journal import AuthorityJournal
    from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
    from ai_task_manager.adapters.workflow_control.owner import WorkflowOwner
    from ai_task_manager.adapters.workflow_reviews.artifacts import CurrentArtifacts
    from ai_task_manager.adapters.workflow_reviews.issuer import WorkflowReviews
    from ai_task_manager.adapters.workflow_reviews.models import KINDS, PREFIX
    from ai_task_manager.adapters.workflows.repository import RepositoryInspector
    from ai_task_manager.adapters.workflows.runtime import LocalCommands, LocalSupervisorPort
    from ai_task_manager.application.workflows.driver import WorkflowDriver
    from ai_task_manager.application.workflows.models import WorkflowPlan, WorkflowPlans
    from ai_task_manager.application.workflows.reviews import ReviewedEvidence
    from ai_task_manager.application.workflows.service import WorkflowService
    from ai_task_manager.config.claim_store import enroll_claim_store
    from ai_task_manager.config.files import write_private
    from ai_task_manager.config.service import load_service
    from ai_task_manager.config.sessions import SessionFile
    from ai_task_manager.domains.work.models import ApprovalPolicy
    from ai_task_manager.storage.workflows import WorkflowStore
    from tests.application.helpers import Observations
    from tests.runtime.helpers import ProviderFixture, RuntimeRig

    root, socket_root = Path(root), Path(socket_root)
    os.umask(0o077)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    rr = RuntimeRig(root)
    r = rr.rig
    r.now = time.time()
    r.policy = ApprovalPolicy("a" * 64, "b" * 64, "c" * 64)
    r.core.context.approval_policy = r.policy
    task = r.queued(code=False)
    other = r.queued(code=False)
    authority = AuthorityJournal(root / "authority.sqlite", root / "authority.key")
    enroll_claim_store(r.db, authority)
    SessionFile(root / "session.json").save(r.handle, r.scope)
    config = {
        "version": 1,
        "core_database": str(r.db.path),
        "authority_database": str(authority.path),
        "authority_key": str(authority.key_path),
        "session_file": str(root / "session.json"),
        "approval_policy": asdict(r.policy),
        "workflow_control": {
            "record": str(socket_root / "owner.json"),
            "endpoint": str(socket_root / "owner.sock"),
            "instance": str(uuid4()),
        },
    }
    if recovery:
        config["grant_ref"] = "fixture-grant"
    config_file = root / "service.json"
    write_private(config_file, config)
    observations = Observations()
    observations.grants["fixture-grant"] = HostGrant(
        r.scope,
        r.scope.logical_session,
        ("start", "non_code_complete"),
        "authored-local-enrollment",
        time.time() + 120,
    )
    observations.instructions["fixture-hold"] = HumanInstruction(
        r.scope,
        task.id,
        "On Hold",
        0,
        "Authored local hold control",
        "a" * 64,
        prose_digest=task.prose_digest,
    )
    binding = {}
    deadline = time.time() + 50

    def compose(core, handle, decisions, provider, workers):
        r.core = core
        rr.supervisor.core = core
        program = "from pathlib import Path;Path('result.txt').write_text('observed owned result')"
        waiting = (
            "from pathlib import Path;import time;"
            f"Path({str(root / 'barrier-entered')!r}).write_text('ready');"
            f"release=Path({str(root / 'barrier-release')!r});"
            "end=time.monotonic()+10;"
            "\nwhile not release.exists() and time.monotonic()<end:time.sleep(.02)\n" + program
        )
        commands = LocalCommands(
            {
                "result": (
                    (sys.executable, "-I", "-c", waiting if barrier else program),
                    {"PATH": os.defpath},
                    12 if barrier else 2,
                ),
                "fast": ((sys.executable, "-I", "-c", program), {"PATH": os.defpath}, 2),
            }
        )
        runtime = LocalSupervisorPort(rr.supervisor, commands)
        store = WorkflowStore(root / "workflow.sqlite")
        import subprocess

        base = subprocess.check_output(
            ["git", "-C", str(rr.repo), "rev-parse", "HEAD"], text=True
        ).strip()
        plan = WorkflowPlan(
            "non-code",
            "result",
            "fixture",
            base,
            ("result.txt",),
            ("result.txt",),
            deadline - 1,
            max_steps,
        )
        from dataclasses import replace

        other_plan = replace(plan, id="other", command_ref="fast")
        plans = WorkflowPlans((plan, other_plan))
        inspector = RepositoryInspector(runtime)
        key = root / "review.key"
        key.write_bytes(os.urandom(32))
        key.chmod(0o600)
        artifacts = CurrentArtifacts(
            core,
            handle,
            store,
            inspector,
            runtime,
            {task.id: plan, other.id: other_plan},
            {
                task.id: (("complete", "Verify the observed result."),),
                other.id: (("complete", "Verify result."),),
            },
        )
        reviews = WorkflowReviews(artifacts, key)
        core.context.evidence = EvidenceRouter(
            [*core.context.evidence.routes, EvidenceRoute(PREFIX, KINDS, reviews)]
        )
        driver = WorkflowDriver(
            core,
            handle,
            store,
            plans,
            runtime=runtime,
            repository=inspector,
            provider=provider,
            decisions=lambda scope, t, target, action: decisions.decision(
                scope, t, target, action, grant_ref="fixture-grant"
            ),
            reviews=ReviewedEvidence(core, reviews.lookup),
        )
        binding.update(store=store, runtime=runtime)
        return WorkflowService(driver, {task.id: plan.id, other.id: other_plan.id})

    from ai_task_manager.adapters.proofs.store import RUNTIME_KINDS

    router = EvidenceRouter(
        [
            EvidenceRoute("runtime-proof:", RUNTIME_KINDS, rr.proofs),
            EvidenceRoute("fixture:", frozenset({"provider_status"}), r.proofs),
            EvidenceRoute("decision-proof:", frozenset({"decision", "human_decision"}), authority),
        ]
    )
    service = load_service(
        config_file,
        observations=observations,
        evidence_factory=lambda journal: router,
        provider_factory=lambda *args: ProviderFixture(r),
        scheduling_sources=r.scheduling_sources,
        workflow_factory=compose,
    )
    installation = None
    if recovery:
        from ai_task_manager.adapters.workflow_control.identity import file_binding
        from ai_task_manager.installation.workflows.spec import Installation

        # This authored selection configures the existing mechanical owner;
        # the product activation code, not this fixture, produces its witness.
        installation_file = root / "installation.json"
        plan = service.workflows.driver.plans.get("non-code")
        write_private(installation_file, {
            "version": 1, "config": file_binding(config_file), "scope": asdict(r.scope),
            "task_id": task.id, "prose_digest": task.prose_digest,
            "native_ref": "declared-mechanical-fixture", "native_digest": "0" * 64,
            "repository": str(rr.repo), "source_head": plan.base,
            "state_root": str(root), "deadline": deadline,
            "principal_ref": "authored-principal", "plan_ref": plan.id,
            "files": list(plan.allowed_files), "artifacts": list(plan.artifacts),
            "installation_path": str(installation_file),
        })
        installation = Installation(installation_file, config_file)
        write_private(root / "prepared.json", {"task_id": task.id, "plan_ref": plan.id})
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *args: stop.set())
    signal.signal(signal.SIGINT, lambda *args: stop.set())
    # Fault injection is confined to this authored transport fixture; product
    # never accepts a model-controlled drop/skip instruction.
    if drop_first:
        original = service.call
        once = [False]

        def lost(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[0] == "workflow_run" and not once[0]:
                once[0] = True
                raise OSError("authored lost reply after committed controller result")
            return result

        service.call = lost

    def ready():
        write_private(
            root / "ready.json",
            {
                "task_id": task.id,
                "other_task_id": other.id,
                "plan_ref": "non-code",
                "config": str(config_file),
                "module_source": __import__("ai_task_manager").__path__[0],
            },
        )

    try:
        WorkflowOwner(
            service, config_file, deadline=deadline, installation=installation
        ).serve(stop, ready)
    finally:
        rr.close()
        reports = []
        for worker in rr.runtime.workers.values():
            reports.append(
                {
                    "pid": worker.process.pid,
                    "group": worker.group.group_id,
                    "start_identity": worker.group.start_identity,
                    "returncode": worker.process.poll(),
                    "outcomes": rr.journal.observations(worker.attempt_id, "runtime_outcome"),
                }
            )
        write_private(
            root / "stop.json", {"workers": reports, "owner_pid": os.getpid(), "owner_closed": True}
        )


def entry():
    owner_main(
        sys.argv[1],
        sys.argv[2],
        drop_first="drop" in sys.argv[3:],
        max_steps=1 if "step" in sys.argv[3:] else 16,
        barrier="barrier" in sys.argv[3:],
        recovery="recovery" in sys.argv[3:],
    )
