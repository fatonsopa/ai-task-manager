"""Trusted fixture composition over the existing service, writer and provider."""

import threading
from contextlib import contextmanager

from ai_task_manager.adapters.linear.client import (
    COMMENT_CREATE,
    COMMENT_READ,
    CREATE,
    IDENTITY,
    PROJECT_TEAMS,
    READ,
    STATES,
    UPDATE,
)
from ai_task_manager.application.linear_sync.progress import (
    ProgressApprovalContext,
    ProgressPublisher,
)
from ai_task_manager.application.linear_sync.reconcile import Reconciliation
from ai_task_manager.config.files import digest

from .fixture_http import FixtureRequestTransport
from .ledger import DriverRefused, uuid
from .run_primary import PrimaryRun


class OwnedProductTransport:
    """Reserve actual product candidate UUIDs without adding raw task endpoints."""

    def __init__(self, ledger, transport):
        self.ledger, self.transport = ledger, FixtureRequestTransport(ledger, transport)
        self.local = threading.local()
        self.binding = None

    def bind_service(self, service):
        observed = service.core.session_binding(service.handle)
        if observed != (service.session_id, service.scope) or self.binding not in (None, observed):
            raise DriverRefused("fixture_product_session_changed")
        self.binding = observed

    @contextmanager
    def service_operation(self, service, operation, data):
        self.bind_service(service)
        if getattr(self.local, "context", None) is not None:
            raise DriverRefused("nested_fixture_product_operation")
        self.local.context = (service, operation, data)
        try:
            yield
        finally:
            self.local.context = None

    def _approved(self, service, operation, data, payload, kind):
        s = self.ledger.scope
        if (service.scope.workspace, service.scope.principal, service.scope.project) != (
            s.workspace,
            s.owner,
            s.project,
        ):
            raise DriverRefused("fixture_product_scope_mismatch")
        if kind == "comment":
            if operation != "task_progress":
                raise DriverRefused("reviewed_progress_service_required")
            task = service.core.get_task(service.handle, data["task_id"])
            view = service.writer.approvals_for_progress(service.scope, task.id, task.prose_digest)
            if payload.get("issueId") != task.issue_id:
                raise DriverRefused("fixture_progress_issue_changed")
        else:
            if operation not in {"task_submit", "task_revise"}:
                raise DriverRefused("reviewed_task_service_required")
            view = service.writer.approvals_for_tasks(service.scope)
        # Consumption is only on a new wire effect. Historical service replays
        # are left to the product's original cache/ownership rules.
        prose = view.prose(data["writing_ref"])
        view.verify_approval(data["writing_ref"])
        rendered = service.writer.renderer.render(prose)
        if ("Fixture run: " + self.ledger.run_id) not in rendered.body.splitlines():
            raise DriverRefused("reviewed_fixture_marker_required")
        if kind == "comment":
            if payload.get("body") != rendered.body:
                raise DriverRefused("reviewed_progress_bytes_changed")
        elif payload.get("title") != rendered.title or payload.get("description") != rendered.body:
            raise DriverRefused("reviewed_task_bytes_changed")

    @contextmanager
    def possible_effect_readback(self, key):
        if getattr(self.local, "recovery", None) is not None:
            raise DriverRefused("nested_product_recovery")
        row = self.ledger.get(key)
        if row["state"] not in {"sent", "uncertain"}:
            raise DriverRefused("possible_product_effect_required")
        self.local.recovery = row
        try:
            yield
        finally:
            self.local.recovery = None

    def send(self, query, variables, credentials):
        recovery = getattr(self.local, "recovery", None)
        if recovery is not None:
            s = self.ledger.scope
            allowed = (
                query == (READ if recovery["kind"] == "issue" else COMMENT_READ)
                and variables == {"id": recovery["candidate"]}
                or query == IDENTITY
                and variables == {"team": s.team, "project": s.project}
                or query == STATES
                and variables.get("team") == s.team
                or query == PROJECT_TEAMS
                and variables.get("project") == s.project
            )
            if not allowed:
                raise DriverRefused("product_recovery_context_mismatch")
            self.ledger.create_recovery_read(recovery["key"])
            return self.transport.send(query, variables, credentials)
        if query in {CREATE, COMMENT_CREATE, UPDATE}:
            context = getattr(self.local, "context", None)
            if context is None:
                raise DriverRefused("trusted_product_service_context_required")
            service, operation, data = context
            s = self.ledger.scope
            if (service.scope.workspace, service.scope.principal, service.scope.project) != (
                s.workspace,
                s.owner,
                s.project,
            ):
                raise DriverRefused("fixture_product_scope_mismatch")
            payload = variables["input"]
            if query == UPDATE:
                owned = self.ledger.resource(uuid(variables["id"]))
                if owned["kind"] != "issue" or owned["state"] != "active":
                    raise DriverRefused("owned_active_issue_required")
                if "description" in payload or "title" in payload:
                    self._approved(service, operation, data, payload, "issue")
                self.ledger.request("product_transport")
            else:
                kind = "issue" if query == CREATE else "comment"
                self._approved(service, operation, data, payload, kind)
                if kind == "issue" and (
                    payload.get("teamId"),
                    payload.get("projectId"),
                    payload.get("assigneeId"),
                ) != (s.team, s.project, s.owner):
                    raise DriverRefused("fixture_issue_wire_scope_changed")
                candidate = uuid(payload.get("id"))
                key = "product:" + kind + ":" + candidate
                row = self.ledger.begin(key, kind, candidate, digest(variables))
                if row["state"] != "intent":
                    raise DriverRefused("product_possible_effect_needs_readback")
                self.ledger.charged_create_send(key)
        elif query.lstrip().startswith("mutation"):
            raise DriverRefused("unknown_product_mutation")
        else:
            self.ledger.request("product_transport")
        try:
            return self.transport.send(query, variables, credentials)
        except Exception:
            if query in {CREATE, COMMENT_CREATE}:
                self.ledger.uncertain(key)
            raise


