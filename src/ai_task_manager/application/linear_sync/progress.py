from dataclasses import asdict, dataclass
from uuid import uuid4

from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.linear.progress_store import ProgressStore
from ai_task_manager.adapters.linear.prose import LinearMarkdownRenderer
from ai_task_manager.application.intake import prose_digest
from ai_task_manager.domains.work.errors import ApprovalRequired, WorkError
from ai_task_manager.domains.work.models import Approval, Scope, TaskProse
from ai_task_manager.domains.work.ports import RejectProofs


def progress_prose(body):
    return TaskProse("Task progress update", body, False, ("progress",))


@dataclass(frozen=True)
class ProgressApprovalContext:
    scope: Scope
    task_id: str
    task_prose_digest: str


class ProgressPublisher:
    def __init__(
        self,
        core,
        client,
        journal,
        *,
        approval_policy,
        approvals=None,
        approval_context=None,
        renderer=None,
        clock=None,
    ):
        self.core, self.client = core, client
        self.journal = journal
        self.approval_policy, self.approvals = approval_policy, approvals or RejectProofs()
        self.approval_context = approval_context
        self.renderer = renderer or LinearMarkdownRenderer()
        self.store = ProgressStore(journal, **({"clock": clock} if clock else {}))

    def _approved(self, prose, approval_ref):
        if (
            not isinstance(prose, TaskProse)
            or prose.title != "Task progress update"
            or prose.code_bearing is not False
            or prose.required_work != ("progress",)
            or not isinstance(prose.body, str)
            or not prose.body.strip()
        ):
            raise ApprovalRequired("Progress needs its reviewed purpose and exact content")
        rendered = self.renderer.render(prose)
        approval = self.approvals.verify_approval(approval_ref)
        digest = prose_digest(prose, rendered)
        if (
            not isinstance(approval, Approval)
            or approval.policy != self.approval_policy
            or approval.digest != digest
            or approval.representation != rendered.representation
        ):
            raise ApprovalRequired("Progress approval does not match content and frozen references")
        return rendered, digest

    def submit(self, handle, task_id, prose, approval_ref, operation_key):
        _, scope = self.core.session_binding(handle)
        task = self.core.get_task(handle, task_id)
        self._bound(scope, task)
        if task.issue_id is None:
            raise LinearError("progress_requires_confirmed_issue")
        rendered, digest = self._approved(prose, approval_ref)
        return self.store.submit(
            scope,
            task.id,
            operation_key,
            {
                "connection_digest": self.client.check_connection(scope).digest,
                "issue_id": task.issue_id,
                "task_prose_digest": task.prose_digest,
                "prose": asdict(progress_prose(rendered.body)),
                "representation": rendered.representation,
                "digest": digest,
                "approval_ref": approval_ref,
            },
        )

    def _context(self, handle, row):
        _, scope = self.core.session_binding(handle)
        task = self.core.get_task(handle, row["task_id"])
        self._bound(scope, task)
        import json

        payload = json.loads(row["payload"])
        prose = TaskProse(
            **{**payload["prose"], "required_work": tuple(payload["prose"]["required_work"])}
        )
        rendered, digest = self._approved(prose, payload["approval_ref"])
        if (
            payload.get("connection_digest") != self.client.check_connection(scope).digest
            or task.issue_id != payload["issue_id"]
            or task.prose_digest != payload["task_prose_digest"]
            or rendered.body != prose.body
            or digest != payload["digest"]
        ):
            raise ApprovalRequired("Task or progress scope changed after approval")
        return task, payload

    def _bound(self, scope, task):
        if self.approval_context != ProgressApprovalContext(scope, task.id, task.prose_digest):
            raise ApprovalRequired(
                "Progress writer context does not match the requested task and session"
            )

    def publish_pending(self, handle, publication_id):
        _, scope = self.core.session_binding(handle)
        row = self.store.get(scope, publication_id)
        task, payload = self._context(handle, row)
        if row["state"] == "confirmed":
            return row
        owner = f"progress:{uuid4()}"
        row = self.store.claim(
            scope, publication_id, owner, self.client.comment_capability is not None
        )
        if row is None:
            return self.store.get(scope, publication_id)
        sent = False
        try:
            if self.client.binding is None:
                self.client.verify_binding()
            self.client.check_connection(scope, refresh=True)
            issue = self.client.require_delegate(self.client.read_issue(task.issue_id))
            if issue.assignee_id != scope.principal:
                raise LinearError("progress_issue_not_owned")
            if issue.title != task.title or issue.body != task.body:
                raise LinearError("progress_issue_scope_changed")
            # Recheck the scoped writer view after network preflight, before effect.
            self._context(handle, row)
            self.store.mark_sent(scope, publication_id, owner)
            sent = True
            result = self.client.create_comment(
                task.issue_id, payload["prose"]["body"], row["candidate_id"]
            )
            self.store.record_response(scope, publication_id, owner, result.id)
            if row["candidate_id"] and result.id != row["candidate_id"]:
                raise LinearError("comment_uuid_not_honored", uncertain=True)
            readback = self.client.read_comment(result.id)
            task, payload = self._context(handle, self.store.get(scope, publication_id))
            self._validate_readback(scope, task, payload, readback)
            receipt_ref = self.journal.progress_receipt(scope, task, publication_id, readback)
            self.core.sync.record_progress_publication(handle, task.id, receipt_ref)
            return self.store.finish(scope, publication_id, "confirmed")
        except (LinearError, WorkError) as exc:
            category = exc.category if isinstance(exc, LinearError) else type(exc).__name__
            return self.store.finish(
                scope, publication_id, "uncertain" if sent else "failed", category
            )

    def _validate_readback(self, scope, task, payload, readback):
        self.client.check_connection(scope, refresh=True)
        self.client.require_delegate(readback.issue)
        if (
            readback.issue.id != task.issue_id
            or readback.issue.assignee_id != scope.principal
            or readback.body != payload["prose"]["body"]
            or readback.author_id != scope.principal
            or readback.issue.title != task.title
            or readback.issue.body != task.body
        ):
            raise LinearError("progress_readback_conflict", uncertain=True)

    def get(self, handle, publication_id):
        _, scope = self.core.session_binding(handle)
        row = self.store.get(scope, publication_id)
        self.core.get_task(handle, row["task_id"])
        return row

    def publish(self, handle, task_id, prose, approval_ref, operation_key):
        row = self.submit(handle, task_id, prose, approval_ref, operation_key)
        return self.publish_pending(handle, row["id"])

    def recover(self, handle, publication_id):
        _, scope = self.core.session_binding(handle)
        row = self.store.get(scope, publication_id)
        task, payload = self._context(handle, row)
        if row["state"] == "confirmed":
            return row
        comment_id = row["comment_id"] or row["candidate_id"]
        if not comment_id:
            raise LinearError("comment_identity_uncertain", uncertain=True)
        if self.client.binding is None:
            self.client.verify_binding()
        readback = self.client.read_comment(comment_id)
        self._validate_readback(scope, task, payload, readback)
        receipt_ref = self.journal.progress_receipt(scope, task, publication_id, readback)
        self.core.sync.record_progress_publication(handle, task.id, receipt_ref)
        return self.store.finish(scope, publication_id, "confirmed")

    def retry_unsent(self, handle, publication_id):
        _, scope = self.core.session_binding(handle)
        self._context(handle, self.store.get(scope, publication_id))
        self.store.retry_unsent(scope, publication_id)
