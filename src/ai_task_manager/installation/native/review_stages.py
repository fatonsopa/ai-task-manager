"""Reconstruct code review input from the retained canonical delivery owner."""

from dataclasses import asdict, replace
from pathlib import Path

from ai_task_manager.adapters.git.candidates import CandidateProfile
from ai_task_manager.adapters.git.candidates.process import CandidateProcesses
from ai_task_manager.adapters.hosts.bridge.admission import NativeSourcePins
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.resources.manager import ResourceManager
from ai_task_manager.adapters.workflow_reviews.code_candidates import CodeCandidateReviews
from ai_task_manager.adapters.workflow_reviews.code_worker_artifacts import CodeWorkerArtifacts
from ai_task_manager.adapters.workflows.committed import CommittedRepositoryInspector
from ai_task_manager.application.merging.candidates import CandidateService, CoreCandidateAuthority
from ai_task_manager.application.workflows.models import WorkflowPlan
from ai_task_manager.config.files import digest, private_path
from ai_task_manager.domains.checks.models import canonical, fingerprint
from ai_task_manager.domains.merging.candidates import CandidateResult
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.commands.manifest import CommandManifest
from ai_task_manager.installation.workflows.composition import NativeContext
from ai_task_manager.installation.workflows.spec import Installation
from ai_task_manager.storage import attempts, sessions
from ai_task_manager.storage.merging.journal import Journal
from ai_task_manager.storage.workflows import WorkflowStore
from ai_task_manager.supervisor.journal import RuntimeJournal
from ai_task_manager.supervisor.models import ResourcePair


class ObservedResources:
    def __init__(self, resources, pair):
        self.resources, self.value = resources, pair

    def pair(self, attempt_id):
        if attempt_id != self.value.attempt_id:
            raise AccessDenied("Review belongs to another canonical attempt")
        self.resources.verify(self.value)
        return self.value

    def ownership(self, attempt_id):
        pair = self.pair(attempt_id)
        fact = self.resources.verify(pair)["worktree"]
        return self.resources.git.inspect(pair.worktree, fact["branch"])


class InspectionOnly:
    def resolve(self, inspection):
        self.value = inspection
        return None


