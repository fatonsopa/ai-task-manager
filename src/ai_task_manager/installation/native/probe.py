"""Actual canary host/session execution, with no task work or Core delegate."""

import hashlib
from dataclasses import asdict, dataclass
from uuid import uuid4

from ai_task_manager.adapters.hosts.bridge.conformance import CanaryGateway, ProbeTurn, cases
from ai_task_manager.adapters.hosts.bridge.controller import ManagedNativeHost
from ai_task_manager.adapters.hosts.bridge.records import HostBinding
from ai_task_manager.adapters.process.groups import GroupStop
from ai_task_manager.domains.work.errors import AccessDenied

from .probe_authority import NativeProbeAuthority
from .probe_permits import ProbePermits
from .resolver import policy_builder


@dataclass(frozen=True)
class ProbePair:
    attempt_id: str
    worktree: object
    worktree_id: str
    database_id: str = "probe:no-database"


class NativeProbeFactory:
    def __init__(self, authority, journal):
        if type(authority) is not NativeProbeAuthority:
            raise AccessDenied("An enrolled native canary authority is required")
        self.authority, self.journal = authority, journal
        self._original = (authority, journal)
        self.native = self._native = None
        self.gateway = None
        self._stop = None
        self._stop_entered = False

    def _current(self, context, *, native=None, phase="observe"):
        if (self.authority, self.journal) != self._original or self.native is not self._native:
            raise AccessDenied("Native probe factory owner changed")
        if native is not None and (
            native is not self._native
            or native.core is not None
            or native.resources is not None
            or native.commands is not None
        ):
            raise AccessDenied("Probe cannot acquire normal task or command authority")
        self.authority.require_current(
            context,
            policy=native.policy if native else None,
            binding=native.binding if native else None,
            phase=phase,
        )

    def start(self, context, gateway):
        self._current(context, phase="start")
        if (
            self.native is not None
            or type(gateway) is not CanaryGateway
            or (
                gateway.context is not context
                or gateway.authority is not self.authority
                or gateway.journal is not self.journal
                or gateway.native is not None
            )
        ):
            raise AccessDenied("An exact unused canary gateway and native owner are required")
        if not self.journal.intend(
            "native-probe-start:" + context.reference,
            {"context": self.authority.permission["context_digest"]},
        ):
            raise AccessDenied("Existing probe startup must be reconciled, never repeated")
        attempt_id = "probe:" + context.reference
        pair = ProbePair(attempt_id, context.fixture_root, "probe:" + str(context.fixture_identity))
        policy = policy_builder(self.authority.manifest)(context.host, pair)
        binding = HostBinding(
            context.scope,
            "probe:" + context.reference,
            attempt_id,
            "probe-worker:" + str(uuid4()),
            context.host,
            self.authority.reference,
            policy.configuration_digest,
            context.deadline,
        )
        permits = ProbePermits(self.authority, policy, binding, gateway, self.journal)
        native = ManagedNativeHost(
            binding,
            pair,
            policy,
            context.fixture_root / ("native-" + str(uuid4())),
            self.journal,
            None,
            None,
            permits,
            context.account_hash,
            commands=None,
        )
        self.native = self._native = native
        self.gateway = gateway
        try:
            native.initialize()
            if native.gateway.delegate is not None or native.startup.admitted:
                raise AccessDenied("Probe initialization acquired unexpected task authority")
            native.verify_active()
            native.start_identity_observations()
            return native
        except BaseException as error:
            if native.process is not None:
                try:
                    self.stop(context, native)
                except BaseException as stop_error:
                    error.probe_stop_error = type(stop_error).__name__
            raise

    def execute(self, context, native, case):
        self._current(context, native=native, phase="canary")
        expected = {c.case_id: c for c in cases(context)}
        if (
            case.case_id not in context.case_ids
            or expected.get(case.case_id) != case
            or self.gateway.native is not native
            or self.gateway.case != case
            or native.gateway.delegate is not self.gateway
            or native.startup.admitted
        ):
            raise AccessDenied("Only the exact active canary case may be submitted")
        if not native._turn_lock.acquire(blocking=False):
            raise AccessDenied("A native canary turn is already active")
        try:
            if not self.journal.intend(
                "native-probe-case:" + context.reference + ":" + case.case_id,
                {"prompt": hashlib.sha256(case.prompt.encode()).hexdigest()},
            ):
                raise AccessDenied("Existing probe submission cannot be repeated")
            identity = native.verify_active()
            permit = native.permits._claim("assignment", "case:" + case.case_id)
            request = self.journal._append(
                "trace",
                {
                    "effect": "native_probe_submission",
                    "case_id": case.case_id,
                    "prompt_sha256": hashlib.sha256(case.prompt.encode()).hexdigest(),
                    "session_id": native.startup.native_session,
                    "invocation_ref": permit.reference,
                    "binding": asdict(native.binding),
                    "process": asdict(identity),
                },
            )
            recorded = False
            try:
                guard = native.startup.model_guard.prepare_effect()
                with native.process.use_invocation(permit, guard):
                    if context.host == "claude":
                        native.portal.call(native.session.turn, case.prompt)
                    else:
                        native.session.turn(case.prompt)
                terminal = native.startup.transcript._last
                p = self.journal._read(terminal, "trace")["payload"]
                frame = p.get("frame", {})
                if p.get("process") != asdict(identity) or any(
                    p.get(k) != getattr(native.binding, k)
                    for k in ["attempt_id", "worker_id", "configuration_digest"]
                ):
                    raise AccessDenied("Canary terminal belongs to another native owner")
                if context.host == "claude":
                    valid = (
                        frame.get("type") == "result"
                        and frame.get("subtype") == "success"
                        and frame.get("is_error") is False
                        and frame.get("session_id") == native.startup.native_session
                    )
                else:
                    valid = (
                        frame.get("method") == "turn/completed"
                        and frame.get("params", {}).get("threadId") == native.startup.native_session
                        and frame.get("params", {}).get("turn", {}).get("status") == "completed"
                    )
                if not valid:
                    raise AccessDenied("A matching successful actual canary terminal is required")
                native.permits._record(
                    permit, "succeeded", True, effect="native_probe_turn", observation_ref=terminal
                )
                recorded = True
                native.invocation_completed(permit)
                return ProbeTurn(request, terminal, permit.reference)
            except BaseException:
                if recorded:
                    self.journal._append(
                        "trace",
                        {
                            "effect": "native_probe_completion_reset_failed",
                            "invocation_ref": permit.reference,
                        },
                    )
                    raise
                native.permits._record(permit, "unknown", False, effect="native_probe_turn_unknown")
                raise
        finally:
            native._turn_lock.release()

    def stop(self, context, native):
        # Cleanup uses the original handles even after permission expiry/revocation.
        self._current(context, native=native, phase="stop")
        if self._stop is not None:
            if native.process is None or not native.process._closed:
                raise AccessDenied("Previous stop does not describe this current process")
            if self._stop.confirmed:
                native.verify_stop_owner()
                return self._stop
        if self._stop_entered:
            # Only retained exact physical stop proof can reconcile a helper
            # that was still alive during the first stop; no transport replay.
            result = native.reconcile_stop()
        else:
            self._stop_entered = True
            result = native.stop()
        if not isinstance(result, GroupStop):
            raise AccessDenied("Actual owned process-group stop observation is unavailable")
        self._stop = result
        return result
