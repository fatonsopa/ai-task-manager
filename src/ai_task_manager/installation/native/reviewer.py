"""Default installed composition for one separately enrolled native reviewer."""

from dataclasses import asdict
from pathlib import Path

from ai_task_manager.adapters.hosts.bridge.controller import ManagedHostFactory
from ai_task_manager.adapters.hosts.bridge.executions import ModelWork, ReviewedExecutions
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.process.runtime import ProcessRuntime
from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.adapters.proofs.store import RUNTIME_KINDS, RuntimeProofs
from ai_task_manager.adapters.resources.git import git
from ai_task_manager.adapters.resources.manager import ResourceManager
from ai_task_manager.adapters.workflow_control.identity import file_binding
from ai_task_manager.adapters.workflow_reviews.native import NativeReviewCapture, review_prompt
from ai_task_manager.application.service.projection import require_owned
from ai_task_manager.config.files import read_private
from ai_task_manager.config.service import load_service
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.workflows.spec import Installation
from ai_task_manager.supervisor.journal import RuntimeJournal
from ai_task_manager.supervisor.managed import ManagedWorkers


class NormalReviewerFactory:
    """The JSON selects a sealed installation, never a callable or a verdict."""

    def __init__(self, author_manifest, *, service_options=None):
        self.author = author_manifest
        self.options = dict(service_options or {})
        if {"workflow_factory", "workers_factory"} & self.options.keys():
            raise AccessDenied("Reviewer composition owns its runtime ports")
        self.pointer = read_private(author_manifest.installation.path)["reviewer_installation"]
        self.producers = {}
        self._check_pointer()

    def _check_pointer(self):
        self.author.check()
        if self.pointer != read_private(self.author.installation.path).get("reviewer_installation"):
            raise AccessDenied("Reviewer pointer differs from sealed author installation")
        if type(self.pointer) is not dict or set(self.pointer) != {
            "path",
            "device",
            "inode",
            "sha256",
        }:
            raise AccessDenied("Exact physical reviewer installation required")
        if file_binding(self.pointer["path"]) != self.pointer:
            raise AccessDenied("Reviewer installation changed or was replaced")

    def __call__(self, inspection):
        from ai_task_manager.installation.workflows.composition import NativeContext, NoCommands

        from .resolver import resolve

        self._check_pointer()
        inspection.check()
        author = read_private(self.author.installation.path)
        spec = Installation(self.pointer["path"], self.author.config.path)
        d, context = spec.data, inspection.context
        if (
            d.get("role") != "reviewer"
            or d.get("reviewer_installation") is not None
            or d.get("review_target")
            != {
                "task_id": context["task_id"],
                "prose_digest": context["prose_digest"],
                "source_head": context["source_version"]["head"],
            }
            or context["task_id"] != author["task_id"]
            or context["prose_digest"] != author["prose_digest"]
            or d["task_id"] == author["task_id"]
            or d["scope"] != author["scope"]
            or d["scope"] != context["scope"]
            or d["deadline"] > author["deadline"]
            or d["state_root"] == author["state_root"]
            or d["repository"] != author["repository"]
            or d["source_head"] != author["source_head"]
        ):
            raise AccessDenied("Independent reviewer target, scope or finite authority differs")
        # A resumed lookup cannot create another worker for the same immutable inspection.
        if inspection.digest in self.producers:
            return self.producers[inspection.digest]
        registry = resolve(self.author.config.path, spec.seal.path, role="reviewer")
        registration = registry.require(d["native_ref"])
        if registration.host != "claude":
            raise AccessDenied("The existing independent native review producer is Claude-only")
        service = load_service(self.author.config.path, **self.options)
        core, handle = service.core, service.handle

        def current_author(connection=None):
            if connection is None:
                self._check_pointer()
                spec.check()
                inspection.check()
            current = core.get_task(handle, author["task_id"], connection=connection)
            require_owned(service, current, connection=connection)
            if (
                current.prose_digest != context["prose_digest"]
                or current.human_hold
                or current.delivered_once
                or current.observed_status not in {"In Progress", "In Review"}
                or current.sync_state != "confirmed"
            ):
                raise AccessDenied("Current author task cannot disclose review content")

        current_author()
        task = core.get_task(handle, d["task_id"])
        require_owned(service, task)
        if (
            asdict(service.scope) != d["scope"]
            or task.prose_digest != d["prose_digest"]
            or task.code_bearing
            or task.observed_status != "Todo"
            or task.sync_state != "confirmed"
            or task.human_hold
            or service.provider is None
            or service.dependencies is None
            or not isinstance(core.context.evidence, EvidenceRouter)
        ):
            raise AccessDenied("Current independently approved reviewer task and provider required")
        if git(Path(d["repository"]), "rev-parse", "HEAD") != d["source_head"]:
            raise AccessDenied("Reviewer repository source changed")
        root = Path(d["state_root"])
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or root.stat().st_mode & 0o077:
            raise AccessDenied("Reviewer state must be private and physical")
        journal = RuntimeJournal(root / "runtime.sqlite")
        host_journal = HostJournal(root / "host.sqlite", root / "host.key")
        resources = ResourceManager(
            journal,
            root / "resources",
            Path(d["repository"]),
            metadata_paths=(core.context.db.path, host_journal.path),
        )
        runtime = ProcessRuntime(journal, root / "outputs")
        authority = registration.prepare(
            NativeContext(
                core, handle, task, journal, host_journal, resources, runtime, d["deadline"]
            )
        )
        authority.require()

        def policy(host, pair):
            self._check_pointer()
            spec.check()
            current_author()
            registration.check()
            return authority.policy_builder(host, pair)

        factory = ManagedHostFactory(
            core,
            resources,
            host_journal,
            root / "native",
            policy,
            authority.launch_permits,
            {registration.host: registration.account_hash},
            model_profiles={registration.host: registration.profile},
        )
        proofs = RuntimeProofs(
            journal, root / "runtime.key", host_startups=factory.startup_verifier
        )
        if any(
            r.prefix == "runtime-proof:" or r.kinds & RUNTIME_KINDS
            for r in core.context.evidence.routes
        ):
            raise AccessDenied("Reviewer requires its own non-overlapping runtime proof context")
        core.context.evidence = EvidenceRouter(
            [*core.context.evidence.routes, EvidenceRoute("runtime-proof:", RUNTIME_KINDS, proofs)]
        )
        executions = ReviewedExecutions(
            host_journal,
            {
                "review": ModelWork(
                    task.id, registration.host, review_prompt(inspection), authority.accounting_ref
                )
            },
            authority.model_accounting,
            NoCommands(),
        )
        workers = ManagedWorkers(
            core,
            journal,
            resources,
            runtime,
            proofs,
            service.provider,
            factory,
            deadline=d["deadline"],
            principal_ref=d["principal_ref"],
            executions=executions,
            base=d["source_head"],
            dependencies=service.dependencies,
            decision_preparation=authority.decision_preparation,
        )
        factory.commands = workers

        def decision(scope, current, target, action):
            self._check_pointer()
            spec.check()
            current_author()
            return service.decisions.decision(
                scope, current, target, action, grant_ref=service.grant_ref
            )

        producer = NativeReviewCapture(
            workers,
            handle,
            task.id,
            registration,
            "review",
            decision,
            current_author=current_author,
        )
        self.producers[inspection.digest] = producer
        return producer