class FixtureProgressFactory:
    """One inspected publisher composition shares the counted product client."""

    def __init__(self, core, client, journal):
        self.core, self.client, self.journal = core, client, journal

    def __call__(self, scope, task, approvals):
        return ProgressPublisher(
            self.core,
            self.client,
            self.journal,
            approval_policy=self.core.context.approval_policy,
            approvals=approvals,
            approval_context=ProgressApprovalContext(scope, task.id, task.prose_digest),
            renderer=self.core.context.renderer,
        )


class PrimaryDriver:
    """The operator supplies genuinely configured product objects, not booleans."""

    def __init__(self, service, administration, product_transport, *, client):
        self.service, self.admin, self.ledger = service, administration, administration.ledger
        self.transport, self.client = product_transport, client
        s = self.ledger.scope
        if (
            product_transport.ledger is not self.ledger
            or client.transport is not product_transport
            or (service.scope.workspace, service.scope.principal, service.scope.project)
            != (s.workspace, s.owner, s.project)
        ):
            raise DriverRefused("fixture_product_composition_mismatch")
        if (
            service.provider.client is not client
            or not isinstance(service.publisher, FixtureProgressFactory)
            or service.publisher.client is not client
        ):
            raise DriverRefused("fixture_product_composition_mismatch")
        if (
            client.create_capability,
            client.comment_capability,
        ) != administration.graphql.product_capabilities():
            raise DriverRefused("fixture_product_uuid_capabilities_required")
        product_transport.bind_service(service)

    def writing(self, operation, data):
        if operation not in {"writing_prepare", "writing_review", "writing_finalize"}:
            raise DriverRefused("unknown_writing_operation")
        # In particular, return the real data.handoff bytes and hashes unchanged.
        return self.service.call(operation, data)

    def invoke(self, operation, data):
        if operation not in {"task_submit", "task_revise", "task_action", "task_progress"}:
            raise DriverRefused("unsupported_fixture_product_operation")
        self.ledger.resource(self.ledger.scope.project)
        with self.transport.service_operation(self.service, operation, data):
            result = self.service.call(operation, data)
            if result.get("ok") and result.get("outcome") == "confirmed":
                if operation == "task_progress":
                    publication = result["data"]
                    if publication.get("state") == "confirmed" and publication.get("comment_id"):
                        self._confirm_comment(publication["comment_id"], data["task_id"])
                else:
                    task = self.service.core.get_task(self.service.handle, result["data"]["id"])
                    if task.issue_id:
                        self._confirm_issue(task.issue_id)
            return result

    def _confirm_issue(self, candidate):
        key = "product:issue:" + uuid(candidate)
        row = self.ledger.get(key)
        if row["state"] == "confirmed":
            return self.ledger.resource(candidate)
        # This only confirms fixture resource ownership. Core/provider status
        # remains governed by the existing outbound/reconciliation services.
        self.admin.read("issue", candidate)
        self.ledger.confirm(key, candidate, "issue")
        return self.ledger.resource(candidate)

    def _confirm_comment(self, candidate, task_id):
        key = "product:comment:" + uuid(candidate)
        row = self.ledger.get(key)
        if row["state"] == "confirmed":
            return self.ledger.resource(candidate)
        task = self.service.core.get_task(self.service.handle, task_id)
        observed = self.client.read_comment(candidate)
        if (
            observed.id != candidate
            or observed.author_id != self.ledger.scope.owner
            or observed.issue.id != task.issue_id
            or observed.issue.title != task.title
            or observed.issue.body != task.body
        ):
            raise DriverRefused("fixture_comment_readback_mismatch")
        self.ledger.confirm(key, candidate, "comment")
        return self.ledger.resource(candidate)

    def recover_issue(self, operation_id):
        operation_id = uuid(operation_id)
        key = "product:issue:" + operation_id
        row = self.ledger.get(key)
        if row["state"] == "confirmed":
            for task in self.service.core.list_tasks(self.service.handle):
                if task.issue_id == operation_id:
                    return task
            raise DriverRefused("recovered_issue_not_owned_by_current_session")
        self.admin.graphql.refresh_if_needed(recovery_key=key)
        self.admin.read("issue", operation_id, recovery_key=key)
        journal = self.service.provider.journal
        with self.transport.possible_effect_readback(key):
            task = Reconciliation(self.service.core, self.client, journal).recover_outbound(
                self.service.handle, operation_id
            )
        if task.issue_id != operation_id:
            raise DriverRefused("product_recovery_identity_changed")
        self.ledger.confirm(key, operation_id, "issue")
        return task

    def recover_progress(self, task_id, publication_id):
        task = self.service.core.get_task(self.service.handle, task_id)
        view = self.service.writer.approvals_for_progress(
            self.service.scope, task.id, task.prose_digest
        )
        publisher = self.service.publisher(self.service.scope, task, view)
        row = publisher.get(self.service.handle, publication_id)
        candidate = row["comment_id"] or row["candidate_id"]
        key = "product:comment:" + uuid(candidate)
        if self.ledger.get(key)["state"] == "confirmed":
            return row
        with self.transport.possible_effect_readback(key):
            recovered = publisher.recover(self.service.handle, publication_id)
            self._confirm_comment(candidate, task_id)
        return recovered

    def receiver(self, journal, binding, webhook_id, secret, inbound, *, clock, **options):
        # This local-only reservation deliberately precedes a provider hook:
        # the owned receiver must exist to obtain and verify its HTTPS route.
        self.admin.graphql.actions.receiver_candidate(webhook_id)
        return PrimaryRun(
            self.service.core,
            self.service.handle,
            journal,
            binding,
            webhook_id,
            secret,
            inbound,
            self.ledger,
            clock=clock,
            **options,
        )

    def reserve_receiver(self, webhook_id):
        return self.admin.graphql.actions.reserve_receiver(webhook_id)
