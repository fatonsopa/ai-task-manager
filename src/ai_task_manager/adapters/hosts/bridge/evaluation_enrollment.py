"""Lead-installed evaluation authority is confined to one isolated provider/Core case."""

import math
from dataclasses import asdict, dataclass
from pathlib import Path

from ai_task_manager.adapters.decisions.authority import HostGrant
from ai_task_manager.config.evaluation_records import EvaluationRecords
from ai_task_manager.config.files import FileSeal, digest, private_path
from ai_task_manager.config.sessions import SessionFile
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.domains.work.models import Scope

from .invocations import PreparedInvocation

EVALUATION_ACTIONS = frozenset(
    {"intake", "enqueue", "start", "hold", "cancel", "non_code_complete"}
)


@dataclass(frozen=True)
class PreparedEvaluationGrant:
    grant: object
    source_metadata: tuple
    native: object
    process: object
    policy_metadata: object

    def check(self):
        """Only local metadata, sticky state and the existing kernel handle."""
        grant = self.grant
        if (
            grant._revoked
            or grant.source is not grant._bound[-1]
            or digest(grant.data) != grant._data_digest
            or grant.scope != Scope(**grant.data["scope"])
            or grant.source.metadata() != self.source_metadata
            or grant.journal.clock() >= grant.data["expires_at"]
            or grant.observations._observed[grant.data["bootstrap_ref"]].native is not self.native
            or self.native.process is not self.process
            or self.process._closed
        ):
            raise AccessDenied("Prepared evaluation authority is stale, changed or revoked")
        self.process.check_current_liveness()
        with self.native.startup.model_guard.startup_current_boundary():
            self.native.policy.check_admission_metadata(self.policy_metadata)


