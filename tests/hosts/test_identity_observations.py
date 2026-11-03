"""Owned authored auth-status processes; no actual account or model evidence."""

import time

import pytest

from ai_task_manager.adapters.hosts.bridge.continuity import NativeContinuity
from ai_task_manager.adapters.hosts.bridge.identity import NativeIdentityObservations
from ai_task_manager.domains.work.errors import AccessDenied
from tests.hosts.bootstrap_helpers import bootstrap_graph as bootstrap_graph


class ScopedAuthPermits:
    def __init__(self, item):
        self.item, self.permit, self.after = item, None, None
        self.maximum = 10

    def authorize_auth(self, policy, binding):
        self.permit = self.item.accounting.claim(
            policy, binding, "auth", maximum=self.maximum,
            parent=self.item.native.process.invocation.native.reference,
        )
        return self.permit

    def record_auth(self, binding, completion):
        assert binding is self.item.binding
        self.item.accounting.control.record_outcome(
            self.permit.reference, outcome=completion["outcome"],
            process_stopped=completion["group"]["confirmed_stop"],
            actual_cost_usd="0" if completion["group"]["confirmed_stop"] else None,
            evidence_ref="actual owned authored identity status command",
        )
        if self.after:
            self.after()


def identity(bootstrap_graph):
    item = bootstrap_graph()
    # A trusted virtual clock makes the first identity window shorter than the
    # independently fixed execution window. No old receipt is rewritten.
    item.journal.clock = lambda: time.time() - 31
    item.bootstrap.start()
    item.journal.clock = time.time
    permits = ScopedAuthPermits(item)
    observer = NativeIdentityObservations(item.native, permits, not_after=item.binding.deadline)
    return item, permits, observer


def test_ni01_new_actual_status_observation_preserves_original_identity_and_cap(bootstrap_graph):
    item, _, observer = identity(bootstrap_graph)
    original = item.journal._read(item.native.startup.ready_ref)
    before = observer.prepare().observation
    current = observer.refresh(item.root / "fresh-auth")
    assert current.observation.reference != before.reference
    assert current.observation.expires_at > before.expires_at
    assert current.observation.expires_at <= observer.execution_not_after
    assert current.observation.original_identity_not_after == before.expires_at
    assert item.journal._read(item.native.startup.ready_ref) == original
    current.check()
    # An observation is not an automatic grant/default-continuity opt-in.
    legacy = NativeContinuity(item.native)
    item.journal.clock = lambda: before.expires_at + 0.1
    with pytest.raises(AccessDenied):
        legacy.prepare()


def test_ni02_expired_identity_refuses_without_starting_auth(bootstrap_graph):
    item, _, observer = identity(bootstrap_graph)
    count = item.accounting.number
    expiry = observer.prepare().observation.expires_at
    item.journal.clock = lambda: expiry
    with pytest.raises(AccessDenied):
        observer.refresh(item.root / "must-not-start")
    assert item.accounting.number == count
    assert not (item.root / "must-not-start").exists()


def test_ni03_probe_finishing_after_previous_expiry_cannot_renew_it(bootstrap_graph):
    item, permits, observer = identity(bootstrap_graph)
    before = observer.prepare().observation
    permits.after = lambda: setattr(item.journal, "clock", lambda: before.expires_at + 0.1)
    with pytest.raises(AccessDenied):
        observer.refresh(item.root / "late-auth")
    assert observer._current is before
    call = next(c for c in item.accounting.control.snapshot()["calls"]
                if c["call_id"] == permits.permit.reference)
    assert call["process_stopped"] is True and call["outcome"] == "succeeded"


@pytest.mark.parametrize("kind", ["account", "session", "execution_cap", "revoked"])
def test_ni04_05_06_binding_cap_and_revocation_refuse_before_auth(bootstrap_graph, kind):
    item, _, observer = identity(bootstrap_graph)
    count = item.accounting.number
    if kind == "account":
        item.native.expected_account = "foreign"
    elif kind == "session":
        item.native.startup.native_session = "foreign"
    elif kind == "execution_cap":
        observer.execution_not_after += 1
    else:
        observer.revoke()
    with pytest.raises(AccessDenied):
        observer.refresh(item.root / "must-not-start")
    assert item.accounting.number == count


def test_ni06_prepared_observation_cannot_survive_revocation(bootstrap_graph):
    _, _, observer = identity(bootstrap_graph)
    prepared = observer.prepare()
    observer.revoke()
    with pytest.raises(AccessDenied):
        prepared.check()
