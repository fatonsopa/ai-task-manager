"""Connect saved committed work, code units and one installed merge authority."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from ai_task_manager.adapters.git.candidates import CandidateProfile
from ai_task_manager.adapters.git.candidates.process import CandidateProcesses
from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.adapters.workflow_reviews.code_candidates import CodeCandidateReviews
from ai_task_manager.adapters.workflow_reviews.code_worker_artifacts import CodeWorkerArtifacts
from ai_task_manager.adapters.workflow_reviews.code_worker_units import CodeWorkerUnitIssuer
from ai_task_manager.adapters.workflows.committed import CommittedRepositoryInspector
from ai_task_manager.application.merge_checks.service import MergeChecks
from ai_task_manager.application.merging.authority import resource_alias
from ai_task_manager.application.merging.candidates import CandidateService, CoreCandidateAuthority
from ai_task_manager.application.merging.workflows import (
    CandidateChecks,
    MergeEvidence,
    MergePlan,
    MergeWorkflowService,
)
from ai_task_manager.domains.merging.candidates import CandidateGrant
from ai_task_manager.domains.merging.contracts import Binding
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.storage.merging.journal import Journal

from .code_registration import CodeCapabilities


@dataclass(frozen=True)
class DeliveryResources:
    """Original parent budget accompanies every physical merge consumer."""

    owner: object
    clock: object
    deadline: float
    freshness: object = None

    @property
    def git(self):
        return self.owner.git

    def _current(self):
        if self.freshness is not None:
            self.freshness.check()
        if self.clock() >= self.deadline:
            raise AccessDenied("Original installed code-delivery deadline expired")

    def verify(self, pair):
        self._current()
        observed = self.owner.verify(pair)
        self._current()
        return observed

    @contextmanager
    def use(self, pair):
        self._current()
        with self.owner.use(pair):
            yield self.verify(pair)
            self._current()


class ResourceReviews:
    def __init__(self, source, resources, pair):
        self.source, self.resources, self.pair = source, resources, pair
        self._bound = (source, resources, pair)

    def check_binding(self):
        if any(a is not b for a, b in zip((self.source, self.resources, self.pair), self._bound)):
            raise EvidenceRequired("Installed resource-bound review owner changed")
        check = getattr(self.source, "check_binding", None)
        if callable(check):
            check()

    def resolve(self, inspection):
        self.check_binding()
        self.resources.verify(self.pair)
        result = self.source.resolve(inspection)
        self.resources.verify(self.pair)
        return result


class ResourceReads:
    """Keep the existing finite read policy and revalidate after each wait/read."""

    def __init__(self, reads, resources, pair):
        self.delegate, self.resources, self.pair = reads, resources, pair
        self.store = reads.store

    def ensure_parent(self):
        self.resources.verify(self.pair)
        self.delegate.ensure_parent()
        self.resources.verify(self.pair)

    def _after(self, callback):
        self.resources.verify(self.pair)
        callback()
        self.resources.verify(self.pair)

    def read(self, ref, scope, *, after_wait):
        self.resources.verify(self.pair)
        result = self.delegate.read(ref, scope, after_wait=lambda: self._after(after_wait))
        self.resources.verify(self.pair)
        return result

    def proofs(self, plan, unit, *, after_wait):
        self.resources.verify(self.pair)
        result = self.delegate.proofs(plan, unit, after_wait=lambda: self._after(after_wait))
        self.resources.verify(self.pair)
        return result


class ResourceGuard:
    def __init__(self, guard, resources, pair):
        self.delegate, self.resources, self.pair = guard, resources, pair

    def verify(self, plan):
        self.resources.verify(self.pair)
        self.delegate.verify(plan)
        self.resources.verify(self.pair)


class ResourceExecutor:
    def __init__(self, executor, resources, pair):
        self.delegate, self.resources, self.pair = executor, resources, pair

    def execute(self, plan, group, units):
        self.resources.verify(self.pair)
        result = self.delegate.execute(plan, group, units)
        self.resources.verify(self.pair)
        return result


def restored_plan(value):
    grant = dict(value["grant"])
    grant["binding"] = Binding(**grant["binding"])
    return MergePlan(**dict(value, grant=CandidateGrant(**grant)))


class InstalledCodeDelivery:
    def __init__(
        self,
        core,
        handle,
        store,
        runtime,
        resources,
        registration,
        context,
        review_source,
        issuer_key,
        decisions,
        provider,
        criteria,
        dependencies=None,
    ):
        self.core, self.handle, self.store, self.runtime = core, handle, store, runtime
        from .delivery_dependencies import DeliveryDependencies

        self.freshness = DeliveryDependencies(core, handle, dependencies, context.deadline)
        self.resources = DeliveryResources(
            resources, core.context.clock, context.deadline, self.freshness
        )
        self.registration, self.context = registration, context
        self.review_source, self.issuer_key = review_source, issuer_key
        self.decisions, self.provider, self.criteria = decisions, provider, criteria
        self._bound = (
            core,
            handle,
            store,
            runtime,
            self.resources,
            registration,
            context,
            review_source,
            issuer_key,
            decisions,
            provider,
            criteria,
        )
        self._receipts, self.components, self.capabilities = {}, {}, {}
        self.inspector = CommittedRepositoryInspector(runtime, self._receipt)
        self.evidence = MergeEvidence(clock=core.context.clock)
        self.issuer = None

    def _receipt(self, attempt_id):
        return self._receipts.get(attempt_id)

    def _step(self, row, owner, name, payload, action):
        prior = self.store.read(row["id"], self.scope)["steps"].get(name)
        if prior and prior["payload"] != payload:
            raise AccessDenied("Retained code continuation step changed")
        if prior and prior["result"] is not None:
            return prior["result"]
        self.store.intend(row["id"], owner, name, payload)
        observed = action()
        self.store.finish(row["id"], owner, name, observed)
        return observed

    def _capabilities(self, attempt, pair):
        if attempt.id not in self.capabilities:
            value = self.registration.prepare(
                replace(self.context, task=self.core.get_task(self.handle, attempt.task_id))
            )
            self.resources.verify(pair)
            if type(value) is not CodeCapabilities:
                raise EvidenceRequired("Typed installed code Git/check capabilities required")
            value.require()
            checks = value.checks.checks
            if type(checks) is not MergeChecks:
                raise EvidenceRequired("Actual persisted merge-check service required")
            wrapped = MergeChecks(
                checks.store,
                ResourceGuard(checks.source_guard, self.resources, pair),
                ResourceExecutor(checks.executor, self.resources, pair),
                catalogs=checks.catalogs,
                baselines=checks.baselines,
                reads=ResourceReads(checks.reads, self.resources, pair),
            )

            def discover(candidate):
                self.resources.verify(pair)
                observed = value.checks.discover(candidate)
                self.resources.verify(pair)
                return observed

            self.capabilities[attempt.id] = replace(
                value, checks=CandidateChecks(wrapped, discover)
            )
        return self.capabilities[attempt.id]

    def _component(self, row, plan, attempt, pair, commit, retained):
        capability = self._capabilities(attempt, pair)
        profile = capability.profile
        worktree_id = resource_alias("worktree", pair.worktree_id)
        repository_id = resource_alias("repository", str(self.resources.git.repository))
        if worktree_id not in profile.repositories:
            profile.enroll(
                pair.worktree,
                worktree_id=worktree_id,
                repository_id=repository_id,
                target_ref=self.registration.data["target_ref"],
            )
        record = profile.validate(worktree_id, repository_id)
        if record["target_ref"] != self.registration.data["target_ref"]:
            raise AccessDenied("Installed code target differs from the enrolled Git profile")
        root = Path(self.context.host_journal.path).parent / "merge"
        root.mkdir(mode=0o700, exist_ok=True)
        candidates = root / "candidates"
        candidates.mkdir(mode=0o700, exist_ok=True)
        candidate_profile = CandidateProfile(
            profile,
            worktree_id,
            repository_id,
            candidates,
            required_hooks=capability.required_hooks,
        )
        task = self.core.get_task(self.handle, row["task_id"])
        self.resources.verify(pair)
        if retained is None:
            binding = Binding(
                task.id,
                attempt.id,
                self.scope.principal,
                self.scope.logical_session,
                self.scope.project,
                task.prose_digest,
                self.registration.fingerprint,
                worktree_id,
                repository_id,
                resource_alias("database", pair.database_id),
                record["configuration_digest"],
                row["id"] + ":candidate",
            )
            grant = CandidateGrant(
                binding,
                "refs/heads/" + commit["branch"],
                commit["commit"],
                profile.reference(pair.worktree, record["target_ref"]),
                task.version,
                attempt.version,
                task.decision_revision,
                self.decisions(self.scope, task, "In Dev", "merge"),
                self.context.task.id + ":installed-merge",
            )
            selected = MergePlan(
                plan.id + ":merge",
                grant,
                plan.deadline,
                self.registration.data["require_human_acceptance"],
            )
        else:
            selected = retained
        g = selected.grant
        if (
            selected.deadline != plan.deadline
            or selected.require_human_acceptance
            != self.registration.data["require_human_acceptance"]
            or (g.binding.task_id, g.binding.attempt_id, g.source_head, g.source_ref)
            != (task.id, attempt.id, commit["commit"], "refs/heads/" + commit["branch"])
            or g.binding.configuration_digest != record["configuration_digest"]
            or g.binding.source_digest != self.registration.fingerprint
        ):
            raise AccessDenied("Saved merge grant differs from exact installed code policy")
        candidate = CandidateService(
            Journal(root / "journal.sqlite"),
            candidate_profile,
            CoreCandidateAuthority(self.core, self.handle, self.resources, pair),
            process_factory=CandidateProcesses,
        )
        source = ResourceReviews(self.review_source, self.resources, pair)
        reviews = CodeCandidateReviews(
            self.core, self.handle, candidate, plan.allowed_files, source
        )
        component = MergeWorkflowService(
            self.core,
            self.handle,
            selected,
            candidate,
            capability.checks,
            self.provider,
            self.evidence,
            clock=self.core.context.clock,
            candidate_reviews=reviews,
        )
        self.components[row["id"]] = component
        return component

    def advance(self, row, plan, owner):
        self.freshness.start(row)
        current = (
            self.core,
            self.handle,
            self.store,
            self.runtime,
            self.resources,
            self.registration,
            self.context,
            self.review_source,
            self.issuer_key,
            self.decisions,
            self.provider,
            self.criteria,
        )
        if any(a is not b for a, b in zip(current, self._bound)):
            raise AccessDenied("Installed code composition changed")
        self.registration.check()
        _, self.scope = self.core.session_binding(self.handle)
        task = self.core.get_task(self.handle, row["task_id"])
        self.registration.require(self.core, self.handle, task)
        attempt = self.core.get_attempt(self.handle, row["attempt_id"])
        commit = row["steps"].get("worker-commit", {}).get("result")
        if not commit or attempt.state != "done" or attempt.task_id != task.id:
            raise EvidenceRequired("Completed owned attempt and actual worker commit required")
        self._receipts[attempt.id] = commit
        pair = self.runtime.pair(attempt.id)
        with self.resources.use(pair):
            self.resources.verify(pair)
            prior = row["steps"].get("code-plan")
            if prior is None:
                observed = self.inspector.inspect(task, attempt, plan)
                self._step(row, owner, "verify", {"attempt_id": attempt.id}, lambda: observed)
                issuer = CodeWorkerUnitIssuer(
                    CodeWorkerArtifacts(
                        self.core,
                        self.handle,
                        self.store,
                        self.inspector,
                        self.runtime,
                        {task.id: plan},
                        self.criteria,
                    ),
                    self.issuer_key,
                    ResourceReviews(self.review_source, self.resources, pair),
                    clock=self.core.context.clock,
                )
                self.issuer = issuer
                # Replace only this installation's code-unit route on re-preparation.
                routes = [r for r in self.core.context.evidence.routes if r.prefix != issuer.prefix]
                self.core.context.evidence = EvidenceRouter(
                    [
                        *routes,
                        EvidenceRoute(issuer.prefix, issuer.kinds, issuer),
                    ]
                )
                refs = issuer.lookup(self.scope, task, observed["digest"])
                self.resources.verify(pair)
                if refs is None:
                    return {
                        "state": "awaiting_review",
                        "reason": issuer.last_wait_reason,
                        "workflow": row["id"],
                        "task_id": task.id,
                    }
                for unit in task.required_work:
                    task = self.core.get_task(self.handle, task.id)
                    name = "unit:" + unit
                    old = self.store.read(row["id"], self.scope)["steps"].get(name)
                    payload = (
                        old["payload"]
                        if old
                        else {
                            "proof_ref": refs["units"][unit],
                            "version": task.version,
                            "decision_revision": task.decision_revision,
                        }
                    )

                    def complete(unit=unit, payload=payload):
                        self.resources.verify(pair)
                        value = self.core.execution.complete_unit(
                            self.handle,
                            task.id,
                            unit,
                            payload["version"],
                            payload["decision_revision"],
                            payload["proof_ref"],
                            row["id"] + ":unit:" + unit,
                        )
                        return {"task_version": value.version}

                    self._step(row, owner, name, payload, complete)
                task = self.core.get_task(self.handle, task.id)
                if not set(task.required_work) <= set(task.completed_work):
                    raise EvidenceRequired("Every code work unit must be verified before merge")
            retained = restored_plan(prior["payload"]["plan"]) if prior else None
            component = self.components.get(row["id"])
            if component is None:
                component = self._component(row, plan, attempt, pair, commit, retained)
            selected = {
                "registration": self.registration.fingerprint,
                "commit": commit,
                "plan": asdict(component.plan),
            }
            self._step(row, owner, "code-plan", selected, lambda: {"installed": True})
            self.resources.verify(pair)
            # Both independent semantic waits and all downstream check consumers
            # stay inside this resource use boundary; return retains logical ownership.
            response = component.run(task.id, component.plan.id, row["id"] + ":merge")
            self.resources.verify(pair)
            body = dict(response.get("data", {}), workflow=row["id"])
            body["merge_workflow"] = response.get("data", {}).get("workflow")
            if body.get("state") == "done":
                self._step(row, owner, "code-merge", selected, lambda: body)
            return body

    def end_call(self):
        self.freshness.close()

    def close(self):
        self.freshness.close()
        return [row for component in self.components.values() for row in component.close()]