class EvaluationGrant:
    """An installer capability, not human acceptance or a model-callable issuer."""

    def __init__(self, records, ref, run_id, observations, core, claim_store, journal, expected):
        if not isinstance(records, EvaluationRecords):
            raise AccessDenied("A lead-installed record verifier is required")
        record = records.resolve(ref, "evaluation_grant", run_id)
        source_binding = records.bindings[ref]
        self.source = FileSeal(private_path(source_binding["path"]))
        if self.source.sha256 != record.sha256:
            raise AccessDenied("The installed evaluation grant changed")
        self.records, self.ref, self.run_id = records, ref, run_id
        self.observations, self.core, self.claim_store, self.journal = (
            observations,
            core,
            claim_store,
            journal,
        )
        self.expected, self.data = expected, record.data
        self._data_digest = digest(record.data)
        self._record_binding = tuple(sorted(source_binding.items()))
        self._bound = (records, observations, core, claim_store, journal, expected, self.source)
        self._revoked = False
        self._task_id = None
        self._handle = None
        self._validate()

    def _validate(self):
        data = self.data
        fields = {
            "version",
            "purpose",
            "case_id",
            "bootstrap_ref",
            "bootstrap_process",
            "bootstrap_configuration_digest",
            "source_template",
            "scope",
            "team_id",
            "team_key",
            "claim_store",
            "task_prose_digest",
            "actions",
            "worker_batch_id",
            "expires_at",
            "model",
            "effort",
            "billing_basis_ref",
        }
        if (
            set(data) != fields
            or data["version"] != 1
            or data["purpose"] != "isolated-native-profile-evaluation"
            or not isinstance(data["case_id"], str)
            or not data["case_id"]
            or not isinstance(data["actions"], list)
            or not data["actions"]
            or not set(data["actions"]) <= EVALUATION_ACTIONS
            or len(data["actions"]) != len(set(data["actions"]))
            or data["team_key"] != "TST"
            or self.expected.team_key != "TST"
            or data["team_id"] != self.expected.team_id
            or isinstance(data["expires_at"], bool)
            or not isinstance(data["expires_at"], (int, float))
            or not math.isfinite(data["expires_at"])
            or any(
                not isinstance(data[k], str) or not data[k]
                for k in (
                    "task_prose_digest",
                    "worker_batch_id",
                    "model",
                    "effort",
                    "billing_basis_ref",
                )
            )
        ):
            raise AccessDenied("The exact isolated evaluation grant is incomplete or unsupported")
        scope = Scope(**data["scope"])
        if (
            scope
            != Scope(
                "linear",
                self.expected.workspace_id,
                self.expected.viewer_id,
                self.expected.project_id,
                "evaluation:" + data["case_id"],
            )
            or data["claim_store"] != self.claim_store.context
            or Path(self.claim_store.path).absolute() != Path(self.core.context.db.path).absolute()
            or getattr(self.core.context.db, "claim_context", None) != self.claim_store.context
        ):
            raise AccessDenied("Evaluation cannot elect another project, session or Core store")
        self.scope = scope
        self.require_active()

    def require_active(self):
        if self._revoked or any(
            a is not b
            for a, b in zip(
                (
                    self.records,
                    self.observations,
                    self.core,
                    self.claim_store,
                    self.journal,
                    self.expected,
                    self.source,
                ),
                self._bound,
            )
        ):
            raise AccessDenied("Evaluation authority is revoked or rebound")
        if (
            self.scope != Scope(**self.data["scope"])
            or self.data["claim_store"] != self.claim_store.context
            or Path(self.claim_store.path).absolute() != Path(self.core.context.db.path).absolute()
            or getattr(self.core.context.db, "claim_context", None) != self.claim_store.context
        ):
            raise AccessDenied("The original evaluation scope or Core store changed")
        if tuple(sorted(self.records.bindings.get(self.ref, {}).items())) != self._record_binding:
            raise AccessDenied("The installed evaluation source binding changed")
        record = self.records.resolve(self.ref, "evaluation_grant", self.run_id)
        if record.data != self.data:
            raise AccessDenied("The original evaluation grant cannot be replaced")
        self.source.check()
        observed = self.observations.verify(self.data["bootstrap_ref"])
        payload = observed["payload"]
        if (
            self.journal.clock() >= self.data["expires_at"]
            or self.data["expires_at"] > observed["expires_at"]
            or self.data["bootstrap_process"] != payload["process"]
            or self.data["bootstrap_configuration_digest"]
            != payload["binding"]["configuration_digest"]
            or self.data["source_template"] != payload["template"]
            or self.data["scope"] != payload["binding"]["scope"]
            or (self.data["model"], self.data["effort"])
            != (payload["model"], payload["requested_effort"])
        ):
            raise AccessDenied("Evaluation lacks its exact current authenticated startup")
        self.claim_store.check()
        return observed

    def prepare_current(self):
        self.require_active()
        native = self.observations._observed[self.data["bootstrap_ref"]].native
        return PreparedEvaluationGrant(
            self, self.source.metadata(), native, native.process, native.policy.prepare_admission()
        )

    def revoke(self):
        self._revoked = True

    def verify_enrollment(self, ref):
        if ref != self.ref:
            raise AccessDenied("Another evaluation grant cannot enroll this session")
        self.require_active()
        return HostGrant(
            self.scope,
            self.scope.logical_session,
            tuple(self.data["actions"]),
            self.ref,
            self.data["expires_at"],
        )

    def verify_instruction(self, ref):
        raise EvidenceRequired("Evaluation authority cannot issue human acceptance")

    def verify_host_observation(self, ref):
        raise EvidenceRequired("Evaluation authority cannot manufacture host notifications")

    def bind_task(self, handle, task_id):
        self.require_active()
        _, scope = self.core.session_binding(handle)
        task = self.core.get_task(handle, task_id)
        if (
            scope != self.scope
            or task.prose_digest != self.data["task_prose_digest"]
            or not task.issue_id
            or task.code_bearing
            or len(self.core.list_tasks(handle)) != 1
            or (self._task_id is not None and self._task_id != task_id)
        ):
            raise AccessDenied("Evaluation owns only its one confirmed non-code task")
        if self._task_id is not None:
            raise AccessDenied("An evaluation task binding cannot be reused or renewed")
        self.journal._append(
            "trace",
            {
                "effect": "evaluation_task_bound",
                "grant_ref": self.ref,
                "task_id": task.id,
                "issue_id": task.issue_id,
                "prose_digest": task.prose_digest,
            },
            sources=(self.data["bootstrap_ref"],),
            expires_at=self.data["expires_at"],
        )
        self._task_id, self._handle = task_id, handle
        self._task_binding = (task_id, handle, task.issue_id, task.prose_digest)

    def require_task(self, task):
        self.require_active()
        if (
            self._task_id is None
            or (self._task_id, self._handle, task.issue_id, task.prose_digest) != self._task_binding
            or task.id != self._task_id
            or task.prose_digest != self.data["task_prose_digest"]
            or task.code_bearing
        ):
            raise AccessDenied("This task is outside the isolated evaluation purpose")

    def require_worker(self, plan, observed):
        self.require_active()
        if (
            observed["ref"] != self.data["bootstrap_ref"]
            or plan.task_id != self._task_id
            or plan.batch_id != self.data["worker_batch_id"]
            or (plan.host, plan.model, plan.effort)
            != ("claude", self.data["model"], self.data["effort"])
            or plan.deadline > self.data["expires_at"]
            or plan.billing_basis_ref != self.data["billing_basis_ref"]
        ):
            raise AccessDenied("The worker plan is outside the exact evaluation grant")
        self.require_task(self.core.get_task(self._handle, self._task_id))

    def require_binding(self, binding):
        self.require_active()
        session, scope = self.core.session_binding(self._handle)
        attempt = self.core.get_attempt(self._handle, binding.attempt_id)
        if (
            scope != self.scope
            or binding.scope != scope
            or binding.core_session != session
            or attempt.task_id != self._task_id
            or binding.host != "claude"
            or binding.deadline > self.data["expires_at"]
        ):
            raise AccessDenied("The actual worker belongs to another evaluation")


