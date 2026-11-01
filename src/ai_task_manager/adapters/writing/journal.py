"""Task and progress approvals have separate verifier views."""

from dataclasses import asdict

from ai_task_manager.domains.work.errors import ApprovalRequired
from ai_task_manager.domains.work.models import Approval, ApprovalPolicy, TaskProse


class ApprovalView:
    def __init__(self, writer, purpose, scope=None, task_id=None, task_prose_digest=None):
        self.writer, self.purpose, self.scope = writer, purpose, scope
        self.task_id, self.task_prose_digest = task_id, task_prose_digest

    def record(self, ref):
        self.writer.package.verify()
        data = self.writer.journal._get(ref, "writing-" + self.purpose + ":")
        if data["purpose"] != self.purpose or data["policy"] != asdict(
            self.writer.package.policy()
        ):
            raise ApprovalRequired("Writing approval belongs to another purpose or version")
        if self.scope is not None and data["scope"] != asdict(self.scope):
            raise ApprovalRequired("Writing approval belongs to another session")
        if self.purpose == "progress" and (data.get("task_id"), data.get("task_prose_digest")) != (
            self.task_id,
            self.task_prose_digest,
        ):
            raise ApprovalRequired("Progress approval belongs to another task or work scope")
        return data

    def verify_approval(self, ref):
        row = self.record(ref)
        return Approval(row["digest"], ApprovalPolicy(**row["policy"]), row["representation"])

    def prose(self, ref):
        row = self.record(ref)
        return TaskProse(**{**row["prose"], "required_work": tuple(row["prose"]["required_work"])})
