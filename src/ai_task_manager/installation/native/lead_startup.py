"""Finite installed lead reporting through the existing owned native transport.

The installer seals one event and lead role. Native permission, actual host
startup, provider enrollment and independent operator input remain separate.
"""

import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from threading import get_ident

from ai_task_manager.adapters.hosts.bridge.accounting import HostInvocationAccounting
from ai_task_manager.adapters.hosts.bridge.controller import ManagedNativeHost
from ai_task_manager.adapters.hosts.bridge.human import OperatorChannel
from ai_task_manager.adapters.hosts.bridge.invocations import InitializationInvocation
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.hosts.bridge.records import HostBinding
from ai_task_manager.config.files import digest, read_private
from ai_task_manager.config.service import load_service
from ai_task_manager.domains.work.errors import AccessDenied

from .lead import connect_lead
from .manifest import NativeManifest
from .normal import NormalAuthoritySource, lead_operator
from .resolver import policy_builder


@dataclass(frozen=True)
class LeadDirectory:
    worktree: Path
    attempt_id: str
    worktree_id: str
    database_id: str = "lead:no-worker-database"


class LeadAccounting(HostInvocationAccounting):
    """Existing finite typed permits, with a distinct sealed lead owner."""

    def __init__(self, permission, service, journal, manifest, binding):
        self.manifest, self.service, self.lead_binding = manifest, service, binding
        self.permission = permission
        self._lead_owner = (manifest, service, binding, permission)
        super().__init__(
            permission.controller,
            service.core,
            service.handle,
            None,
            journal,
            permission.plan,
            admissions=permission.admissions,
        )

    def _owner(self, policy, binding):
        if any(
            a is not b
            for a, b in zip(
                (self.manifest, self.service, self.lead_binding, self.permission), self._lead_owner
            )
        ):
            raise AccessDenied("Lead accounting composition changed")
        self.check_admission(binding.host)
        self.manifest.check()
        current = self.admissions.store.permission(self.permission.reference)
        installation = read_private(self.manifest.installation.path)
        if (
            current["kind"] != "normal-lead-v1"
            or installation.get("role") != "lead"
            or binding != self.lead_binding
            or self.core.session_binding(self.handle) != (binding.core_session, binding.scope)
            or asdict(binding.scope) != installation["scope"]
            or binding.core_session != installation["lead"]["session_id"]
            or binding.deadline != self.plan.deadline
        ):
            raise AccessDenied("Current lead role, session or permission changed")
        with self.core.context.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM attempts WHERE id=?", (binding.attempt_id,)).fetchone():
                raise AccessDenied("Worker attempts cannot own lead invocation permits")
        grant = self.service.decisions.observations.verify_enrollment(self.service.grant_ref)
        if grant.scope != binding.scope or grant.expires_at <= self.journal.clock():
            raise AccessDenied("Current provider enrollment cannot authorize this lead")
        self.admissions.verify_policy(policy, binding, self.plan, self.control)
        self._owner_binding = binding

    def authorize_initialization(self, policy, binding):
        if not self._initialization_lock.acquire(blocking=False):
            raise AccessDenied("Lead initialization is already in progress")
        try:
            if self._initialization_attempted:
                raise AccessDenied("Lead initialization was already attempted")
            self._owner(policy, binding)
            self._policy = policy
            self._initialization_attempted = True
            token = self._initialization_token = object()
            self._initialization_thread = get_ident()
            try:
                host = self._claim(policy, binding, "host", self.plan.host_seconds, worker=True)
                relay = self._claim(
                    policy,
                    binding,
                    "relay",
                    host.binding.deadline - self.control.clock().timestamp(),
                    parent=host.binding.call_id,
                )
                self._initialization = InitializationInvocation(host, relay)
                return self._initialization
            except BaseException as error:
                try:
                    self._abort_unreturned_initialization(binding, token, type(error).__name__)
                except BaseException as abort_error:
                    error.initialization_abort_error = type(abort_error).__name__
                raise
            finally:
                self._initialization_token = self._initialization_thread = None
        finally:
            self._initialization_lock.release()

    def authorize(self, reference, attempt):
        raise AccessDenied("Lead permission cannot authorize a worker assignment")

    def event_turn(self, event_id):
        if event_id != read_private(self.manifest.installation.path)["lead"]["event_id"]:
            raise AccessDenied("Event is outside the finite lead permission")
        return self._claim(
            self._policy,
            self.lead_binding,
            "assignment",
            self.plan.assignment_seconds,
            reference="assignment:lead-event",
            parent=self._calls["host"],
        )


def projected_event(call, event_id, task_id):
    """Consume the existing public projection; retained audit payloads are not input."""
    result = call("event_list", {})
    if not result.get("ok"):
        raise AccessDenied("Current scoped projected event is unavailable")
    event = next((item for item in result["data"] if item["id"] == event_id), None)
    if (
        event is None
        or event.get("task_id") != task_id
        or not isinstance(event.get("payload"), dict)
        or event["payload"].get("quarantined")
        or event["payload"].get("ownership_reconciliation_required")
    ):
        raise AccessDenied("Current scoped projected event is absent or quarantined")
    return event


class LeadDelegate:
    """Keep the captured tool protocol; never grant its worker file/command tools."""

    def __init__(self, native, connection, permits):
        self.native, self.connection, self.permits = native, connection, permits

    def check(self):
        self.permits._owner(self.native.policy, self.native.binding)
        self.native.verify_active()
        self.native.startup.model_guard.prepare_effect()

    @contextmanager
    def lease_boundary(self):
        self.check()
        yield
        self.check()

    def call(self, name, arguments):
        self.check()
        if name != "acknowledge_event":
            raise AccessDenied("Lead reporting has no worker file or command authority")
        return self.connection.gateway.call(name, arguments)


