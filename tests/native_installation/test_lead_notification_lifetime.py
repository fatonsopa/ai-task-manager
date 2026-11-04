"""Normal lead observation chain across a declared startup-admission expiry."""

import pytest

from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.config.managed import ManagedServiceSlot
from tests.native_installation.test_lead_startup import lead as lead
from tests.native_installation.test_lead_startup import (
    test_LC01_installed_entrypoint_native_operator_receipts_and_restart as complete_report,
)


@pytest.mark.parametrize("lead", ["delegated"], indirect=True)
def test_delegated_reporting_outlives_startup_admission_with_current_authority(
    lead, monkeypatch, capsys
):
    original_append = HostJournal._append
    observed = {}

    def short_admission(journal, kind, payload, **kwargs):
        if payload.get("auth_method") == "claude.ai":
            kwargs["expires_at"] = journal.clock() + 10
            observed["admission_expiry"] = kwargs["expires_at"]
        return original_append(journal, kind, payload, **kwargs)

    original_publish = ManagedServiceSlot.publish_notification

    def after_admission(slot, generation, notifications, event_id):
        now = observed["admission_expiry"] + 0.1
        assert now < notifications.binding.deadline
        # Advance only the declared receipt clocks. The actual normal producer,
        # provider observer, signed sources and public PTY remain in the chain.
        slot._service.decisions.journal.clock = lambda: now
        notifications.journal.clock = lambda: now
        observed["published_after_admission"] = True
        return original_publish(slot, generation, notifications, event_id)

    monkeypatch.setattr(HostJournal, "_append", short_admission)
    monkeypatch.setattr(ManagedServiceSlot, "publish_notification", after_admission)
    complete_report(lead, capsys, monkeypatch)
    assert observed["published_after_admission"]


@pytest.mark.parametrize("lead", ["delegated"], indirect=True)
@pytest.mark.parametrize("boundary", ["input", "publication"])
def test_real_elapsed_startup_window_keeps_finite_delegated_reporting(
    lead, monkeypatch, capsys, boundary
):
    import time

    from ai_task_manager.installation.native.lead_startup import LeadAccounting

    append = HostJournal._append
    expiry = []

    def short_admission(journal, kind, payload, **kwargs):
        if payload.get("auth_method") == "claude.ai":
            kwargs["expires_at"] = journal.clock() + 8
            expiry.append(kwargs["expires_at"])
        return append(journal, kind, payload, **kwargs)

    waited = []

    def wait_for_expiry():
        remaining = expiry[0] + 0.05 - time.time()
        assert 0 < remaining < 8
        time.sleep(remaining)
        assert time.time() > expiry[0]
        waited.append(remaining)

    event_turn = LeadAccounting.event_turn
    publish = ManagedServiceSlot.publish_notification

    def delayed_input(owner, event_id):
        wait_for_expiry()
        return event_turn(owner, event_id)

    def delayed_publication(slot, generation, notifications, event_id):
        wait_for_expiry()
        return publish(slot, generation, notifications, event_id)

    monkeypatch.setattr(HostJournal, "_append", short_admission)
    if boundary == "input":
        monkeypatch.setattr(LeadAccounting, "event_turn", delayed_input)
    else:
        monkeypatch.setattr(ManagedServiceSlot, "publish_notification", delayed_publication)
    complete_report(lead, capsys, monkeypatch)
    assert len(waited) == 1


