"""Assemble real existing worker/provider ports; never an evaluation default."""

from dataclasses import asdict, dataclass, replace
from pathlib import Path

from ai_task_manager.adapters.hosts.bridge.controller import ManagedHostFactory
from ai_task_manager.adapters.hosts.bridge.executions import (
    ConfinedWork,
    ModelWork,
    ReviewedExecutions,
)
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.hosts.bridge.policy import NativePolicy
from ai_task_manager.adapters.process.runtime import ProcessRuntime
from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.adapters.proofs.store import RUNTIME_KINDS, RuntimeProofs
from ai_task_manager.adapters.resources.git import git
from ai_task_manager.adapters.resources.manager import ResourceManager
from ai_task_manager.adapters.workflow_control.owner import WorkflowOwner
from ai_task_manager.adapters.workflow_reviews.artifacts import CurrentArtifacts
from ai_task_manager.adapters.workflow_reviews.issuer import WorkflowReviews
from ai_task_manager.adapters.workflow_reviews.models import KINDS, PREFIX
from ai_task_manager.adapters.workflows.repository import RepositoryInspector
from ai_task_manager.adapters.workflows.runtime import ManagedWorkerPort
from ai_task_manager.application.service.models import Unavailable
from ai_task_manager.application.workflows.driver import WorkflowDriver
from ai_task_manager.application.workflows.models import WorkflowPlans
from ai_task_manager.application.workflows.reviews import ReviewedEvidence
from ai_task_manager.application.workflows.service import WorkflowService
from ai_task_manager.config.files import private_path
from ai_task_manager.config.service import load_service
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.storage.workflows import WorkflowStore
from ai_task_manager.supervisor.journal import RuntimeJournal
from ai_task_manager.supervisor.managed import ManagedWorkers

from .registry import NativeAuthorities, NativeRegistry
from .spec import Installation


@dataclass(frozen=True)
class NativeContext:
    core: object
    handle: object
    task: object
    runtime_journal: object
    host_journal: object
    resources: object
    runtime: object
    deadline: float


class NoCommands:
    def execute_registered(self, *args, **kwargs):
        raise AccessDenied("This installation registered only its managed model assignment")

    def reconcile_stopped(self, attempt_id):
        return True


