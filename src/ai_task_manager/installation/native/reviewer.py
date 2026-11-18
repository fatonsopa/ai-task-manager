"""Default installed composition for separately enrolled native review stages."""

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
from ai_task_manager.config.files import digest, read_private
from ai_task_manager.config.service import load_service
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import ACTIVE_ATTEMPTS
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
        from threading import RLock

        self._lifecycle_lock = RLock()
        self._closed = False
        self._close_outcomes = None
        self._check_pointer()

    def _check_pointer(self):
        self.author.check()
        if self.pointer != read_private(self.author.installation.path).get("reviewer_installation"):
            raise AccessDenied("Reviewer pointer differs from sealed author installation")
        stages = type(self.pointer) is dict and set(self.pointer) == {
            "code_worker",
            "code_candidate",
        }
        selected = []
        if stages:
            for stage, value in self.pointer.items():
                if type(value) is list:
                    if stage != "code_candidate" or not 1 <= len(value) <= 4:
                        raise AccessDenied("Only a bounded candidate reviewer pool is supported")
                    from .code_provisioning import sealed

                    source = sealed(self.author.data.get("code_manifest"))
                    maximum = read_private(source.path).get("attempts")
                    if type(maximum) is not int or not 1 <= maximum <= 4 or len(value) > maximum:
                        raise AccessDenied("Candidate reviewer pool exceeds sealed attempt budget")
                    selected.extend((stage, pointer) for pointer in value)
                else:
                    selected.append((stage, value))
        else:
            selected.append((None, self.pointer))
        specs = []
        author = read_private(self.author.installation.path)
        for stage, pointer in selected:
            if type(pointer) is not dict or set(pointer) != {"path", "device", "inode", "sha256"}:
                raise AccessDenied("Exact physical reviewer installation required")
            if file_binding(pointer["path"]) != pointer:
                raise AccessDenied("Reviewer installation changed or was replaced")
            if stages:
                specs.append(Installation(pointer["path"], self.author.config.path).data)
        if stages and (
            len({s["task_id"] for s in specs}) != len(specs)
            or len({s["state_root"] for s in specs}) != len(specs)
        ):
            raise AccessDenied("Code stages require distinct reviewer tasks and state roots")
        if stages:
            roots = [Path(author["state_root"]), *(Path(spec["state_root"]) for spec in specs)]
            if any(
                a == b or a in b.parents or b in a.parents
                for i, a in enumerate(roots)
                for b in roots[i + 1 :]
            ):
                raise AccessDenied("Code reviewer state roots cannot overlap")
            for (stage, _), spec in zip(selected, specs):
                if (
                    spec.get("role") != "reviewer"
                    or spec.get("reviewer_installation") is not None
                    or spec.get("review_target")
                    != {
                        "task_id": author["task_id"],
                        "prose_digest": author["prose_digest"],
                        "stage": stage,
                    }
                    or spec["task_id"] == author["task_id"]
                    or spec["scope"] != author["scope"]
                    or spec["deadline"] > author["deadline"]
                    or spec["repository"] != author["repository"]
                    or spec["source_head"] != author["source_head"]
                ):
                    raise AccessDenied(
                        "Code stage reviewer installation differs from original authority"
                    )

    def _selected(self, inspection, *, service=None):
        stage = inspection.context.get("review_kind")
        stages = set(self.pointer) == {"code_worker", "code_candidate"}
        if stages:
            if stage not in self.pointer:
                raise AccessDenied("A configured code stage is required")
            value = self.pointer[stage]
            if type(value) is list:
                return self._candidate_slot(inspection, value, service), stage
            return value, stage
        if stage in {"code_worker", "code_candidate"}:
            raise AccessDenied("Both independent code reviewer stages must be installed")
        return self.pointer, None

    def _candidate_slot(self, inspection, pointers, service):
        if service is None:
            raise AccessDenied("Current scoped code authority is required before slot reservation")
        author = read_private(self.author.installation.path)
        task = service.core.get_task(service.handle, author["task_id"])
        require_owned(service, task)
        if (
            asdict(service.scope) != author["scope"]
            or task.prose_digest != author["prose_digest"]
            or not task.code_bearing
            or task.human_hold
            or task.delivered_once
            or task.observed_status != "In Progress"
            or task.sync_state != "confirmed"
            or inspection.context.get("task_id") != task.id
            or inspection.context.get("prose_digest") != task.prose_digest
            or inspection.context.get("scope") != author["scope"]
            or service.core.context.clock() >= author["deadline"]
        ):
            raise AccessDenied("Current author cannot reserve a candidate reviewer")
        root = Path(author["state_root"])
        journal = HostJournal.open_existing(root / "host.sqlite", root / "host.key")
        binding = file_binding(self.author.installation.path)
        for index, pointer in enumerate(pointers):
            key = "code-candidate-slot:" + digest([binding, index])
            expected = {
                "version": 1,
                "author": binding,
                "pool": digest(pointers),
                "context_digest": inspection.digest,
                "pointer": pointer,
            }
            prior = journal.intent(key)
            if prior is not None:
                if prior == expected:
                    return pointer
                if (
                    type(prior) is not dict
                    or set(prior) != set(expected)
                    or any(prior[k] != expected[k] for k in expected if k != "context_digest")
                ):
                    raise AccessDenied("Retained candidate reviewer slot binding changed")
                occupied = Installation(pointer["path"], self.author.config.path)
                previous = service.core.get_task(service.handle, occupied.data["task_id"])
                require_owned(service, previous)
                if previous.execution_state in ACTIVE_ATTEMPTS:
                    raise AccessDenied("Prior candidate reviewer is active or unresolved")
                continue
            spec = Installation(pointer["path"], self.author.config.path)
            reviewer = service.core.get_task(service.handle, spec.data["task_id"])
            require_owned(service, reviewer)
            if (
                reviewer.prose_digest != spec.data["prose_digest"]
                or reviewer.code_bearing
                or reviewer.human_hold
                or reviewer.sync_state != "confirmed"
                or reviewer.observed_status != "Todo"
            ):
                raise AccessDenied(
                    "New candidate slot requires its original approved Todo reviewer"
                )
            try:
                journal.intend(key, expected)
            except AccessDenied:
                # Another exact owner may have atomically claimed this slot.
                # Re-read its signed binding; never overwrite or reuse it.
                current = journal.intent(key)
                if type(current) is not dict or (
                    set(current) != set(expected)
                    or any(current[k] != expected[k] for k in expected if k != "context_digest")
                ):
                    raise
                if current != expected:
                    continue
            if journal.intent(key) != expected:
                raise AccessDenied("Candidate reviewer reservation was not retained")
            return pointer
        raise AccessDenied("Preapproved candidate reviewer capacity is exhausted")

    def __call__(self, inspection):
        with self._lifecycle_lock:
            if self._closed:
                raise AccessDenied("The retained reviewer owner is closed")
            return self._create(inspection)

    def _create(self, inspection):
        from ai_task_manager.installation.workflows.composition import NativeContext, NoCommands

        from .resolver import resolve

        self._check_pointer()
        inspection.check()
        author = read_private(self.author.installation.path)
        service = code_source = None
        if (
            inspection.context.get("review_kind") == "code_candidate"
            and type(self.pointer.get("code_candidate")) is list
        ):
            from .review_stages import CodeStageSource

            service = load_service(self.author.config.path, **self.options)
            code_source = CodeStageSource(self.author, service, inspection)
        pointer, stage = self._selected(inspection, service=service)
        spec = Installation(pointer["path"], self.author.config.path)
        d, context = spec.data, inspection.context
        target = {"task_id": context["task_id"], "prose_digest": context["prose_digest"]}
        target.update(
            {"stage": stage} if stage else {"source_head": context["source_version"]["head"]}
        )
        if (
            d.get("role") != "reviewer"
            or d.get("reviewer_installation") is not None
            or d.get("review_target") != target
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
        if registration.host not in {"claude", "codex"}:
            raise AccessDenied("Independent native review requires a supported managed host")
        service = service or load_service(self.author.config.path, **self.options)
        core, handle = service.core, service.handle
        if stage is not None and code_source is None:
            from .review_stages import CodeStageSource

            code_source = CodeStageSource(self.author, service, inspection)

        def current_author(connection=None):
            if connection is None:
                self._check_pointer()
                spec.check()
                inspection.check()
            if code_source is not None:
                code_source.check(connection)
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
        plan = spec.plan(registration.host)

        def resource_binding(current, selected):
            self._check_pointer()
            spec.check()
            registration.check()
            if (current.id != task.id or current.prose_digest != d["prose_digest"]
                    or selected != plan):
                raise AccessDenied("Current reviewer task/plan binding differs")

        from ai_task_manager.adapters.workflows.resources import bind_resources

        witnesses = tuple(tuple(value[key] for key in ("path", "device", "inode", "sha256"))
                          for value in (spec.seal.original, spec.config_seal.original))
        bind_resources(core, task, plan, resources.git.common, resource_binding,
                       witnesses=witnesses)
        control_parent = Path(
            read_private(self.author.config.path)["workflow_control"]["endpoint"]
        ).parent.resolve()
        runtime = ProcessRuntime(journal, root / "outputs", recovery_directory=control_parent)
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
        from ai_task_manager.adapters.workflow_control.recovery import physical

        parent = HostJournal.open_existing(
            Path(author["state_root"]) / "host.sqlite", Path(author["state_root"]) / "host.key"
        )
        parent._append(
            "trace",
            {
                "effect": "reviewer_state_created",
                "installation": file_binding(spec.seal.path),
                "task_id": task.id,
                "scope": d["scope"],
                "files": {
                    "runtime": physical(journal.path),
                    "host": physical(host_journal.path),
                    "runtime_key": file_binding(root / "runtime.key"),
                    "host_key": file_binding(host_journal.key_path),
                },
            },
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

    def close(self):
        """Inspect retained stop evidence; never repeat an uncertain native stop."""
        from ai_task_manager.domains.work.errors import EvidenceRequired
        from ai_task_manager.domains.work.models import ACTIVE_ATTEMPTS

        with self._lifecycle_lock:
            self._closed = True
            if self._close_outcomes is not None:
                return [dict(row) for row in self._close_outcomes]
            outcomes = []
            for inspection, producer in self.producers.items():
                workers, handle = producer.workers, producer.handle
                base = {"role": "reviewer", "inspection": inspection, "task_id": producer.task_id}
                try:
                    _, scope = workers.core.session_binding(handle)
                    with workers.journal.transaction() as conn:
                        ids = [
                            row[0]
                            for row in conn.execute("SELECT id FROM runtime_attempts ORDER BY id")
                        ]
                    for attempt_id in ids:
                        item = {**base, "attempt_id": attempt_id}
                        try:
                            record = workers.journal.attempt(attempt_id)
                            attempt = workers.core.get_attempt(handle, attempt_id)
                            if (
                                record["task_id"] != producer.task_id
                                or attempt.task_id != producer.task_id
                                or record["scope"] != asdict(scope)
                            ):
                                raise EvidenceRequired("Retained reviewer ownership differs")
                            observations = workers.journal.observations(
                                attempt_id, "runtime_outcome"
                            )
                            stops = [row for row in observations if row.get("managed_stop") is True]
                            if (
                                not stops
                                or stops[-1].get("quiescent") is not True
                                or stops[-1].get("worker_id") != attempt.worker_id
                                or stops[-1].get("runtime_owner_worker_id") != record["worker_id"]
                            ):
                                raise EvidenceRequired("Complete reviewer stop is unavailable")
                            # Core validates the original managed proof and current scope.
                            # Reconciliation never launches or stops a process again.
                            if attempt.state in ACTIVE_ATTEMPTS:
                                attempt = workers.reconcile(
                                    handle,
                                    attempt_id,
                                    operation_key="review-owner-close:" + attempt_id,
                                )
                            if (
                                attempt.state in ACTIVE_ATTEMPTS
                                or stops[-1].get("state") != attempt.state
                            ):
                                raise EvidenceRequired("Reviewer settlement remains unresolved")
                            outcomes.append({**item, "state": attempt.state, "quiescent": True})
                        except Exception as error:
                            outcomes.append(
                                {
                                    **item,
                                    "state": "uncertain",
                                    "quiescent": False,
                                    "reason": type(error).__name__,
                                }
                            )
                except Exception as error:
                    outcomes.append(
                        {
                            **base,
                            "state": "uncertain",
                            "quiescent": False,
                            "reason": type(error).__name__,
                        }
                    )
            self._close_outcomes = tuple(outcomes)
            return [dict(row) for row in outcomes]