class CodeStageSource:
    """Physical preparation stays outside Core; the input lease checks Core only."""

    def __init__(self, author, service, inspection):
        self.author, self.service, self.inspection = author, service, inspection
        self.spec = Installation(author.installation.path, author.config.path)
        self.core, self.handle = service.core, service.handle
        self.scope = service.scope
        self.task = self.core.get_task(self.handle, self.spec.data["task_id"])
        self.stage = inspection.context.get("review_kind")
        if self.stage not in {"code_worker", "code_candidate"} or not self.task.code_bearing:
            raise AccessDenied("An installed code stage and current code task are required")
        root = Path(self.spec.data["state_root"])
        for name in ("workflow.sqlite", "runtime.sqlite", "host.sqlite", "host.key"):
            private_path(root / name)
        if not (root / "resources").is_dir():
            raise AccessDenied("Original code resources are required; review cannot allocate them")
        self.store = WorkflowStore(root / "workflow.sqlite")
        self.journal = RuntimeJournal(root / "runtime.sqlite")
        self.resources = ResourceManager(
            self.journal,
            root / "resources",
            Path(self.spec.data["repository"]),
            metadata_paths=(self.core.context.db.path, self.store.path, root / "host.sqlite"),
        )
        with self.store.transaction() as conn:
            head = conn.execute(
                "SELECT run_id FROM workflow_heads WHERE scope=? AND task_id=?",
                (canonical(asdict(self.scope)), self.task.id),
            ).fetchone()
        if head is None:
            raise AccessDenied("Original canonical code workflow is required")
        self.row = self.store.read(head[0], self.scope)
        raw = dict(self.row["plan"])
        for key in ("allowed_files", "artifacts", "commit_checks"):
            raw[key] = tuple(raw[key])
        if raw.get("resource_locks") is not None:
            raw["resource_locks"] = tuple(tuple(pair) for pair in raw["resource_locks"])
        self.plan = WorkflowPlan(**raw)
        pointer = author.data.get("commands_manifest")
        if pointer is None:
            raise AccessDenied("Installed worker verification sources are required")
        command = CommandManifest(pointer["path"], pointer["sha256"])
        expected = replace(
            self.spec.plan(author.data["host"]),
            commit_checks=tuple(d.reference for d in command.definitions),
        )
        pins = NativeSourcePins({str(author.seal.path): author.seal.sha256})
        native = digest(
            {
                "reference": author.data["native_ref"],
                "host": author.data["host"],
                "model": author.data["model"],
                "effort": author.data["effort"],
                "account": author.data["account_hash"],
                "source": pins.fingerprint,
            }
        )
        if (
            self.plan != expected
            or self.row["plan_digest"] != self.plan.digest
            or self.row["runtime_digest"] != native + ":" + self.plan.digest
        ):
            raise AccessDenied("Code review plan differs from the installed canonical source")
        self.attempt = self.core.get_attempt(self.handle, self.row["attempt_id"])
        owner = self.journal.attempt(self.attempt.id)
        if (
            owner["task_id"] != self.task.id
            or owner["scope"] != asdict(self.scope)
            or owner["deadline"] != self.plan.deadline
            or self.attempt.host != self.plan.host
        ):
            raise AccessDenied("Original runtime owner differs from installed code authority")
        records = self.journal.observations(self.attempt.id, "resources_ready")
        if not records:
            raise AccessDenied("Original owned code resources are unavailable")
        fact = records[-1]
        self.pair = ResourcePair(
            self.attempt.id,
            Path(fact["worktree"]["path"]),
            Path(fact["database"]["path"]),
            fact["worktree_id"],
            fact["database_id"],
            fact["id"],
        )
        self.runtime = ObservedResources(self.resources, self.pair)
        self.receipt = self.row["steps"].get("worker-commit", {}).get("result")
        completions = self.journal.observations(self.attempt.id, "worker-commit-complete")
        intent = self.journal.effect(self.attempt.id, "worker-commit-intent")
        candidate = self.journal.effect(self.attempt.id, "worker-commit-candidate")
        outcomes = self.journal.observations(self.attempt.id, "runtime_outcome")
        if (
            not self.receipt
            or not intent
            or not candidate
            or not outcomes
            or outcomes[-1].get("quiescent") is not True
            or fingerprint(intent) != self.receipt.get("intent_digest")
            or candidate.get("tree") != self.receipt.get("tree")
            or not any(
                all(row.get(key) == value for key, value in self.receipt.items())
                for row in completions
            )
        ):
            raise AccessDenied("Original completed worker commit and quiescence are required")
        self._core()
        if self.stage == "code_worker":
            criteria = {
                self.task.id: tuple(
                    (unit, self.task.title + "\n\n" + self.task.body)
                    for unit in self.task.required_work
                )
            }
            artifacts = CodeWorkerArtifacts(
                self.core,
                self.handle,
                self.store,
                CommittedRepositoryInspector(
                    self.runtime, lambda ref: self.receipt if ref == self.attempt.id else None
                ),
                self.runtime,
                {self.task.id: self.plan},
                criteria,
            )
            fresh = artifacts.prepare(
                self.scope, self.task, inspection.context["inspection"].get("digest")
            )
            self.candidates = None
        else:
            fresh = self._candidate(root)
        if (
            fresh is None
            or fresh.digest != inspection.digest
            or fresh.witnesses != inspection.witnesses
        ):
            raise AccessDenied("Review inspection differs from canonical current code artifacts")
        self.prepared = fresh
        self.check()

    def _candidate(self, root):
        from ai_task_manager.application.merging.workflows.recovery import current_plan, restore

        from .code_provisioning import CodeProvisioning

        for unit in self.task.required_work:
            if (
                unit not in self.task.completed_work
                or self.row["steps"].get("unit:" + unit, {}).get("result") is None
            ):
                raise AccessDenied(
                    "Worker review and every canonical code unit must precede candidate review"
                )
        saved = self.row["steps"].get("code-plan", {}).get("payload")
        if (
            not saved
            or self.row["steps"]["code-plan"].get("result") is None
            or saved["commit"] != self.receipt
        ):
            raise AccessDenied("Original installed candidate plan is required")
        original = restore(saved["plan"])
        grant = original.grant
        pointer = self.author.data.get("code_manifest")
        source = CodeProvisioning(
            pointer,
            self.author.data["commands_manifest"],
            self.spec.data,
            parent_check=self.author.check,
        )
        if saved["registration"] != source.registration.fingerprint:
            raise AccessDenied("Candidate review registration changed")
        if (
            saved["plan"]["id"] != self.plan.id + ":merge"
            or saved["plan"]["deadline"] != self.plan.deadline
            or saved["plan"]["require_human_acceptance"]
            != source.registration.data["require_human_acceptance"]
            or grant.source_head != self.receipt["commit"]
            or grant.source_ref != "refs/heads/" + self.receipt["branch"]
            or grant.binding.operation_key != self.row["id"] + ":candidate"
            or grant.binding.source_digest != source.registration.fingerprint
        ):
            raise AccessDenied("Saved candidate authority differs from exact installed code plan")
        capability = source.prepare(
            NativeContext(
                self.core,
                self.handle,
                self.task,
                self.journal,
                HostJournal(root / "host.sqlite", root / "host.key"),
                self.resources,
                None,
                self.spec.data["deadline"],
            )
        )
        capability.profile.enroll(
            self.pair.worktree,
            worktree_id=grant.binding.worktree_id,
            repository_id=grant.binding.repository_id,
            target_ref=source.registration.data["target_ref"],
        )
        profile = CandidateProfile(
            capability.profile,
            grant.binding.worktree_id,
            grant.binding.repository_id,
            root / "merge/candidates",
            required_hooks=capability.required_hooks,
        )
        private_path(root / "merge/journal.sqlite")
        journal = Journal(root / "merge/journal.sqlite")
        matches = [
            op
            for op in journal.for_attempt(self.attempt.id)
            if op["facts"].get("merge_workflow", {}).get("request", {}).get("operation_key")
            == self.row["id"] + ":merge"
        ]
        if len(matches) != 1:
            raise AccessDenied("One exact canonical merge workflow is required for review")
        merge_row = matches[0]["facts"]["merge_workflow"]
        if (merge_row["request"]["plan"], merge_row["request"]["grant"]) != (
            original.digest,
            asdict(original.grant),
        ):
            raise AccessDenied("Canonical merge request differs from the original code plan")
        maximum = source.data["attempts"]
        selected = current_plan(original, merge_row, maximum_attempts=maximum)
        if merge_row["request"]["profile"] != profile.digest:
            raise AccessDenied("Canonical merge profile differs from installed code authority")
        self.original_merge_plan, self.merge_plan = original, selected
        self.merge_workflow_ref, self.maximum_attempts = matches[0]["ref"], maximum
        self.merge_request = merge_row["request"]
        grant = selected.grant
        self.candidates = CandidateService(
            journal,
            profile,
            CoreCandidateAuthority(self.core, self.handle, self.resources, self.pair),
            process_factory=CandidateProcesses,
        )
        observed = CandidateResult(**self.inspection.context["inspection"]["candidate"])
        if merge_row["steps"].get("candidate", {}).get("result") != asdict(observed):
            raise AccessDenied("Review candidate differs from the canonical current merge stage")
        self.candidates.grants[observed.operation_ref] = grant
        recorder = InspectionOnly()
        CodeCandidateReviews(
            self.core, self.handle, self.candidates, tuple(self.spec.data["files"]), recorder
        ).read(self.task, observed)
        self.grant, self.candidate = grant, observed
        return recorder.value

    def _core(self, connection=None):
        if connection is None:
            _, scope = self.core.session_binding(self.handle)
            task = self.core.get_task(self.handle, self.task.id)
            attempt = self.core.get_attempt(self.handle, self.attempt.id)
        else:
            self.core.context.db.require_transaction(connection)
            sid, scope = sessions.resolve(connection, self.handle)
            task = self.core.get_task(self.handle, self.task.id, connection=connection)
            attempt = attempts.get(connection, sid, self.attempt.id)
        context = self.inspection.context
        if (
            scope != self.scope
            or asdict(scope) != context["scope"]
            or task != self.task
            or attempt != self.attempt
            or attempt.state != "done"
            or attempt.task_id != task.id
            or task.prose_digest != self.row["prose_digest"]
            or not task.code_bearing
            or task.human_hold
            or task.delivered_once
            or task.sync_state != "confirmed"
            or task.observed_status != "In Progress"
            or task.decision_revision != self.row["decision_revision"]
            or [attempt.worktree_id, attempt.database_id]
            != [self.pair.worktree_id, self.pair.database_id]
            or context["task_id"] != task.id
            or context["prose_digest"] != task.prose_digest
            or context["author"]
            != {"attempt_id": attempt.id, "worker_id": attempt.worker_id, "host": attempt.host}
            or self.core.context.clock() >= self.spec.data["deadline"]
        ):
            raise AccessDenied("Current canonical code review authority changed")

    def check(self, connection=None):
        self._core(connection)
        if connection is not None:
            return
        self.author.check()
        self.spec.check()
        self.prepared.check()
        with self.store.transaction() as conn:
            head = conn.execute(
                "SELECT run_id FROM workflow_heads WHERE scope=? AND task_id=?",
                (canonical(asdict(self.scope)), self.task.id),
            ).fetchone()
        if head is None or head[0] != self.row["id"]:
            raise AccessDenied("Canonical code workflow was replaced during review")
        current = self.store.read(self.row["id"], self.scope)
        for key in (
            "task_id",
            "attempt_id",
            "prose_digest",
            "decision_revision",
            "plan_digest",
            "runtime_digest",
        ):
            if current[key] != self.row[key]:
                raise AccessDenied("Canonical code workflow changed during review")
        if current["steps"].get("worker-commit", {}).get("result") != self.receipt:
            raise AccessDenied("Canonical worker commit changed during review")
        if self.runtime.ownership(self.attempt.id)["head"] != self.receipt["commit"]:
            raise AccessDenied("Owned worker source changed during review")
        if self.stage == "code_worker" and current["steps"].get("verify") != self.row["steps"].get(
            "verify"
        ):
            raise AccessDenied("Canonical worker inspection changed during review")
        if self.stage == "code_candidate":
            if any(
                current["steps"].get("unit:" + unit) != self.row["steps"].get("unit:" + unit)
                for unit in self.task.required_work
            ):
                raise AccessDenied("Canonical code units changed during review")
            if current["steps"].get("code-plan") != self.row["steps"]["code-plan"]:
                raise AccessDenied("Canonical candidate plan changed during review")
            from ai_task_manager.application.merging.workflows.recovery import current_plan

            merge_row = self.candidates.store.journal.current(self.merge_workflow_ref)["facts"][
                "merge_workflow"
            ]
            if (
                merge_row["request"] != self.merge_request
                or current_plan(
                    self.original_merge_plan, merge_row, maximum_attempts=self.maximum_attempts
                )
                != self.merge_plan
                or merge_row["steps"].get("candidate", {}).get("result") != asdict(self.candidate)
            ):
                raise AccessDenied(
                    "Canonical current merge target or candidate changed during review"
                )
            observed = self.candidates.reconcile(self.candidate.operation_ref)
            if observed != self.candidate:
                raise AccessDenied("Prepared candidate changed during review")