@pytest.mark.parametrize("lead", ["delegated"], indirect=True)
@pytest.mark.parametrize("fault", ["legacy_expired", "source_expired", "revoked"])
def test_expired_or_revoked_source_never_publishes(lead, monkeypatch, fault):
    import json

    from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
    from ai_task_manager.installation.native import lead_startup

    p, b, store, manifest, service, event = lead
    append = HostJournal._append
    publish = ManagedServiceSlot.publish_notification
    seen = {}

    def enrollment(journal, kind, payload, **kwargs):
        if payload.get("auth_method") == "claude.ai":
            kwargs["expires_at"] = journal.clock() + 10
        if kind == "delegated_enrollment":
            if fault == "legacy_expired":
                payload = {
                    k: v
                    for k, v in payload.items()
                    if k not in {"provenance_version", "startup_hash"}
                }
                kwargs["sources"] = (payload["startup_ref"],)
            ref = append(journal, kind, payload, **kwargs)
            seen["expires"] = journal._read(ref)["expires_at"]
            return ref
        return append(journal, kind, payload, **kwargs)

    def refuse_publication(slot, generation, notifications, event_id):
        seen["boundary"] = True
        if fault == "revoked":
            store.revoke(manifest.data["authorization_ref"])
        else:
            now = seen["expires"] + 0.01
            slot._service.decisions.journal.clock = lambda: now
            notifications.journal.clock = lambda: now
        return publish(slot, generation, notifications, event_id)

    monkeypatch.setattr(HostJournal, "_append", enrollment)
    monkeypatch.setattr(ManagedServiceSlot, "publish_notification", refuse_publication)
    with pytest.raises((AccessDenied, EvidenceRequired)):
        lead_startup.report(p.config, b.installation)
    assert seen["boundary"]
    receipts = service.core.notifications.receipts(service.handle, event.id)
    assert set(receipts) == {"queued", "delivered", "ai_acknowledged"}
    spec = json.loads(b.installation.read_text())
    journal = HostJournal.open_existing(spec["lead"]["journal"], spec["lead"]["key"])
    assert not [
        r for r in journal._history("notification") if r["payload"]["stage"] == "user_reported"
    ]
    with store.transaction() as conn:
        assert not conn.execute("SELECT 1 FROM native_calls WHERE process_stopped=0").fetchall()


@pytest.mark.parametrize("lead", ["delegated"], indirect=True)
def test_legacy_enrollment_keeps_original_validity(lead, monkeypatch, capsys):
    append = HostJournal._append

    def legacy(journal, kind, payload, **kwargs):
        if kind == "delegated_enrollment":
            payload = {
                k: v for k, v in payload.items() if k not in {"provenance_version", "startup_hash"}
            }
            kwargs["sources"] = (payload["startup_ref"],)
        return append(journal, kind, payload, **kwargs)

    monkeypatch.setattr(HostJournal, "_append", legacy)
    complete_report(lead, capsys, monkeypatch)


@pytest.mark.parametrize("lead", ["delegated"], indirect=True)
@pytest.mark.parametrize("fault", ["model", "account", "source"])
def test_cold_historical_startup_still_requires_exact_current_sources(
    lead, monkeypatch, capsys, fault
):
    import json
    from copy import deepcopy

    from ai_task_manager.config.service import load_service
    from ai_task_manager.domains.work.errors import AccessDenied
    from ai_task_manager.installation.native.delegated_lead import verify

    p, b, store, manifest, service, event = lead
    complete_report(lead, capsys, monkeypatch)
    spec = json.loads(b.installation.read_text())
    journal = HostJournal.open_existing(spec["lead"]["journal"], spec["lead"]["key"])
    record = journal._history("delegated_enrollment")[0]
    startup = journal._read(record["payload"]["startup_ref"], fresh=False)
    # The fixture's host bound is shorter than its default startup TTL. Narrow
    # startup freshness at creation in the other controls; this one checks
    # immutable version/hash and cold model/account/source guards directly.
    assert record["payload"]["provenance_version"] == 2
    cold = load_service(p.config)
    grant = cold.decisions.observations.verify_enrollment(cold.grant_ref)
    for field, value in (
        ("startup_hash", "0" * 64),
        ("provenance_version", 3),
        ("event_id", "foreign"),
        ("binding", {}),
    ):
        changed = deepcopy(record)
        changed["payload"][field] = value
        with pytest.raises(AccessDenied):
            verify(journal, changed, grant, record["payload"]["binding"], service.session_id)
    changed = deepcopy(record)
    changed["sources"] = [startup["ref"]]
    with pytest.raises(AccessDenied):
        verify(journal, changed, grant, record["payload"]["binding"], service.session_id)
    receipt = journal._history("notification")[-1]["ref"]
    if fault == "source":
        b.manifest.write_text(b.manifest.read_text() + " ")
    else:
        rows = [
            r
            for r in journal._history("trace")
            if r["payload"].get("frame", {}).get("type") == "system"
        ]
        payload = deepcopy(rows[0]["payload"])
        payload["frame"]["model" if fault == "model" else "apiKeySource"] = "foreign"
        journal._append("trace", payload)
    with pytest.raises(AccessDenied):
        cold.decisions.observations.verify_host_observation(receipt)
