"""Trusted normal probe orchestration; no evaluation grants or semantic approval."""

from dataclasses import asdict

from ai_task_manager.adapters.process.groups import GroupStop
from ai_task_manager.config.files import digest
from ai_task_manager.observability.failures import FailureStage, capture_failure
from ai_task_manager.supervisor.models import Command

from ..admission import launch_template
from ..controller import ManagedNativeHost
from ..identity_refresh import IdentityRefresh
from .canary import CanaryGateway
from .completion import CompletedCapture
from .models import CASE_IDS, ProbeTurn, cases
from .profile import StableProfile, prepare_profile
from .verification import require, startup, terminal_identity, verify_capture, verify_case


def check_command(policy, command, root):
    """Compare the actual shared Command contract with the pinned SDK builder."""
    launched = launch_template(policy)
    require(
        isinstance(command, Command)
        and list(command.argv) == launched["argv"]
        and command.cwd == root
        and dict(command.environment) == dict(policy.environment),
        "Actual owned command differs from the observed launch template",
    )
    return digest(launched)


class ConformanceCapture:
    def __init__(self, manifest, pins, context, authority, journal, *, profile):
        self.manifest, self.pins, self.context = manifest, pins, context
        self.authority, self.journal = authority, journal
        require(isinstance(profile, StableProfile), "An explicit stable profile is required")
        self.profile = profile
        self._bound = (manifest, pins, context, authority, journal, profile)
        self.used = False
        self.report_ref = None
        self._native_bound = None
        self._identity = None
        self._identity_ref = None
        self._launch = None
        self._completion_token = object()
        self._completion = None
        self._complete_hash = None
        self._native_owner = None

    def check(self, native=None):
        require(
            all(
                a is b
                for a, b in zip(
                    self._bound,
                    (
                        self.manifest,
                        self.pins,
                        self.context,
                        self.authority,
                        self.journal,
                        self.profile,
                    ),
                    strict=True,
                )
            ),
            "The original conformance capture owner changed",
        )
        self.manifest.check()
        self.pins.check()
        c, d = self.context, self.manifest.data
        require(
            c.profile_digest == self.profile.fingerprint
            and self.profile
            == prepare_profile(
                self.manifest,
                self.pins,
                self.profile.cli_version,
                policy=native.policy if native else None,
            )
            and c.installation_digest == self.manifest.digest
            and (c.host, c.model, c.effort, c.account_hash)
            == (d["host"], d["model"], d["effort"], d["account_hash"])
            and self.journal.clock() < c.deadline,
            "Probe profile or original deadline changed",
        )
        self.authority.require_current(
            c,
            policy=native.policy if native else None,
            binding=native.binding if native else None,
            phase="observe",
        )
        if native is not None:
            require(
                isinstance(native, ManagedNativeHost)
                and native.journal is self.journal
                and native.core is None
                and native.resources is None
                and native.binding.scope == c.scope
                and native.binding.deadline <= c.deadline
                and native.policy.configuration_digest == native.binding.configuration_digest
                and native.policy.cwd == c.fixture_root
                and native.expected_account == c.account_hash
                and (native.policy.host, native.policy.model, native.policy.effort)
                == (c.host, c.model, c.effort),
                "The actual probe host or scope differs",
            )
            identity = native.verify_active()
            helper = native.identity
            require(helper is native._identity_bound, "The native identity source changed")
            if helper is None:
                self._identity_ref = self.journal._read(native.startup.ready_ref, "startup")["ref"]
            else:
                require(
                    isinstance(helper, IdentityRefresh) and helper.observations.native is native,
                    "The exact trusted identity observer is required",
                )
                prepared = helper.observations.prepare()
                # This is a pre-Core observation, so serialize transient contention.
                # Core-held consumers retain their nonblocking current check.
                with native.startup.model_guard.observation_boundary(not_after=c.deadline):
                    prepared.check()
                    self.authority.require_current(
                        c, policy=native.policy, binding=native.binding, phase="observe"
                    )
                    self._identity_ref = prepared.observation.reference
            if self._native_bound is not None:
                require(
                    all(
                        a is b
                        for a, b in zip(
                            self._native_bound,
                            (
                                native,
                                native.process,
                                native.startup,
                                native.session,
                                native.policy,
                                native.binding,
                            ),
                            strict=True,
                        )
                    )
                    and identity == self._identity,
                    "The original probe process/session changed",
                )

    def run(self, factory):
        require(not self.used, "An original conformance probe cannot be replayed")
        self.used = True
        self.check()
        c = self.context
        require(c.case_ids == CASE_IDS, "The complete frozen profile matrix is required")
        gateway = CanaryGateway(c, self.authority, self.journal)
        native, owner, stop, error = None, None, None, None
        completed = []
        failure = None
        try:
            native = factory.start(c, gateway)
            self.check(native)
            require(native.gateway.delegate is None, "Probe startup cannot delegate Core work")
            self._native_owner = native
            self._native_bound = (
                native,
                native.process,
                native.startup,
                native.session,
                native.policy,
                native.binding,
            )
            self._identity = native.process.identity
            owner = {"binding": asdict(native.binding), "process": asdict(native.process.identity)}
            self._launch = check_command(native.policy, native.process.command, c.fixture_root)
            profile = vars(self.profile)
            session = startup(self.journal, native.startup.ready_ref, owner, profile)
            gateway.bind(native)
            native.gateway.delegate = gateway
            for case in cases(c):
                self.check(native)
                gateway.begin(case)
                before = gateway.snapshot()
                identity_before = self._identity_ref
                prior = {r["ref"] for r in self.journal._history("trace")}
                turn = factory.execute(c, native, case)
                require(isinstance(turn, ProbeTurn), "Actual accounted probe turn is missing")
                self.check(native)
                observations = [r for r in self.journal._history("trace") if r["ref"] not in prior]
                # The current check can refresh identity after the terminal.
                # Keep its current-authority check separate from effect provenance.
                identity_after = terminal_identity(
                    self.journal,
                    identity_before,
                    observations,
                    turn.terminal_ref,
                    owner,
                    session,
                    native.startup.ready_ref,
                    c.account_hash,
                )
                rows = [r for r in observations if r["payload"].get("frame_kind") == "native_frame"]
                require(len(rows) <= 256, "Native probe trace exceeds its frozen bound")
                value = {
                    **asdict(turn),
                    "frame_refs": [r["ref"] for r in rows],
                    "identity_before": identity_before,
                    "identity_after": identity_after,
                    "gateway_refs": list(gateway.end()),
                    "before": before,
                    "after": gateway.snapshot(),
                }
                verify_case(self.journal, case, value, owner, c.reference, session, c.model)
                completed.append({"case_id": case.case_id, **value})
        except BaseException as failed:
            error = failed
            failure = capture_failure(failed, FailureStage.MANAGED_EXECUTION)
        finally:
            if native is not None:
                try:
                    stop = factory.stop(c, native)
                except BaseException as failed_stop:
                    if error is None:
                        error = failed_stop
                        failure = capture_failure(failed_stop, FailureStage.MANAGED_STOP)
        confirmed = (
            isinstance(stop, GroupStop)
            and stop.confirmed
            and stop.observation.known
            and not stop.observation.live_members
            and isinstance(native, ManagedNativeHost)
            and native.process is not None
            and native.process._closed
        )
        payload = {
            "effect": "native_conformance_capture",
            "version": 2,
            "profile": asdict(self.profile),
            "profile_digest": self.profile.fingerprint,
            "launch_fingerprint": self._launch,
            "manifest_digest": self.manifest.digest,
            "source": self.pins.fingerprint,
            "context_ref": c.reference,
            "owner": owner,
            "startup_ref": native.startup.ready_ref if native and native.startup else None,
            "cases": completed,
            "stop": asdict(stop) if isinstance(stop, GroupStop) else None,
            "confirmed_stop": bool(confirmed),
            "error_type": type(error).__name__ if error else None,
            "failure": failure,
            "provenance": {
                "manifest_digest": self.manifest.digest,
                "probe_deadline": c.deadline,
                "context": {**asdict(c), "fixture_root": str(c.fixture_root)},
            },
        }
        self.report_ref = self.journal._append("trace", payload)
        if error is not None:
            raise error
        require(confirmed, "Unknown native probe stop retains ownership")
        record, _, _ = verify_capture(self.journal, self.report_ref)
        self._complete_hash = digest(record)
        self._completion = CompletedCapture(self, self._completion_token)
        return self.report_ref

    def completed(self):
        require(self._completion is not None, "No actual completed capture is available")
        self._completion.export()
        return self._completion

    def _export_completion(self, completed, token):
        require(
            completed is self._completion
            and token is self._completion_token
            and self._native_owner is not None
            and self._native_owner is self._native_bound[0]
            and self._native_owner.process is self._native_bound[1]
            and self._native_owner.process._closed,
            "Unissued or unresolved capture cannot enter durable enrollment",
        )
        self.pins.check()
        record, profile, context = verify_capture(self.journal, self.report_ref)
        require(
            digest(record) == self._complete_hash and profile == self.profile,
            "The original completed capture changed",
        )
        return {
            "version": 2,
            "reference": self.report_ref,
            "capture_hash": self._complete_hash,
            "profile": asdict(profile),
            "profile_digest": profile.fingerprint,
            "provenance": record["payload"]["provenance"],
        }
