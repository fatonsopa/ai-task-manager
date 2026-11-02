"""Execute a persisted plan through configured scoped source/execution adapters."""

from typing import Protocol

from ai_task_manager.domains.checks.models import Outcome, Plan
from ai_task_manager.domains.checks.results import Execution, comparison
from ai_task_manager.domains.checks.reuse import eligible
from ai_task_manager.domains.checks.selection import select
from ai_task_manager.domains.work.errors import StaleVersion, ValidationError


class SourceGuard(Protocol):
    def verify(self, plan: Plan) -> None:
        """Check immutable source/input and scoped execution binding at this boundary."""
        ...


class Executor(Protocol):
    def execute(self, plan: Plan, group: str, units: tuple[str, ...]) -> Execution:
        """Observe actual execution; a thrown exception leaves the effect uncertain."""
        ...


class BaselineReader(Protocol):
    def comparable_failures(self, plan: Plan, group: str) -> tuple[tuple[str, str], ...]:
        """Return verified exact named failure signatures, not a dev-red boolean."""
        ...


class MergeChecks:
    def __init__(
        self, store, source_guard: SourceGuard, executor: Executor, *, catalogs, baselines=None
    ):
        self.store, self.source_guard, self.executor = store, source_guard, executor
        self.baselines, self.catalogs = baselines, catalogs

    def _verify(self, plan):
        self.catalogs.verify(plan)
        self.source_guard.verify(plan)

    def prepare(self, scope, attempt, source_digest, change, discovery, key):
        scope.validate()
        catalog = self.catalogs.for_project(scope.project)
        plan = select(catalog, scope, attempt, source_digest, change, discovery)
        self._verify(plan)
        return self.store.create(plan, key)

    def run(self, ref, scope):
        snapshot = self.store.read(ref, scope)
        plan = snapshot["plan"]
        self._verify(plan)
        parents = dict(plan.prerequisites)
        for group in plan.groups:
            snapshot = self.store.read(ref, scope)
            row = snapshot["groups"][group.key]
            if row["state"] != "pending":
                # An unresolved intent is observable uncertainty, never permission to rerun.
                continue
            self._verify(plan)
            if any(
                snapshot["groups"][p]["state"] != "complete"
                or snapshot["groups"][p]["result"]["outcome"]
                not in {Outcome.PASSED, Outcome.SKIPPED}
                for p in parents.get(group.key, ())
            ):
                try:
                    self.store.block(
                        ref, scope, group.key, row["version"], "Prerequisite not passed"
                    )
                except StaleVersion:
                    pass
                continue
            units = tuple(u for u in plan.units if u.group == group.key)
            carried = {}
            for unit in units:
                proof = next(
                    (p for p in self.store.proofs(plan, unit) if eligible(plan, unit, p)), None
                )
                if proof is not None:
                    carried[unit.name] = proof
            dispatched = tuple(u.name for u in units if u.name not in carried)
            try:
                version = self.store.begin(
                    ref, scope, group.key, row["version"], dispatched, carried
                )
            except StaleVersion:
                continue
            if not dispatched:
                self.store.finish(ref, scope, group.key, version)
                continue
            try:
                execution = self.executor.execute(plan, group.key, dispatched)
            except Exception as error:
                self.store.finish(ref, scope, group.key, version, error=type(error).__name__)
                continue
            baseline = None
            if self.baselines is not None:
                try:
                    baseline = comparison(self.baselines.comparable_failures(plan, group.key))
                except Exception:
                    # Preserve the physical result; unavailable comparison grants no dev-red credit.
                    baseline = None
            try:
                self._verify(plan)
            except Exception as error:
                self.store.finish(
                    ref, scope, group.key, version, execution, error=type(error).__name__
                )
                continue
            try:
                self.store.finish(ref, scope, group.key, version, execution, baseline=baseline)
            except ValidationError as error:
                self.store.finish(ref, scope, group.key, version, error=type(error).__name__)
        return self.store.read(ref, scope)