def report(config_file, installation_file):
    """Supported CLI owner: one sealed event, one native lifecycle, no automatic retry."""
    spec = read_private(installation_file)
    if spec.get("role") != "lead":
        raise AccessDenied("An explicitly enrolled lead installation is required")
    lead = spec.get("lead")
    fields = {"session_id", "event_id", "journal", "key", "directory", "cwd"}
    if (
        type(lead) is not dict
        or not fields <= set(lead)
        or set(lead) - fields - {"operator_mode"}
        or any(type(value) is not str or not value for value in lead.values())
    ):
        raise AccessDenied("Exact finite lead startup fields are required")
    for name in ("journal", "key", "directory", "cwd"):
        if not Path(lead[name]).is_absolute():
            raise AccessDenied("Lead paths must be explicit absolute paths")
    manifest = NativeManifest(spec["native_manifest"], config_file, installation_file)
    source = NormalAuthoritySource(manifest, role="lead")
    permission = source.resolve(manifest.data["authorization_ref"], manifest)
    permission.check(manifest, spec["task_id"], spec["deadline"])
    service = load_service(config_file)
    if asdict(service.scope) != spec["scope"] or service.session_id != lead["session_id"]:
        raise AccessDenied("Lead installation belongs to another provider session")
    event = projected_event(service.call, lead["event_id"], spec["task_id"])
    journal = HostJournal(lead["journal"], lead["key"])
    owner = "lead:" + digest({"manifest": manifest.digest, "session": service.session_id})
    cwd = Path(lead["cwd"])
    if cwd.is_symlink() or cwd.resolve() != cwd or not cwd.is_dir():
        raise AccessDenied("Lead working directory must be its actual physical directory")
    pair = LeadDirectory(cwd, owner, "lead-directory:" + str(cwd.stat().st_ino))
    policy = policy_builder(manifest)(manifest.data["host"], pair)
    binding = HostBinding(
        service.scope,
        service.session_id,
        owner,
        owner,
        policy.host,
        service.grant_ref,
        policy.configuration_digest,
        permission.plan.deadline,
    )
    permits = LeadAccounting(permission, service, journal, manifest, binding)
    # Reserve durable ownership before any native process. Restart never resends.
    if not journal.intend(
        "normal-lead:" + owner, {"binding": asdict(binding), "event": event["id"]}
    ):
        raise AccessDenied("Existing lead startup requires reconciliation, not another launch")
    native = ManagedNativeHost(
        binding,
        pair,
        policy,
        lead["directory"],
        journal,
        service.core,
        None,
        permits,
        manifest.data["account_hash"],
    )
    connection = None
    terminal = None
    try:
        startup_ref = native.initialize()
        native.verify_active()
        permits.require_native_conformance(policy, binding)
        grant = service.decisions.observations.verify_enrollment(service.grant_ref)
        if lead_operator(spec) == "delegated":
            from .delegated_lead import capture

            enrollment = capture(manifest, permits, native, service, event)
        else:
            enrollment = OperatorChannel(journal).capture_enrollment(
                scope=service.scope,
                actions=grant.actions,
                startup_ref=startup_ref,
                timeout=min(60, binding.deadline - journal.clock()),
            )
        terminal = os.open("/dev/tty", os.O_WRONLY | os.O_NOCTTY)
        connection = connect_lead(
            config_file, native.startup.transcript, native.startup, enrollment, terminal
        )
        event = projected_event(connection.gateway.call, lead["event_id"], spec["task_id"])
        delegate = LeadDelegate(native, connection, permits)
        native.gateway.delegate = delegate
        native.startup.admitted = True
        accepted = connection.notifications.prepare(
            event["id"],
            "Acknowledge this exact event with acknowledge_event and its challenge, "
            "then report the event ID and outcome to the user. This is notification "
            "delivery, not human approval of task work.\n"
            + json.dumps(event["payload"], sort_keys=True, ensure_ascii=False),
        )
        text = connection.notifications.context(event["id"])
        permit = permits.event_turn(event["id"])
        try:
            projected_event(connection.gateway.call, event["id"], spec["task_id"])
            guard = native.startup.model_guard.prepare_effect()
            with native.process.use_invocation(permit, guard, delegate.lease_boundary):
                turn = partial(native.session.turn, text, input_id=accepted.input_id)
                if native.portal is None:
                    turn()
                else:
                    native.portal.call(turn)
            delegate.check()
            permits.record(permit, outcome="observed_turn", detail=None)
            native.process.complete_invocation(permit)
        except BaseException:
            if journal.intent("accounting-outcome:" + permit.reference) is None:
                permits.record(permit, outcome="failed_or_uncertain", detail="Lead turn incomplete")
            raise
        delegate.check()
        projected_event(connection.gateway.call, event["id"], spec["task_id"])
        result = connection.gateway.publish(event["id"])
        return {"ok": True, "data": {"event_id": event["id"], "reported": result}}
    finally:
        try:
            if connection is not None:
                connection.close()
        finally:
            try:
                if terminal is not None:
                    os.close(terminal)
            finally:
                if native.process is not None:
                    stop = native.stop()
                    if not stop.confirmed:
                        raise AccessDenied("Owned lead process stop remains unconfirmed")
