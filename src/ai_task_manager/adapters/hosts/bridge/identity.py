"""Fresh same-native Claude account observations, separate from execution grants.

Only a trusted installer may compose this port with unique accounted auth
permits. It changes neither the original startup nor default continuity rules.
"""

import math
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock

from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied

from .auth_probe import ClaudeAuthProbe
from .invocations import InitializationInvocation, require_invocation


class _IdentityPermits:
    def __init__(self, native, source, current_limit):
        self.native, self.source = native, source
        self._native = native
        self._source = source
        self._limit = current_limit
        self._used = set()

    def authorize_auth(self, policy, binding):
        if self.source is not self._source or self.native is not self._native:
            raise AccessDenied("The identity invocation issuer changed")
        permit = require_invocation(
            self.source.authorize_auth(policy, binding), policy, binding, "auth"
        )
        parent = self.native.process.invocation.native
        calls = permit.issuer.controller.snapshot()["calls"]
        call = next((c for c in calls if c["call_id"] == permit.reference), None)
        if (
            permit.issuer.controller is not parent.issuer.controller
            or call is None
            or call["metadata"].get("parent_call_id") != parent.reference
            or permit.binding.deadline > self._limit()
            or permit.reference in self._used
        ):
            raise AccessDenied("Identity needs a unique invocation under its original native owner")
        self._used.add(permit.reference)
        return permit

    def record_auth(self, binding, completion):
        if self.source is not self._source or self.native is not self._native:
            raise AccessDenied("The identity invocation issuer changed")
        self.source.record_auth(binding, completion)


@dataclass(frozen=True)
class IdentityObservation:
    reference: str
    observed_at: float
    expires_at: float
    original_startup_ref: str
    original_identity_not_after: float
    execution_not_after: float


@dataclass(frozen=True)
class PreparedIdentity:
    owner: object
    observation: IdentityObservation
    policy_metadata: object

    def check(self):
        return self.owner._check_local(self.observation, self.policy_metadata)


