"""Deterministic authored observations; these do not execute a native model."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.conformance import ConformanceCapture, ProbeTurn
from ai_task_manager.adapters.hosts.bridge.conformance.verification import (
    identity_at,
    verify_capture,
)
from ai_task_manager.adapters.hosts.bridge.identity_refresh import IdentityRefresh
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied
from tests.hosts.conformance.test_capture import native_frame
from tests.hosts.conformance.test_capture import rig as rig
from tests.hosts.conformance.test_profile import AuthoredFactory

# Frozen from the retained ACTUAL-INSTALLED-WORKFLOW-11 failure. The records
# below are newly authored local controls, not reused live observations.
PREVIOUS = 1793727104.586483
PREVIOUS_EXPIRY = 1793727164.563405
TERMINAL = 1793727150.937757
POST_CHECK = 1793727151.194037
POST_EXPIRY = 1793727211.155103


class TimingFactory(AuthoredFactory):
    def __init__(self, r, change=None):
        super().__init__(r)
        self.change = change
        self.checked_after = False
        self.current = self.previous = self.during = self.post = None
        self.turn_done = False

    def refreshed(self, native, expires, **changes):
        r = self.r
        auth = r.journal._append(
            "trace",
            {
                "auth_method": "claude.ai",
                "account_hash": r.context.account_hash,
                "configuration_digest": native.binding.configuration_digest,
            },
            expires_at=expires,
        )
        return r.journal._append(
            "trace",
            {
                "effect": "native_identity_refreshed",
                **r.owner,
                "session_id": r.session,
                "account_hash": r.context.account_hash,
                "original_startup_ref": native.startup.ready_ref,
                **changes,
            },
            sources=(auth,),
            expires_at=expires,
        )

    def start(self, context, gateway):
        native = super().start(context, gateway)
        expires = (
            TERMINAL
            if self.change
            in {
                "expired",
                "during",
                "foreign_owner",
                "foreign_session",
                "foreign_account",
                "foreign_startup",
            }
            else PREVIOUS_EXPIRY
        )
        self.current = self.previous = self.refreshed(native, expires)
        helper = object.__new__(IdentityRefresh)

        def prepare():
            def check():
                # The production orchestration must still require a fresh
                # current observation after the terminal has been recorded.
                r.journal._read(self.current)

            check()
            return SimpleNamespace(observation=SimpleNamespace(reference=self.current), check=check)

        r = self.r
        helper.observations = SimpleNamespace(native=native, prepare=prepare)
        native.identity = native._identity_bound = helper

        def active():
            if self.turn_done:
                self.checked_after = True
                r.now[0] = POST_CHECK
                self.current = self.post = self.refreshed(native, POST_EXPIRY)
                self.turn_done = False
                if self.change == "current_expired":
                    r.now[0] = POST_EXPIRY
            return native.process.identity

        native.verify_active = active
        self.native = native
        return native

    def execute(self, context, native, case):
        r = self.r
        if case.case_id != "native_shell_absent":
            return super().execute(context, native, case)
        r.now[0] = PREVIOUS + 20
        turn = super().execute(context, native, case)
        if self.change in {
            "during",
            "foreign_owner",
            "foreign_session",
            "foreign_account",
            "foreign_startup",
        }:
            r.now[0] = TERMINAL - 1
            changes = {
                "foreign_owner": {"process": {**r.owner["process"], "pid": 999}},
                "foreign_session": {"session_id": "foreign"},
                "foreign_account": {"account_hash": digest("foreign")},
                "foreign_startup": {"original_startup_ref": "host-record:foreign"},
            }.get(self.change, {})
            self.during = self.current = self.refreshed(native, POST_EXPIRY, **changes)
        r.now[0] = TERMINAL
        terminal = native_frame(
            r, {"type": "result", "subtype": "success", "is_error": False, "session_id": r.session}
        )
        self.turn_done = True
        if self.change == "source":
            r.pins.files[0][1].write_text("changed source")
        elif self.change == "account":
            native.expected_account = digest("changed-account")
        elif self.change == "identity_source":
            native.identity = object()
        elif self.change == "tampered_source":
            source = r.journal._read(self.previous, fresh=False)["sources"][0]
            with r.journal.transaction() as conn:
                conn.execute("UPDATE host_records SET data=data || ' ' WHERE ref=?", (source,))
        return ProbeTurn(turn.request_ref, terminal, turn.invocation_ref)


def capture_at_actual_times(r, change=None):
    r.now[0] = PREVIOUS
    r.context = replace(r.context, deadline=POST_EXPIRY + 60)
    r.authority.context = r.context
    r.manifest.installation.path.write_text(json.dumps({"deadline": POST_EXPIRY + 120}))
    (r.root / "input.txt").unlink()
    capture = ConformanceCapture(
        r.manifest, r.pins, r.context, r.authority, r.journal, profile=r.profile
    )
    return capture, TimingFactory(r, change)


@pytest.mark.parametrize("change", [None, "during"])
def test_terminal_keeps_covering_identity_when_post_check_refreshes(rig, change):
    capture, factory = capture_at_actual_times(rig, change)
    ref = capture.run(factory)
    record, _, _ = verify_capture(rig.journal, ref)
    case = next(v for v in record["payload"]["cases"] if v["case_id"] == "native_shell_absent")
    assert factory.checked_after
    assert case["identity_before"] == factory.previous
    assert case["identity_after"] == (factory.during if change else factory.previous)
    assert capture._identity_ref == factory.post
    selected = rig.journal._read(case["identity_after"], fresh=False)
    terminal = rig.journal._read(case["terminal_ref"], fresh=False)
    later = rig.journal._read(factory.post, fresh=False)
    assert terminal["observed_at"] == TERMINAL
    assert selected["observed_at"] <= TERMINAL < selected["expires_at"]
    assert later["observed_at"] == POST_CHECK > TERMINAL
    assert later["expires_at"] == POST_EXPIRY
    with pytest.raises(AccessDenied, match="not fresh for the actual probe effect"):
        identity_at(rig.journal, factory.post, rig.owner, rig.session, TERMINAL)
    if change is None:
        assert (selected["observed_at"], selected["expires_at"]) == (PREVIOUS, PREVIOUS_EXPIRY)


@pytest.mark.parametrize(
    "change",
    [
        "expired",
        "foreign_owner",
        "foreign_session",
        "foreign_account",
        "foreign_startup",
        "source",
        "account",
        "identity_source",
        "tampered_source",
        "current_expired",
    ],
)
def test_historical_selection_cannot_replace_current_or_effect_identity_checks(rig, change):
    capture, factory = capture_at_actual_times(rig, change)
    with pytest.raises(AccessDenied):
        capture.run(factory)
    report = rig.journal._read(capture.report_ref, fresh=False)["payload"]
    assert report["error_type"] == "AccessDenied"
    assert report["confirmed_stop"]
    assert len(report["cases"]) == 8
    assert factory.native.process._closed
    with pytest.raises(AccessDenied):
        capture.completed()