class InstalledWorkflow:
    def __init__(
        self, config_file, installation_file, registry=None, *, service_options=None, reviews=None
    ):
        self.spec = spec = Installation(installation_file, config_file)
        # This gate precedes provider authentication or resource/native effects.
        self.registry = registry or NativeRegistry()
        self.native = self.registry.require(spec.data["native_ref"])
        if self.native.fingerprint != spec.data["native_digest"]:
            raise AccessDenied("Native account/model/source registration differs")
        if reviews is not None:
            raise Unavailable("Independent genuine review capture admission is not installed")
        from ai_task_manager.config.files import read_private

        configuration = read_private(config_file)
        control = configuration.get("workflow_control", {})
        if any(Path(control.get(name, "/__missing__")).exists() for name in ("record", "endpoint")):
            raise AccessDenied("Existing owner state requires verified recovery, never replacement")
        options = dict(
            self.registry.service_options if service_options is None else service_options
        )
        if {"workflow_factory", "workers_factory"} & options.keys():
            raise AccessDenied("Installation owns its worker and workflow composition")
        dependencies = options.get("dependencies_factory")
        if dependencies is None:
            from ai_task_manager.application.service.linear import LinearComponents
            from ai_task_manager.config.files import read_private

            config = read_private(config_file)
            if not config.get("linear"):
                raise Unavailable("Configured real provider and dependency integration required")
            components = options.get("linear_components") or LinearComponents(config["linear"])
            options["linear_components"] = components
            dependencies = components.dependencies

        def dependent(core, handle):
            self.dependencies = dependencies(core, handle)
            return self.dependencies

        options["dependencies_factory"] = dependent
        options["workflow_factory"] = self._compose
        self.service = load_service(config_file, **options)
        self.service.workers = self.workers
        self.service.dependencies = self.dependencies
        self.owner = WorkflowOwner(self.service, config_file, deadline=spec.data["deadline"])

    def _compose(self, core, handle, decisions, provider, unused):
        d = self.spec.data
        self.spec.check()
        self.native.check()
        task = core.get_task(handle, d["task_id"])
        if (
            asdict(core.session_binding(handle)[1]) != d["scope"]
            or task.prose_digest != d["prose_digest"]
            or task.code_bearing
        ):
            raise AccessDenied("Installed non-code task, scope or approved prose differs")
        if provider is None or self.dependencies is None:
            raise Unavailable("Real provider/dependency ports required before native execution")
        if not isinstance(core.context.evidence, EvidenceRouter):
            raise Unavailable("Explicit non-overlapping provider/decision evidence routes required")
        if git(Path(d["repository"]), "rev-parse", "HEAD") != d["source_head"]:
            raise AccessDenied("Installed repository source changed")
        root = Path(d["state_root"])
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.stat().st_mode & 0o077:
            raise AccessDenied("Installed worker state must be private")
        self.journal = RuntimeJournal(root / "runtime.sqlite")
        self.host_journal = HostJournal(root / "host.sqlite", root / "host.key")
        self.resources = ResourceManager(
            self.journal,
            root / "resources",
            Path(d["repository"]),
            metadata_paths=(core.context.db.path, root / "workflow.sqlite", self.host_journal.path),
        )
        self.processes = ProcessRuntime(self.journal, root / "outputs")
        context = NativeContext(
            core,
            handle,
            task,
            self.journal,
            self.host_journal,
            self.resources,
            self.processes,
            d["deadline"],
        )
        authority = self.native.prepare(context)
        if type(authority) is not NativeAuthorities:
            raise Unavailable("Trusted native preparation did not return actual capabilities")
        authority.require()

        def policy(host, pair):
            self.spec.check()
            self.native.check()
            value = authority.policy_builder(host, pair)
            if (
                not isinstance(value, NativePolicy)
                or value.host != self.native.host
                or (value.model, value.effort)
                != (self.native.profile.model, self.native.profile.effort)
            ):
                raise AccessDenied(
                    "Native policy differs from installed account/model registration"
                )
            return replace(value, seals=(*value.seals, self.spec.seal, self.spec.config_seal))

        self.factory = ManagedHostFactory(
            core,
            self.resources,
            self.host_journal,
            root / "native",
            policy,
            authority.launch_permits,
            {self.native.host: self.native.account_hash},
            model_profiles={self.native.host: self.native.profile},
        )
        proofs = RuntimeProofs(
            self.journal, root / "runtime.key", host_startups=self.factory.startup_verifier
        )
        prompt = (
            task.title
            + "\n\n"
            + task.body
            + "\n\nRequired work units: "
            + ", ".join(task.required_work)
        )
        executions = ReviewedExecutions(
            self.host_journal,
            {
                "assignment": ModelWork(
                    task.id, self.native.host, prompt, authority.accounting_ref
                ),
                **{ref: ConfinedWork(task.id, ref) for ref in authority.command_refs},
            },
            authority.model_accounting,
            authority.confined_commands or NoCommands(),
        )
        self.workers = ManagedWorkers(
            core,
            self.journal,
            self.resources,
            self.processes,
            proofs,
            provider,
            self.factory,
            deadline=d["deadline"],
            principal_ref=d["principal_ref"],
            executions=executions,
            base=d["source_head"],
            dependencies=self.dependencies,
            decision_preparation=authority.decision_preparation,
        )
        self.factory.commands = self.workers
        plan = self.spec.plan(self.native.host)

        def binding(current, selected):
            self.spec.check()
            self.native.check()
            if (
                current.id != task.id
                or current.prose_digest != d["prose_digest"]
                or selected != plan
            ):
                raise AccessDenied("Current native task/plan binding differs")
            return self.native.fingerprint + ":" + plan.digest

        runtime = ManagedWorkerPort(self.workers, binding)
        self.store = WorkflowStore(root / "workflow.sqlite")
        inspector = RepositoryInspector(runtime)
        # Full original approved prose, not a private expected-answer evaluator.
        criteria = {
            task.id: tuple((unit, task.title + "\n\n" + task.body) for unit in task.required_work)
        }
        key = root / "review.key"
        if not key.exists():
            import os

            with key.open("xb") as stream:
                key.chmod(0o600)
                stream.write(os.urandom(32))
        private_path(key)
        reviewer = WorkflowReviews(
            CurrentArtifacts(
                core, handle, self.store, inspector, runtime, {task.id: plan}, criteria
            ),
            key,
        )
        core.context.evidence = EvidenceRouter(
            [
                *core.context.evidence.routes,
                EvidenceRoute("runtime-proof:", RUNTIME_KINDS, proofs),
                EvidenceRoute(PREFIX, KINDS, reviewer),
            ]
        )
        from ai_task_manager.config.files import read_private

        grant = read_private(self.spec.config_seal.path).get("grant_ref")
        driver = WorkflowDriver(
            core,
            handle,
            self.store,
            WorkflowPlans((plan,)),
            runtime=runtime,
            repository=inspector,
            provider=provider,
            reviews=ReviewedEvidence(core, reviewer.lookup),
            decisions=lambda scope, current, target, action: decisions.decision(
                scope, current, target, action, grant_ref=grant
            ),
        )
        return WorkflowService(driver, {task.id: plan.id})

    def serve(self, stop=None, ready=None):
        return self.owner.serve(stop, ready)
