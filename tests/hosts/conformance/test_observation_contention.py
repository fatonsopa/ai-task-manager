"""Declared local concurrency control; no genuine account or model evidence."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

import pytest

from ai_task_manager.adapters.hosts.bridge.conformance import CanaryGateway, ConformanceCapture
from ai_task_manager.domains.work.errors import AccessDenied
from tests.native_installation.test_probe import probe as probe


@pytest.mark.parametrize(
    "change",
    [None, "identity_revoked", "permission_revoked", "account", "model", "expired", "source"],
)
def test_capture_consumes_identity_after_concurrent_observation(probe, monkeypatch, change):
    _, authority, context, journal, factory = probe
    gateway = CanaryGateway(context, authority, journal)
    native = factory.start(context, gateway)
    capture = ConformanceCapture(
        authority.manifest, authority.pins, context, authority, journal, profile=authority.profile
    )
    observer = native.identity.observations
    original = observer.prepare
    ready, held, release = Event(), Event(), Event()

    def prepare(**kwargs):
        value = original(**kwargs)
        # Capture already prepared current identity, then the other observation
        # acquires the guard before capture consumes the prepared observation.
        ready.set()
        assert held.wait(3)
        return value

    def hold():
        assert ready.wait(3)
        with native.startup.model_guard.observation_boundary(not_after=context.deadline):
            held.set()
            assert release.wait(3)

    monkeypatch.setattr(observer, "prepare", prepare)
    with ThreadPoolExecutor(max_workers=2) as pool:
        holding = pool.submit(hold)
        checking = pool.submit(capture.check, native)
        try:
            assert held.wait(3)
            with pytest.raises(TimeoutError):
                checking.result(timeout=0.1)
            if change == "identity_revoked":
                observer.revoke()
            elif change == "permission_revoked":
                authority.store.revoke(authority.reference)
            elif change == "account":
                native.expected_account = "foreign"
            elif change == "model":
                native.startup.model_guard.refusal = "declared-model-change"
            elif change == "source":
                authority.pins.files[0][1].write_text("changed authored executable")
            elif change == "expired":
                journal.clock = lambda: context.deadline
        finally:
            release.set()
        holding.result(timeout=3)
        if change is None:
            checking.result(timeout=3)
            assert capture._identity_ref == observer._current.reference
        else:
            with pytest.raises(AccessDenied):
                checking.result(timeout=3)
            assert capture._identity_ref is None


def test_core_current_identity_still_refuses_contention(probe):
    _, authority, context, journal, factory = probe
    gateway = CanaryGateway(context, authority, journal)
    native = factory.start(context, gateway)
    prepared = native.identity.observations.prepare()
    held, release = Event(), Event()

    def hold():
        with native.startup.model_guard.observation_boundary(not_after=context.deadline):
            held.set()
            assert release.wait(3)

    with ThreadPoolExecutor(max_workers=1) as pool:
        holding = pool.submit(hold)
        assert held.wait(3)
        try:
            with pytest.raises(AccessDenied, match="contended"):
                prepared.check()
        finally:
            release.set()
        holding.result(timeout=3)
