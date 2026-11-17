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
from ai_task_manager.config.files import private_path, read_private
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
    recovery: object = None
    code_history: object = None


class NoCommands:
    def execute_registered(self, *args, **kwargs):
        raise AccessDenied("This installation registered only its managed model assignment")

    def reconcile_stopped(self, attempt_id):
        return True


class InstalledWorkflow:
    def __init__(
        self,
        config_file,
        installation_file,
        registry=None,
        *,
        service_options=None,
        reviews=None,
        recovery=None,
    ):
        self.recovery = recovery
        if recovery is not None:
            from .cold import ColdRecovery

            if type(recovery) is not ColdRecovery:
                raise AccessDenied("Typed current original-owner recovery required")
            recovery.validate_stores()
        self.spec = spec = Installation(installation_file, config_file)
        # This gate precedes provider authentication or resource/native effects.
        self.registry = registry or NativeRegistry()
        self.native = self.registry.require(spec.data["native_ref"])
        self.code = (
            self.registry.require_code(spec.data["code_ref"]) if "code_ref" in spec.data else None
        )
        if self.code is not None and self.code.fingerprint != spec.data["code_digest"]:
            raise AccessDenied("Installed code policy digest differs")
        if self.native.fingerprint != spec.data["native_digest"]:
            raise AccessDenied("Native account/model/source registration differs")
        if reviews is not None:
            raise Unavailable("Independent genuine review capture admission is not installed")
        from ai_task_manager.config.files import read_private

        configuration = read_private(config_file)
        from ai_task_manager.adapters.workflow_control.identity import file_binding

        self.successor_mode = read_private(configuration["session_file"]).get("version") == 2
        self.successor_store = None
        if self.successor_mode:
            from ai_task_manager.adapters.decisions.journal import AuthorityJournal
            from ai_task_manager.adapters.workflow_control.recovery import physical
            from ai_task_manager.config.files import digest
            from ai_task_manager.installation.sessions.recovery import history

            authority = AuthorityJournal.open_existing(
                configuration["authority_database"], configuration["authority_key"]
            )
            retained = history(
                authority, "workflow-successor-store:" + digest(configuration["grant_ref"])
            )
            if retained is None or retained["config"] != file_binding(config_file):
                raise AccessDenied("Original successor installation history is unavailable")
            for binding in retained["files"].values():
                observed = (
                    file_binding(binding["path"])
                    if "sha256" in binding
                    else physical(binding["path"])
                )
                if observed != binding:
                    raise AccessDenied("Original successor journal or key changed")
            self.successor_store = WorkflowStore.open_existing(
                retained["workflow"]["path"], store_id=retained["workflow"]["store_id"]
            )

        control = configuration.get("workflow_control", {})
        if recovery is None and any(
            Path(control.get(name, "/__missing__")).exists() for name in ("record", "endpoint")
        ):
            raise AccessDenied("Existing owner state requires verified recovery, never replacement")
        options = dict(
            self.registry.service_options if service_options is None else service_options
        )
        if {"workflow_factory", "workers_factory", "existing_only"} & options.keys():
            raise AccessDenied("Installation owns its worker and workflow composition")
        dependencies = options.get("dependencies_factory")
        if dependencies is None:
            from ai_task_manager.application.service.linear import LinearComponents
            from ai_task_manager.config.files import read_private

            config = read_private(config_file)
            if not config.get("linear"):
                raise Unavailable("Configured real provider and dependency integration required")
            components = options.get("linear_components") or LinearComponents(
                config["linear"], existing_only=recovery is not None
            )
            options["linear_components"] = components
            dependencies = components.dependencies

        def dependent(core, handle):
            self.dependencies = dependencies(core, handle)
            return self.dependencies

        options["dependencies_factory"] = dependent
        options["workflow_factory"] = self._compose
        options["existing_only"] = recovery is not None or self.successor_mode
        self.service = load_service(config_file, **options)
        self.service.workers = self.workers
        self.service.dependencies = self.dependencies
        self.owner = WorkflowOwner(
            self.service,
            config_file,
            deadline=spec.data["deadline"],
            installation=spec,
            recovery=recovery,
        )

    def _compose(self, core, handle, decisions, provider, unused):
        d = self.spec.data
        self.spec.check()
        self.native.check()
        task = core.get_task(handle, d["task_id"])
        if (
            asdict(core.session_binding(handle)[1]) != d["scope"]
            or task.prose_digest != d["prose_digest"]
        ):
            raise AccessDenied(
                "Installed non-code task, scope or approved prose differs"
                if not task.code_bearing
                else "Installed task, scope or approved prose differs"
            )
        if task.code_bearing:
            if self.code is None:
                raise Unavailable("Code delivery requires an installed sealed code registration")
            self.code.require(core, handle, task)
        elif self.code is not None:
            raise AccessDenied("Non-code work cannot select an installed merge policy")
        if provider is None or self.dependencies is None:
            raise Unavailable("Real provider/dependency ports required before native execution")
        if not isinstance(core.context.evidence, EvidenceRouter):
            raise Unavailable("Explicit non-overlapping provider/decision evidence routes required")
        if git(Path(d["repository"]), "rev-parse", "HEAD") != d["source_head"]:
            raise AccessDenied("Installed repository source changed")
        root = Path(d["state_root"])
        if self.recovery is None:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.stat().st_mode & 0o077:
            raise AccessDenied("Installed worker state must be private")
        self.journal = (
            RuntimeJournal.open_existing if self.recovery or self.successor_mode else RuntimeJournal
        )(root / "runtime.sqlite")
        self.host_journal = (
            HostJournal.open_existing if self.recovery or self.successor_mode else HostJournal
        )(root / "host.sqlite", root / "host.key")
        successor_sources = {}
        completed_successors = set()
        from ai_task_manager.storage.sessions import TaskSession

        session_id, _ = core.session_binding(handle)
        if isinstance(session_id, TaskSession):
            retained_store = self.successor_store
            with core.context.db.transaction() as conn:
                enrolled = conn.execute(
                    "SELECT workflow_ref FROM task_capabilities WHERE reference=?",
                    (session_id.reference,),
                ).fetchone()
            successor_row = retained_store.read(enrolled[0], core.session_binding(handle)[1])
            prior_attempt = successor_row["succession"]["retained"]["body"].get("attempt_id")
            if prior_attempt:
                if core.get_attempt(handle, prior_attempt).state == "done":
                    completed_successors.add(prior_attempt)
                else:
                    successor_sources[task.id] = prior_attempt
        self.resources = ResourceManager(
            self.journal,
            root / "resources",
            Path(d["repository"]),
            metadata_paths=(core.context.db.path, root / "workflow.sqlite", self.host_journal.path),
            existing_only=self.recovery is not None or self.successor_mode,
            successor_sources=successor_sources,
        )
        self.resources.completed_successors = frozenset(completed_successors)
        control_parent = Path(
            read_private(self.spec.config_seal.path)["workflow_control"]["endpoint"]
        ).parent.resolve()
        self.processes = ProcessRuntime(
            self.journal,
            root / "outputs",
            recovery_directory=control_parent,
            existing_only=self.recovery is not None or self.successor_mode,
        )
        context = NativeContext(
            core,
            handle,
            task,
            self.journal,
            self.host_journal,
            self.resources,
            self.processes,
            d["deadline"],
            recovery=self.recovery,
        )
        authority = self.native.prepare(context)
        if type(authority) is not NativeAuthorities:
            raise Unavailable("Trusted native preparation did not return actual capabilities")
        authority.require()
        worker_git = authority.require_worker_git(self.journal) if task.code_bearing else None

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
            self.journal,
            root / "runtime.key",
            host_startups=self.factory.startup_verifier,
            existing_only=self.recovery is not None or self.successor_mode,
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
            reservation_recovery=(
                lambda original, current: self.recovery.retry_reservation(
                    self.service, self.journal, original, current
                )
            )
            if self.recovery
            else None,
        )
        self.factory.commands = self.workers
        plan = self.spec.plan(self.native.host)
        if task.code_bearing:
            if not authority.command_refs or not callable(
                getattr(authority.confined_commands, "verified_result", None)
            ):
                raise Unavailable("Code workflows require installed verified commit checks")
            plan = replace(plan, commit_checks=authority.command_refs)

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
        self.store = (
            self.recovery.store()
            if self.recovery
            else self.successor_store
            if self.successor_mode
            else WorkflowStore(root / "workflow.sqlite")
        )
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
        reviewer = None
        routes = [
            *core.context.evidence.routes,
            EvidenceRoute("runtime-proof:", RUNTIME_KINDS, proofs),
        ]
        if not task.code_bearing:
            reviewer = WorkflowReviews(
                CurrentArtifacts(
                    core, handle, self.store, inspector, runtime, {task.id: plan}, criteria
                ),
                key,
                self._review_source(root),
            )
            routes.append(EvidenceRoute(PREFIX, KINDS, reviewer))
        core.context.evidence = EvidenceRouter(routes)
        grant = read_private(self.spec.config_seal.path).get("grant_ref")
        from ai_task_manager.application.commits import WorkerCommits

        commits = (
            WorkerCommits(
                core,
                handle,
                runtime,
                self.journal,
                worker_git,
                checks=authority.confined_commands,
            )
            if task.code_bearing
            else None
        )
        from ai_task_manager.adapters.workflow_control.recovery import RecoveryHistory

        provider_config = read_private(self.spec.config_seal.path).get("linear")
        history = None
        if provider_config:
            history = RecoveryHistory(
                self.spec.config_seal.path,
                core=core.context.db.path,
                workflow=self.store.path,
                runtime=self.journal.path,
                host=self.host_journal.path,
                provider=provider_config["journal"],
            )

        def decision_lookup(scope, current, target, action):
            return decisions.decision(scope, current, target, action, grant_ref=grant)

        code_delivery = None
        if task.code_bearing:
            from .code_delivery import InstalledCodeDelivery

            code_delivery = InstalledCodeDelivery(
                core,
                handle,
                self.store,
                runtime,
                self.resources,
                self.code,
                context,
                self._review_source(root, deadline=d["deadline"]),
                key,
                decision_lookup,
                provider,
                criteria,
                dependencies=self.dependencies,
            )
            from .worker_resources import ResourceWorkerCommits

            commits = ResourceWorkerCommits(
                commits, code_delivery.resources, code_delivery.freshness
            )
            core.context.evidence = EvidenceRouter(
                [
                    *core.context.evidence.routes,
                    EvidenceRoute(
                        "merge-publication:",
                        frozenset({"merge", "publication_intent"}),
                        code_delivery.evidence,
                    ),
                ]
            )
            inspector = code_delivery.inspector
        driver = WorkflowDriver(
            core,
            handle,
            self.store,
            WorkflowPlans((plan,)),
            runtime=runtime,
            repository=inspector,
            commits=commits,
            provider=provider,
            reviews=ReviewedEvidence(core, reviewer.lookup) if reviewer is not None else None,
            code_delivery=code_delivery,
            recovery_history=history,
            decisions=decision_lookup,
            owner_identity=self._owner_identity,
        )
        from ai_task_manager.installation.native.reviewer import NormalReviewerFactory

        # Normal sealed composition retains its lifecycle owner. Explicit trusted
        # callable injection keeps its existing contract, without a new close API.
        factory = self.registry.reviewer_factory
        return WorkflowService(
            driver,
            {task.id: plan.id},
            reviewer_owner=factory if isinstance(factory, NormalReviewerFactory) else None,
        )

    def _owner_identity(self):
        import os

        from ai_task_manager.adapters.workflow_control.identity import process_start

        control = (
            self.recovery.successor
            if self.recovery
            else read_private(self.spec.config_seal.path)["workflow_control"]
        )
        return {
            "pid": os.getpid(),
            "start": process_start(os.getpid()),
            "instance": control["instance"],
        }

    def _review_source(self, root, *, deadline=None):
        from ai_task_manager.adapters.workflow_reviews.capture_store import (
            CaptureStore,
            NativeReviewSource,
        )

        # Only trusted native registration supplies a producer. A model request
        # cannot choose an implementation or manufacture a verdict.
        factory = self.registry.reviewer_factory
        if deadline is not None and factory is not None:
            original = factory

            def bounded(inspection):
                import time

                from ai_task_manager.adapters.workflow_reviews.native import NativeReviewCapture

                if time.time() >= deadline:
                    raise AccessDenied("Original installed review deadline expired")
                producer = original(inspection)
                if (
                    type(producer) is not NativeReviewCapture
                    or producer.workers.deadline > deadline
                ):
                    raise AccessDenied("Independent review outlives its original parent workflow")
                return producer

            factory = bounded
        return NativeReviewSource(
            CaptureStore(
                root / "reviews", existing_only=self.successor_mode or self.recovery is not None
            ),
            factory,
        )

    def serve(self, stop=None, ready=None):
        return self.owner.serve(stop, ready)
