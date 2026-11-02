"""One managed native host, with no work tools until Core admission completes."""

import json
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from threading import Lock

from anyio.from_thread import start_blocking_portal

from ai_task_manager.adapters.hosts.transports.claude import (
    ClaudeSession,
    ClaudeTransport,
    sdk_command,
    sdk_options,
)
from ai_task_manager.adapters.hosts.transports.codex import CodexSession
from ai_task_manager.adapters.hosts.transports.native_exit import require_exit_monitoring
from ai_task_manager.adapters.hosts.transports.process import HostProcess
from ai_task_manager.domains.work.errors import AccessDenied

from .auth_probe import ClaudeAuthProbe
from .codex_auth import CodexAuthProbe
from .codex_configuration import CodexConfigurationObserver
from .codex_home import AclAuditPort
from .gateway import SCHEMAS, ManagedToolGateway, tool_schema
from .invocations import InitializationInvocation, require_invocation
from .policy import NativeProfile, codex_command
from .records import ManagedProcesses
from .startup import HostStartupReceipts, StartupObserver
from .transcript import OwnedTranscript
from .workspace import Workspace


def _audit_context(host, pair, scope):
    return json.dumps(
        {
            "host": host,
            "attempt_id": pair.attempt_id,
            "scope": scope,
            "worktree_id": pair.worktree_id,
            "database_id": pair.database_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class WaitingGateway:
    schemas = {name: tool_schema(name) for name in SCHEMAS}

    def __init__(self):
        self.delegate = None

    def call(self, name, args):
        if self.delegate is None:
            raise AccessDenied("Native initialization has no assigned task work lease")
        return self.delegate.call(name, args)


class ManagedNativeHost:
    def __init__(
        self,
        binding,
        pair,
        policy,
        directory,
        journal,
        core,
        resources,
        permits,
        expected_account,
        commands=None,
        process_registry=None,
        source_auditor=None,
    ):
        self.binding, self.pair, self.policy = binding, pair, policy
        self.directory, self.journal = Path(directory), journal
        self.core, self.resources, self.permits = core, resources, permits
        self.expected_account, self.commands = expected_account, commands
        self.process_registry = (
            process_registry if process_registry is not None else ManagedProcesses()
        )
        self.gateway = WaitingGateway()
        self.process = self.session = self.startup = None
        self._portal_context = self.portal = None
        self._turn_lock = Lock()
        self._source_auditor = source_auditor
        if self.policy.codex_settings is not None:
            context = _audit_context(binding.host, pair, asdict(binding.scope))
            source = self.policy.codex_settings.sources
            if source_auditor is None:
                source_auditor = AclAuditPort(source, journal, context)
                self._source_auditor = source_auditor
            source_auditor.verify(source, journal, context)
            source.bind_acl_auditor(source_auditor)

    def _flush_source_observations(self):
        if self.policy.codex_settings is not None:
            context = _audit_context(self.binding.host, self.pair, asdict(self.binding.scope))
            self._source_auditor.verify(self.policy.codex_settings.sources, self.journal, context)
            self._source_auditor()

    @contextmanager
    def _source_audit_boundary(self, *, before=True):
        if before:
            self._flush_source_observations()
        try:
            yield
        except BaseException as original:
            try:
                self._flush_source_observations()
            except BaseException as audit_error:
                original.acl_audit_error = type(audit_error).__name__
                original.acl_observations = getattr(audit_error, "acl_observations", ())
                original.owned_stop_evidence = getattr(self.process, "stop_evidence", None)
            raise
        else:
            try:
                self._flush_source_observations()
            except BaseException as audit_error:
                audit_error.owned_stop_evidence = getattr(self.process, "stop_evidence", None)
                raise

    def initialize(self):
        with self._source_audit_boundary(before=True):
            self.policy.check()
            self._flush_source_observations()
            permit = self.permits.authorize_initialization(self.policy, self.binding)
            try:
                if not isinstance(permit, InitializationInvocation):
                    raise AccessDenied("Native initialization needs registered typed invocations")
                require_invocation(permit.native, self.policy, self.binding, "host")
                reference = self._initialize(permit)
            except BaseException as error:
                self.permits.record_initialization(
                    permit,
                    outcome="failed_or_uncertain",
                    observation_ref=self.startup.ready_ref if self.startup else None,
                    detail=type(error).__name__,
                )
                raise
            self.permits.record_initialization(
                permit, outcome="observed_native_startup", observation_ref=reference, detail=None
            )
            return reference

    def _initialize(self, invocation):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        options = None
        if self.binding.host == "claude":
            auth_ref = ClaudeAuthProbe(self.journal, self.permits).observe(
                self.policy, self.binding, self.directory
            )
            options = sdk_options(self.policy, self.gateway)
            command = sdk_command(
                self.policy, options, self.binding.deadline - self.journal.clock()
            )
        else:
            auth_ref = None
            codex_probe = CodexAuthProbe(self.journal, self.permits)
            codex_auth_ref = codex_probe.observe(self.policy, self.binding, self.directory)
            codex_probe.consume(self.policy, self.binding, codex_auth_ref)
            command = codex_command(self.policy, self.binding.deadline - self.journal.clock())

        def native_boundary():
            self.policy.check()
            if self.binding.host == "codex" and self.process.process is None:
                codex_probe.check_launch(self.policy, self.binding, codex_auth_ref)
            self._flush_source_observations()

        self.process = HostProcess(
            command,
            self.directory,
            deadline=self.binding.deadline,
            clock=self.journal.clock,
            boundary=native_boundary,
            invocation=invocation,
        )
        self.process_registry.attach(self.binding.attempt_id, self.process)
        transcript = OwnedTranscript(self.process, self.journal, self.binding)
        self.startup = StartupObserver(
            transcript,
            self.policy,
            expected_account=self.expected_account,
            claude_auth_ref=auth_ref,
            codex_auth_ref=codex_auth_ref if self.binding.host == "codex" else None,
            configuration=(
                CodexConfigurationObserver(
                    self.policy.codex_settings,
                    self.journal,
                    self.binding,
                    self.policy,
                    self.permits,
                )
                if self.binding.host == "codex"
                else None
            ),
        )
        if self.binding.host == "claude":
            transport = ClaudeTransport(self.process, transcript)
            self.session = ClaudeSession(options, transport, self.startup)
            self._portal_context = start_blocking_portal()
            self.portal = self._portal_context.__enter__()
            self.portal.call(self.session.initialize)
            if self.startup.ready_ref is None:
                # This fixed task-free turn is separately accounted before sending.
                # No task prose, resources or effects are available to the model.
                neutral = require_invocation(
                    self.permits.authorize_neutral_start(self.policy, self.binding),
                    self.policy,
                    self.binding,
                    "neutral",
                )
                try:
                    guard = self.startup.model_guard.prepare_effect(initialization=True)
                    with self.process.use_invocation(neutral, guard):
                        result = self.portal.call(
                            self.session.turn,
                            "Initialize this managed session. Do not use tools. Reply READY.",
                        )
                except BaseException:
                    self.permits.record_neutral_start(
                        neutral,
                        outcome="failed_or_uncertain",
                        result=None,
                        observation_ref=transcript._last,
                    )
                    raise
                self.permits.record_neutral_start(
                    neutral,
                    outcome="observed_turn",
                    result=result,
                    observation_ref=transcript._last,
                )
                self.process.complete_invocation(neutral)
        else:
            self.session = CodexSession(self.process, transcript, self.startup, self.gateway)
            self.session.initialize(self.policy.cwd)
        if self.startup.initialization_tool_calls:
            raise AccessDenied("Native initialization attempted tools before task admission")
        if not self.startup.ready_ref:
            raise AccessDenied("Native host did not emit authenticated session startup")
        return self.startup.ready_ref

    def admit(self, handle, dispatch, attempt):
        gateway = ManagedToolGateway(
            self.core,
            handle,
            dispatch,
            Workspace(self.pair.worktree),
            self.resources,
            self.journal,
            deadline=self.binding.deadline,
            commands=self.commands,
            process_registry=self.process_registry,
            model_guard=self.startup.model_guard,
            invocation_source=self.current_invocation,
        )
        gateway.admit()
        self.gateway.delegate = gateway
        self.startup.admitted = True
        self.process.boundary = lambda: self._current_lease(handle, attempt)

    def _current_lease(self, handle, attempt):
        with self._source_audit_boundary(before=False):
            if self.journal.clock() >= self.binding.deadline:
                raise AccessDenied("Original managed deadline expired")
            self.policy.check()
            self.startup.assert_stable()
            self._flush_source_observations()
            return self.core.execution.validate_work_lease(handle, attempt.id, attempt.version)

    def verify_active(self):
        with self._source_audit_boundary(before=True):
            self.policy.check()
            if self.startup is None:
                raise AccessDenied("Native startup is not ready")
            self.startup.assert_stable()
            if self.process is None:
                raise AccessDenied("Native host process was not created")
            return self.process.verify_identity()

    def current_invocation(self):
        permit = self.process._active_invocation
        if permit is None or not self.process._active_started:
            raise AccessDenied("Managed work requires an active accounted model invocation")
        return permit.running()

    @contextmanager
    def effect_boundary(self):
        permit = self.current_invocation()
        guard = self.startup.model_guard.prepare_effect()
        with permit.effect_boundary():
            with self.startup.model_guard.current_boundary(guard):
                yield

    def execute_model_turn(self, text, *, invocation=None):
        with self._source_audit_boundary(before=True):
            if not self._turn_lock.acquire(blocking=False):
                raise AccessDenied("An owned native model turn is already active")
            try:
                if self.gateway.delegate is None:
                    raise AccessDenied("Assigned model work requires a current admitted lease")
                self.gateway.delegate.check()
                self._flush_source_observations()
                permit = require_invocation(invocation, self.policy, self.binding, "assignment")
                guard = self.startup.model_guard.prepare_effect()
                with self.process.use_invocation(
                    permit, guard, self.gateway.delegate.lease_boundary
                ):
                    if self.binding.host == "claude":
                        return self.portal.call(self.session.turn, text)
                    return self.session.turn(text)
            finally:
                self._turn_lock.release()

    def invocation_completed(self, permit):
        # Trusted accounting must have recorded completion before the watchdog
        # accepts this reset to the immutable host deadline.
        self.process.complete_invocation(permit)

    def stop(self):
        with self._source_audit_boundary(before=False):
            if self.process is None:
                raise AccessDenied("Native startup outcome is unresolved")
            errors = []
            try:
                if self.portal is not None:
                    self.portal.call(self.session.close)
                elif self.session is not None:
                    self.session.close()
            except BaseException as error:
                errors.append(("session_close", type(error).__name__))
            finally:
                try:
                    if self._portal_context is not None:
                        self._portal_context.__exit__(None, None, None)
                except BaseException as error:
                    errors.append(("portal_close", type(error).__name__))
                finally:
                    self._portal_context = self.portal = None
            try:
                stopped = self.process.stop()
            finally:
                for stage, error in errors:
                    self.journal._append(
                        "trace",
                        {
                            "effect": "native_disconnect_error",
                            "attempt_id": self.binding.attempt_id,
                            "stage": stage,
                            "error": error,
                        },
                    )
            self.permits.record_host_stop(self.binding, stopped)
            return stopped


class ManagedHostFactory:
    def __init__(
        self,
        core,
        resources,
        journal,
        root,
        policy_builder,
        permits,
        accounts,
        process_registry=None,
        model_profiles=None,
    ):
        self.core, self.resources, self.journal = core, resources, journal
        self.root = Path(root)
        self.policies, self.permits, self.accounts = policy_builder, permits, accounts
        self.owned = {}
        self.commands = None
        self.model_profiles = dict(model_profiles or {})
        if any(not isinstance(profile, NativeProfile) for profile in self.model_profiles.values()):
            raise AccessDenied("Native model profiles must be typed trusted configuration")
        self.process_registry = (
            process_registry if process_registry is not None else ManagedProcesses()
        )
        self.startup_verifier = HostStartupReceipts(
            journal, self.process_registry, hosts=self.owned
        )

    def check_admission(self, host):
        # Trusted installer/evaluator port, not a public caller flag. It must
        # positively verify tested native restrictions and current launch authority.
        require_exit_monitoring()
        if host == "claude" and not isinstance(self.model_profiles.get(host), NativeProfile):
            raise AccessDenied("A trusted explicit Claude model profile is required")
        self.permits.check_admission(host)

    def _audit_policy(self, policy, host, pair):
        if policy.codex_settings is None:
            return None
        owner = self.resources.journal.attempt(pair.attempt_id)
        if owner["host"] != host or owner["attempt_id"] != pair.attempt_id:
            raise AccessDenied("Source audit owner differs from the resource assignment")
        context = _audit_context(host, pair, owner["scope"])
        source = policy.codex_settings.sources
        port = source._acl_auditor
        if port is None:
            port = AclAuditPort(source, self.journal, context)
        if not isinstance(port, AclAuditPort):
            raise AccessDenied("Source audit port was not installed by trusted composition")
        port.verify(source, self.journal, context)
        source.bind_acl_auditor(port)
        port()
        return port

    def configuration_digest(self, host, pair):
        policy = self.policies(host, pair)
        port = self._audit_policy(policy, host, pair)
        policy.check()
        if port is not None:
            port()
        if host in self.model_profiles and (policy.model, policy.effort) != (
            self.model_profiles[host].model,
            self.model_profiles[host].effort,
        ):
            raise AccessDenied("The native policy differs from its trusted model profile")
        return policy.configuration_digest

    def launch(self, binding, pair):
        if binding.attempt_id in self.owned:
            raise AccessDenied("Existing native host ownership must be reconciled")
        policy = self.policies(binding.host, pair)
        source_auditor = self._audit_policy(policy, binding.host, pair)
        policy.check()
        if source_auditor is not None:
            source_auditor()
        if binding.host in self.model_profiles and (policy.model, policy.effort) != (
            self.model_profiles[binding.host].model,
            self.model_profiles[binding.host].effort,
        ):
            raise AccessDenied("The native model profile changed before launch")
        if policy.configuration_digest != binding.configuration_digest:
            raise AccessDenied("Native policy changed between reservation and launch")
        native = ManagedNativeHost(
            binding,
            pair,
            policy,
            self.root / binding.attempt_id,
            self.journal,
            self.core,
            self.resources,
            self.permits,
            self.accounts[binding.host],
            commands=self.commands,
            process_registry=self.process_registry,
            source_auditor=source_auditor,
        )
        # Publish ownership before any subprocess or SDK action, including failed initialization.
        self.owned[binding.attempt_id] = native
        return native

    def reconcile_stopped(self, attempt_id):
        native = self.owned.get(attempt_id)
        if native is None:
            return False
        try:
            return native.stop().confirmed
        except BaseException:
            return False
