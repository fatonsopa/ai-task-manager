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


def decision_prose(fields):
    import re

    from ai_task_manager.domains.work.errors import ValidationError

    options = fields["options"]
    ids = [o["id"] for o in options]
    if (
        not 2 <= len(options) <= 8
        or len(set(ids)) != len(ids)
        or fields["recommendation"] not in ids
        or any(not re.fullmatch(r"[A-Za-z0-9]{1,16}", v) for v in ids)
        or any(
            not isinstance(fields[k], str) or not fields[k].strip()
            for k in ("title", "context", "rationale")
        )
        or any(not o["label"].strip() or not o["description"].strip() for o in options)
    ):
        raise ValidationError(
            "Decision needs context, distinct options, recommendation and rationale"
        )
    text = [
        fields["title"],
        fields["context"],
        fields["rationale"],
        *[value for option in options for value in (option["label"], option["description"])],
    ]
    if any(re.search(r"(?m)^#{1,6} |linear\.app/[^\s]+/profiles/", value) for value in text):
        raise ValidationError(
            "Decision fields cannot inject document headings or another human mention"
        )
    if any("\n" in option["label"] or "\r" in option["label"] for option in options):
        raise ValidationError("Option labels must remain one line")
    body = "## Context\n\n" + fields["context"] + "\n\n## Options considered\n\n"
    body += "\n\n".join(
        "### Option " + o["id"] + ": " + o["label"] + "\n\n" + o["description"] for o in options
    )
    body += "\n\n## Recommended option (" + fields["recommendation"] + ")\n\n" + fields["rationale"]
    body += "\n\n## Outcome\n\nPending an observed human choice.\n\n" + fields["recipient"]["url"]
    return TaskProse(fields["title"], body, False, ("decision",))


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
        check_authority=lambda: None,
    ):
        self.core, self.client = core, client
        self.check_authority = check_authority
        self.journal = journal
        self.approval_policy, self.approvals = approval_policy, approvals or RejectProofs()
        self.approval_context = approval_context
        self.renderer = renderer or LinearMarkdownRenderer()
        self.store = ProgressStore(journal, **({"clock": clock} if clock else {}))

    def _approved(self, prose, approval_ref):
        if isinstance(prose, TaskProse) and prose.required_work == ("decision",):
            record = self.approvals.record(approval_ref)
            metadata = record.get("decision")
            if (
                record.get("purpose") != "decision"
                or record.get("kind") != "decision"
                or not metadata
            ):
                raise ApprovalRequired("Decision publication needs exact decision-purpose approval")
            rendered = self.renderer.render_decision(prose, metadata["recipient"]["url"])
            approval = self.approvals.verify_approval(approval_ref)
            digest = prose_digest(prose, rendered)
            if (
                approval.policy != self.approval_policy
                or approval.digest != digest
                or approval.representation != rendered.representation
                or rendered
                != self.renderer.render_decision(
                    decision_prose(metadata), metadata["recipient"]["url"]
                )
            ):
                raise ApprovalRequired("Decision Record content differs from its approved fields")
            return rendered, digest
        if (
            not isinstance(prose, TaskProse)
            or prose.title != "Task progress update"
            or prose.code_bearing is not False
            or prose.required_work != ("progress",)
            or not isinstance(prose.body, str)
            or not prose.body.strip()
        ):
            raise ApprovalRequired("Progress needs its reviewed purpose and exact content")
        if "linear.app/" in prose.body and "/profiles/" in prose.body:
            raise ApprovalRequired("Routine progress cannot publish a human decision mention")
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
        payload = {
            "connection_digest": self.client.check_connection(scope).digest,
            "issue_id": task.issue_id,
            "task_prose_digest": task.prose_digest,
            "prose": asdict(TaskProse(prose.title, rendered.body, False, prose.required_work)),
            "representation": rendered.representation,
            "digest": digest,
            "approval_ref": approval_ref,
        }
        if prose.required_work == ("decision",):
            payload["decision"] = self.approvals.record(approval_ref)["decision"]
        return self.store.submit(
            scope,
            task.id,
            operation_key,
            payload,
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
            or (
                "decision" in payload
                and task.decision_revision != payload["decision"]["decision_revision"]
            )
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

    def _publish_comment(self, handle, publication_id):
        _, scope = self.core.session_binding(handle)
        row = self.store.get(scope, publication_id)
        self.check_authority()
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
            self.check_authority()
            self.store.mark_sent(scope, publication_id, owner)
            sent = True
            result = self.client.create_comment(
                task.issue_id, payload["prose"]["body"], row["candidate_id"]
            )
            self.check_authority()
            self.store.record_response(scope, publication_id, owner, result.id)
            if row["candidate_id"] and result.id != row["candidate_id"]:
                raise LinearError("comment_uuid_not_honored", uncertain=True)
            readback = self.client.read_comment(result.id)
            task, payload = self._context(handle, self.store.get(scope, publication_id))
            self.check_authority()
            self._validate_readback(scope, task, payload, readback)
            self.check_authority()
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
            or not self._comment_matches(payload, readback.body)
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

    def _recover_comment(self, handle, publication_id):
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
        self.check_authority()
        readback = self.client.read_comment(comment_id)
        self.check_authority()
        self._validate_readback(scope, task, payload, readback)
        self.check_authority()
        receipt_ref = self.journal.progress_receipt(scope, task, publication_id, readback)
        self.core.sync.record_progress_publication(handle, task.id, receipt_ref)
        return self.store.finish(scope, publication_id, "confirmed")

    def retry_unsent(self, handle, publication_id):
        _, scope = self.core.session_binding(handle)
        self._context(handle, self.store.get(scope, publication_id))
        self.store.retry_unsent(scope, publication_id)

    @staticmethod
    def _comment_matches(payload, body):
        expected = payload["prose"]["body"]
        if "decision" not in payload:
            return body == expected
        url = payload["decision"]["recipient"]["url"]
        prefix = expected[: -len(url)]
        if not body.startswith(prefix):
            return False
        suffix = body[len(prefix) :]
        if suffix == url:
            return True
        # Only the mention representation may change; DR context/options remain exact.
        from markdown_it import MarkdownIt

        tokens = MarkdownIt().parseInline(suffix)[0].children or []
        return (
            len(tokens) == 3
            and tokens[0].type == "link_open"
            and tokens[0].attrGet("href") == url
            and tokens[1].type == "text"
            and tokens[2].type == "link_close"
        )

    @staticmethod
    def _decision_view(row):
        if row.get("decision_state") is None:
            return row
        value = dict(row, comment_state=row["state"])
        value["state"] = (
            "confirmed"
            if row["state"] == "confirmed" and row["label_state"] == "confirmed"
            else "uncertain"
            if row["state"] == "uncertain" or row["label_state"] in {"sent", "uncertain"}
            else "pending"
        )
        return value

    def prepare_decision(self, handle, task_id, fields, writer):
        _, scope = self.core.session_binding(handle)
        task = self.core.get_task(handle, task_id)
        self.check_authority()
        context = self.client.decision_context(task.issue_id)
        self.check_authority()
        if (
            context["issue"].assignee_id != scope.principal
            or context["issue"].title != task.title
            or context["issue"].body != task.body
        ):
            raise ApprovalRequired("Decision task scope changed")
        metadata = {
            **fields,
            "recipient": context["recipient"],
            "label_id": context["label_id"],
            "label_name": context["label_name"],
            "decision_revision": task.decision_revision,
        }
        prose = decision_prose(metadata)
        return writer.prepare(
            scope, "decision", prose, purpose="decision", task=task, decision=metadata
        )

    def _decision_context(self, handle, row):
        task, payload = self._context(handle, row)
        if "decision" not in payload:
            raise ApprovalRequired("Not a decision request")
        self.check_authority()
        context = self.client.decision_context(task.issue_id)
        self.check_authority()
        task, payload = self._context(handle, row)
        m = payload["decision"]
        if (
            context["recipient"] != m["recipient"]
            or context["label_id"] != m["label_id"]
            or task.decision_revision != m["decision_revision"]
            or context["issue"].assignee_id != self.approval_context.scope.principal
            or context["issue"].title != task.title
            or context["issue"].body != task.body
        ):
            raise ApprovalRequired("Current decision recipient, authority or work changed")
        return task, payload, context

    def _label(self, handle, row, *, add):
        _, scope = self.core.session_binding(handle)
        task, payload, context = self._decision_context(handle, row)
        label = payload["decision"]["label_id"]
        present = label in context["labels"]
        if row["label_state"] in {"sent", "uncertain"}:
            if not row["label_response"]:
                return self._decision_view(row)  # Lost label reply is not a completed mutation.
            if present != add:
                raise LinearError("decision_label_readback_conflict", uncertain=True)
        elif present != add:
            self.check_authority()
            self.store.label_stage(scope, row["id"], "sent")
            try:
                response = self.client.change_decision_label(task.issue_id, label, add=add)
                self.check_authority()
                self.store.label_stage(scope, row["id"], "sent", response)
                _, _, observed = self._decision_context(handle, self.store.get(scope, row["id"]))
                if (label in observed["labels"]) != add:
                    raise LinearError("decision_label_readback_conflict", uncertain=True)
            except (LinearError, WorkError):
                self.store.label_stage(scope, row["id"], "uncertain")
                raise
        self.check_authority()
        self.store.label_stage(scope, row["id"], "confirmed")
        return self._decision_view(self.store.get(scope, row["id"]))

    def publish_pending(self, handle, publication_id):
        _, scope = self.core.session_binding(handle)
        row = self.store.get(scope, publication_id)
        if row["decision_state"] is None:
            return self._publish_comment(handle, publication_id)
        self._decision_context(handle, row)
        if row["decision_state"] != "open" or (
            row["state"] == "confirmed" and row["label_state"] == "confirmed"
        ):
            return self._decision_view(row)
        owner = "decision:" + str(uuid4())
        anchor = self.store.decision_claim(scope, publication_id, owner)
        try:
            if row["state"] in {"uncertain", "leased"}:
                row = self._recover_comment(handle, publication_id)
            else:
                row = self._publish_comment(handle, publication_id)
            if row["state"] != "confirmed":
                return self._decision_view(row)
            return self._label(handle, row, add=True)
        finally:
            self.store.decision_release(anchor, owner)

    def recover(self, handle, publication_id):
        _, scope = self.core.session_binding(handle)
        row = self.store.get(scope, publication_id)
        return (
            self.publish_pending(handle, publication_id)
            if row["decision_state"] is not None
            else self._recover_comment(handle, publication_id)
        )

    def resolve_decision(self, handle, publication_id, human_ref, operation_key, observations):
        import json

        from ai_task_manager.adapters.decisions.human import verify_receipt

        _, scope = self.core.session_binding(handle)
        row = self.store.get(scope, publication_id)
        task, payload, _ = self._decision_context(handle, row)
        if row["state"] != "confirmed":
            raise ApprovalRequired("Decision comment is not confirmed")
        if payload["decision"]["recipient"]["id"] != scope.principal:
            raise ApprovalRequired("Current enrolled human is not the decision recipient")
        observed = (
            verify_receipt(observations.journal, human_ref)
            if row["resolution_key"]
            else observations.verify_instruction(human_ref)
        )
        if (
            observed.scope != scope
            or observed.task_id != task.id
            or observed.prose_digest != task.prose_digest
            or observed.decision_request_id != publication_id
            or observed.decision_digest != row["fingerprint"]
            or observed.decision_option_id not in {o["id"] for o in payload["decision"]["options"]}
            or observed.revision != task.decision_revision
            or (not row["resolution_key"] and observed.task_version != task.version)
        ):
            raise ApprovalRequired("Resolution needs this request's actual current human choice")
        resolution = {
            "human_ref": human_ref,
            "option_id": observed.decision_option_id,
            "request_digest": row["fingerprint"],
        }
        if row["resolution_key"] and (
            row["resolution_key"] != operation_key or json.loads(row["resolution"]) != resolution
        ):
            raise ApprovalRequired("Resolution differs from the original key/source")
        if row["decision_state"] == "resolved":
            return self._decision_view(row)
        owner = "decision-resolve:" + str(uuid4())
        anchor = self.store.decision_claim(scope, publication_id, owner)
        try:
            self.check_authority()
            self.store.record_resolution(scope, publication_id, operation_key, resolution)
            row = self.store.get(scope, publication_id)
            result = self._label(
                handle, row, add=self.store.open_decisions(scope, task.issue_id) > 0
            )
            if result["label_state"] == "confirmed":
                self.store.finish_resolution(scope, publication_id)
            return self._decision_view(self.store.get(scope, publication_id))
        finally:
            self.store.decision_release(anchor, owner)
