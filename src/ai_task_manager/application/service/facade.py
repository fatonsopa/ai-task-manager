"""One scoped service is shared by protocol and terminal entry points."""

from ai_task_manager.domains.work.errors import ValidationError, WorkError

from .handlers import HANDLERS
from .invocations import Invocation
from .models import Unavailable, failure, result, validate
from .projection import attempt_view, task_view


class ApplicationService:
    def __init__(
        self,
        core,
        handle,
        *,
        writer=None,
        decisions=None,
        grant_ref=None,
        provider=None,
        workers=None,
        publisher=None,
        sessions=None,
        inbound=None,
        seal=None,
        existing=None,
        claim_store=None,
        dependencies=None,
        workflows=None,
    ):
        self.core, self.handle, self.writer, self.decisions = core, handle, writer, decisions
        self.grant_ref, self.provider, self.workers = grant_ref, provider, workers
        self.publisher, self.sessions, self.seal = publisher, sessions, seal
        self.inbound = inbound
        self.existing, self.claim_store = existing, claim_store
        self.dependencies = dependencies
        self.workflows = workflows
        self.session_id, self.scope = core.session_binding(handle)

    def confirm(self, task):
        task = self.core.get_task(self.handle, task.id)
        if task.sync_state != "confirmed" and self.provider is not None:
            task = self.provider.confirm_pending(self.handle, task.id)
        return result(
            task_view(self, task), "confirmed" if task.sync_state == "confirmed" else "pending"
        )

    def _required(self, name):
        value = getattr(self, name)
        if value is None:
            raise Unavailable(name.replace("_", " ") + " integration is not configured")
        return value

    def call(self, operation, data):
        try:
            validate(operation, data)
            if self.seal:
                self.seal.check()
            if self.claim_store:
                self.claim_store.check()
            current_id, current_scope = self.core.session_binding(self.handle)
            if (current_id, current_scope) != (self.session_id, self.scope):
                raise ValidationError("Session binding changed")
            forward = getattr(self.workflows, "forward", None)
            if forward is not None:
                return forward(operation, data)
            invocation = None
            if "operation_key" in data and operation not in {"workflow_run", "workflow_resume"}:
                self._required("decisions")
                invocation = Invocation(
                    self.decisions.journal,
                    self.scope,
                    operation,
                    data["operation_key"],
                    data,
                    claim_store=self.claim_store.context if self.claim_store else None,
                )
                cached = invocation.cached()
                if cached is not None and operation != "task_select":
                    if operation in {
                        "task_action",
                        "task_revise",
                        "task_submit",
                        "task_admit",
                        "task_accept_external_prose",
                    }:
                        current = self.core.get_task(self.handle, cached["data"]["id"])
                        return result(
                            task_view(self, current),
                            "confirmed" if current.sync_state == "confirmed" else "pending",
                        )
                    if operation == "worker_start":
                        current = self.core.get_attempt(self.handle, cached["data"]["id"])
                        return result(attempt_view(self, current))
                    return cached
            envelope = self._invoke(operation, data, invocation)
            if invocation and envelope["ok"]:
                invocation.finish(envelope)
            return envelope
        except (WorkError, ValueError, KeyError, TypeError) as error:
            return failure(error)
        except Exception as error:
            return failure(error)

    def _invoke(self, operation, data, invocation):
        return HANDLERS[operation](self, data, invocation)