class NativeIdentityObservations:
    def __init__(self, native, permits, *, not_after):
        from .controller import ManagedNativeHost

        if not isinstance(native, ManagedNativeHost) or native.binding.host != "claude":
            raise AccessDenied("Fresh identity needs its actual owned Claude host")
        if type(not_after) not in {int, float} or not math.isfinite(not_after):
            raise AccessDenied("A finite original identity execution cap is required")
        native.verify_active()
        self.native, self.journal = native, native.journal
        self._owner = self._owner_fields()
        self._identity = native.process.identity
        self._session = native.startup.native_session
        self._account = native.expected_account
        self._original_ref = native.startup.ready_ref
        original = self.journal._read(self._original_ref, "startup")
        data = original["payload"]
        if (
            any(data.get(k) != value for k, value in asdict(native.binding).items())
            or (data.get("process_id"), data.get("process_start_identity"))
            != (self._identity.pid, self._identity.start_identity)
            or data.get("session_id") != self._session
            or data.get("model") != native.policy.model
            or data.get("requested_effort") != native.policy.effort
        ):
            raise AccessDenied("Original identity belongs to another native owner or profile")
        self._original_digest = digest(original)
        self.original_identity_not_after = original["expires_at"]
        invocation = native.process.invocation
        if not isinstance(invocation, InitializationInvocation):
            raise AccessDenied("Fresh identity needs its original accounted native owner")
        self._caps = (
            not_after, native.binding.deadline, native.process.deadline,
            invocation.native.binding.deadline, invocation.relay.binding.deadline,
        )
        self._invocation = invocation
        self._original_caps = self._caps
        self.execution_not_after = min(self._caps)
        self._initial_execution_cap = self.execution_not_after
        self._initial_identity_cap = self.original_identity_not_after
        issuer = _IdentityPermits(
            native, permits, lambda: min(self._current.expires_at, self.execution_not_after)
        )
        self._probe = ClaudeAuthProbe(self.journal, issuer)
        self._probe_binding = (self._probe, issuer)
        self._revoked = False
        self._refresh_lock = Lock()
        self._current = IdentityObservation(
            self._original_ref, original["observed_at"],
            min(original["expires_at"], self.execution_not_after), self._original_ref,
            self.original_identity_not_after, self.execution_not_after,
        )
        self._issued = {self._current.reference: self._current}
        auth = [self.journal._read(ref)["payload"] for ref in original["sources"]
                if self.journal._read(ref)["payload"].get("auth_method")]
        if (
            len(auth) != 1
            or auth[0].get("account_hash") != native.expected_account
            or auth[0].get("auth_method") != "claude.ai"
            or auth[0].get("configuration_digest") != native.binding.configuration_digest
        ):
            raise AccessDenied("Original native account identity is not verified")
        self.prepare()

    def _owner_fields(self):
        n = self.native
        return (
            n, n.binding, n.policy, n.process, n.process.native_exit,
            n.startup, n.session, n.startup.model_guard,
        )

    def _bound_current(self):
        n = self.native
        if (
            self._revoked
            or any(a is not b for a, b in zip(self._owner, self._owner_fields(), strict=True))
            or n.journal is not self.journal
            or n.startup.ready_ref != self._original_ref
            or n.startup.native_session != self._session
            or n.expected_account != self._account
            or n.process.identity != self._identity
            or n.process.invocation is not self._invocation
            or self._caps is not self._original_caps
            or self.execution_not_after != self._initial_execution_cap
            or self.original_identity_not_after != self._initial_identity_cap
            or self._probe is not self._probe_binding[0]
            or self._probe.permits is not self._probe_binding[1]
            or n.binding.deadline != self._caps[1]
            or n.process.deadline != self._caps[2]
            or self._invocation.native.binding.deadline != self._caps[3]
            or self._invocation.relay.binding.deadline != self._caps[4]
            or self._issued.get(self._current.reference) is not self._current
            or self.journal.clock() >= min(self._current.expires_at, self.execution_not_after)
        ):
            raise AccessDenied("Native identity is expired, revoked or has changed owner")

    def _check_local(self, observed, metadata):
        self._bound_current()
        if (
            self._issued.get(observed.reference) is not observed
            or self.journal.clock() >= observed.expires_at
        ):
            raise AccessDenied("Prepared identity is not an unexpired issued observation")
        n = self.native
        n.process.check_current_liveness()
        with n.startup.model_guard.startup_current_boundary():
            n.policy.check_admission_metadata(metadata)
        return observed

    def prepare(self, *, cancelled=None):
        # Preparation is outside Core. Waiting here must not strand shutdown
        # behind an effect's model fence; local consumers still never wait.
        with self.native.startup.model_guard.observation_boundary(
            cancelled=cancelled, not_after=self.execution_not_after
        ):
            return self._prepare()

    def _prepare(self):
        self._bound_current()
        n = self.native
        n.verify_active()
        if digest(self.journal._read(self._original_ref, "startup", fresh=False)) != (
            self._original_digest
        ):
            raise AccessDenied("Original startup history changed")
        result = PreparedIdentity(self, self._current, n.policy.prepare_admission())
        result.check()
        return result

    def refresh(self, directory, *, cancelled=None):
        if not self._refresh_lock.acquire(blocking=False):
            raise AccessDenied("Native identity collection is already in progress")
        try:
            return self._refresh(directory, cancelled=cancelled)
        except BaseException:
            self._revoked = True
            raise
        finally:
            self._refresh_lock.release()

    def _refresh(self, directory, *, cancelled=None):
        self.prepare(cancelled=cancelled)
        n = self.native
        expected_account = n.expected_account
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=False, exist_ok=False)
        # The unchanged probe obtains a unique accounted invocation, performs
        # actual auth status and records its owned subprocess outcome.
        reference = self._probe.observe(n.policy, n.binding, directory)
        self.prepare(
            cancelled=cancelled
        )  # Collection must finish before the previous proof expires.
        data = self.journal._read(reference, "trace")
        if (
            n.expected_account != expected_account
            or data["payload"].get("account_hash") != expected_account
            or data["payload"].get("auth_method") != "claude.ai"
            or data["payload"].get("configuration_digest") != n.binding.configuration_digest
        ):
            raise AccessDenied("Fresh native account differs from the original approved owner")
        expires = min(data["expires_at"], self.execution_not_after)
        payload = {
            "effect": "native_identity_refreshed",
            "binding": asdict(n.binding),
            "process": asdict(self._identity),
            "session_id": self._session,
            "account_hash": expected_account,
            "model": n.policy.model,
            "effort": n.policy.effort,
            "original_startup_ref": self._original_ref,
            "original_startup_digest": self._original_digest,
            "original_identity_not_after": self.original_identity_not_after,
            "execution_not_after": self.execution_not_after,
        }
        current = self.journal._append("trace", payload, sources=(reference,), expires_at=expires)
        observed = IdentityObservation(
            current,
            data["observed_at"],
            expires,
            self._original_ref,
            self.original_identity_not_after,
            self.execution_not_after,
        )
        self._issued[current] = observed
        self._current = observed
        prepared = self.prepare(cancelled=cancelled)
        # Later SDK system/init frames must consume this successfully verified
        # same-native auth child, not the expired original startup auth receipt.
        # The original signed startup and its sources remain unchanged.
        n.startup.claude_auth_ref = reference
        return prepared

    def revoke(self):
        self._revoked = True