class EvaluationEnrollment:
    def __init__(self, grant, client, provider_journal):
        if not isinstance(grant, EvaluationGrant):
            raise AccessDenied("A lead-installed bound evaluation grant is required")
        self.grant, self.client, self.provider_journal = grant, client, provider_journal

    def establish(self, permit, destination):
        grant = self.grant
        grant.require_active()
        if (
            not isinstance(permit, PreparedInvocation)
            or permit.mode != "launch"
            or permit.binding.purpose != "enrollment"
            or permit.binding.run_id != grant.run_id
            or permit.binding.deadline > grant.data["expires_at"]
        ):
            raise AccessDenied("Enrollment needs the exact finite registered local invocation")
        original = grant.observations._observed[
            grant.data["bootstrap_ref"]
        ].native.permits.initialization.native
        if permit.issuer.controller is not original.issuer.controller or any(
            getattr(permit.binding, name) != getattr(original.binding, name)
            for name in (
                "run_id",
                "policy_revision",
                "worker_identity",
                "attempt_id",
                "host",
                "configuration_digest",
                "model",
                "effort",
            )
        ):
            raise AccessDenied("Enrollment belongs to another root/account/native owner")
        # Actual provider I/O precedes the short Root/Core publication boundary.
        binding = self.client.verify_binding()
        if binding.expected != grant.expected:
            raise AccessDenied("Actual provider authentication differs from the evaluation")
        core = grant.core
        with core.context.db.transaction() as conn:
            if conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] != 0:
                raise AccessDenied("Evaluation enrollment requires a fresh Core store")
            if conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] != 0:
                raise AccessDenied("Evaluation enrollment cannot adopt existing tasks")
        principal = self.provider_journal.bind_principal(binding, grant.scope)
        prepared = grant.prepare_current()
        if not grant.journal.intend(
            "evaluation-enrollment:" + grant.ref,
            {"scope": asdict(grant.scope), "destination": str(destination)},
        ):
            raise AccessDenied("Enrollment is already complete or uncertain; reconcile")
        with permit.launch_boundary() as report:
            prepared.check()
            handle = core.register_session(principal)
            SessionFile(destination).save(handle, grant.scope)
            report(process_pid=None, process_start_identity=None)
        ref = grant.journal._append(
            "trace",
            {
                "effect": "evaluation_session_established",
                "grant_ref": grant.ref,
                "scope": asdict(grant.scope),
                "claim_store": grant.claim_store.context,
                "human_acceptance": "not_established",
            },
            sources=(grant.data["bootstrap_ref"],),
            expires_at=grant.data["expires_at"],
        )
        permit.issuer.controller.record_outcome(
            permit.reference, outcome="succeeded", process_stopped=True, evidence_ref=ref
        )
        return handle
