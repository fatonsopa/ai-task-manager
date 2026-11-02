"""A lead-declared evaluation graph is never a Linear relationship observation."""

from dataclasses import asdict

from ai_task_manager.config.evaluation_records import EvaluationRecords
from ai_task_manager.config.files import FileSeal, private_path
from ai_task_manager.domains.scheduling.models import DependencySnapshot
from ai_task_manager.domains.work.errors import AccessDenied

from .evaluation_enrollment import EvaluationGrant


class EvaluationScheduling:
    def __init__(self, grant, records):
        if not isinstance(grant, EvaluationGrant) or not isinstance(records, EvaluationRecords):
            raise AccessDenied("An installed local evaluation declaration is required")
        self.grant, self.records = grant, records
        self._bound = (grant, records)
        self._prepared = {}

    def _current(self):
        if self.grant is not self._bound[0] or self.records is not self._bound[1]:
            raise AccessDenied("The evaluation dependency authority changed")

    def prepare(self, ref):
        self._current()
        if ref in self._prepared:
            raise AccessDenied("An installed declaration cannot be reused or renewed")
        grant = self.grant
        grant.require_active()
        record = self.records.resolve(ref, "evaluation_dependencies", grant.run_id)
        data = record.data
        task = grant.core.get_task(grant._handle, grant._task_id)
        grant.require_task(task)
        session, scope = grant.core.session_binding(grant._handle)
        graph = grant.core.scheduling.explain(grant._handle, task.id)
        fields = {
            "grant_ref",
            "case_id",
            "scope",
            "session_id",
            "claim_store",
            "task_id",
            "issue_id",
            "prose_digest",
            "source_revision",
            "previous_graph_version",
            "observed_at",
            "expires_at",
            "completeness",
            "dependencies",
            "priority",
        }
        if (
            set(data) != fields
            or data["grant_ref"] != grant.ref
            or data["case_id"] != grant.data["case_id"]
            or data["scope"] != asdict(scope)
            or data["session_id"] != session
            or data["claim_store"] != grant.claim_store.context
            or (data["task_id"], data["issue_id"], data["prose_digest"])
            != (task.id, task.issue_id, task.prose_digest)
            or type(data["source_revision"]) is not int
            or data["source_revision"] < 1
            or type(data["previous_graph_version"]) is not int
            or data["previous_graph_version"] != graph["graph_version"]
            or data["dependencies"] != []
            or data["completeness"] != "complete"
            or data["priority"] != "normal"
            or data["expires_at"] > grant.data["expires_at"]
            or data["expires_at"] <= grant.journal.clock()
            or data["observed_at"] > grant.journal.clock()
        ):
            raise AccessDenied(
                "The complete empty case graph does not match current task authority"
            )
        source = FileSeal(private_path(self.records.bindings[ref]["path"]))
        if source.sha256 != record.sha256:
            raise AccessDenied("The installed dependency declaration changed")
        snapshot = DependencySnapshot(
            scope,
            task.id,
            task.prose_digest,
            task.issue_id,
            "lead-evaluation-case:" + data["case_id"],
            data["source_revision"],
            data["observed_at"],
            data["expires_at"],
            "complete",
            "normal",
            (),
            data["previous_graph_version"],
        )
        self._prepared[ref] = (snapshot, source, source.metadata(), grant.prepare_current())
        return ref

    def verify_snapshot(self, ref):
        """Core resolves only a prepared local receipt, without provider/file-content I/O."""
        self._current()
        value = self._prepared.get(ref)
        if value is None:
            raise AccessDenied("The current evaluation declaration was not prepared")
        snapshot, source, metadata, grant = value
        grant.check()
        if source.metadata() != metadata or snapshot.expires_at <= self.grant.journal.clock():
            raise AccessDenied("The prepared evaluation declaration changed or expired")
        return snapshot
