"""Declared lead protocol with the real durable capability-read boundary."""

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.application.service.models import Unavailable
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.native import delegated_lead, lead_startup
from ai_task_manager.installation.native.enrollment import CaptureEnrollments, physical
from ai_task_manager.installation.native.normal import NormalAuthoritySource
from ai_task_manager.installation.native.store import NativeStore
from ai_task_manager.presentation.cli.main import main
from tests.native_installation import test_lead_startup

lead = test_lead_startup.lead


def declared_capability(store, root):
    journal = HostJournal(root / "capability.sqlite", root / "capability.key")
    row = {
        "reference": "declared-contract",
        "capture_hash": "c" * 64,
        "profile_digest": "d" * 64,
        "revoked": False,
        "valid_until": time.time() + 60,
        "capture": {},
        "journal": physical(journal.path),
        "key": physical(journal.key_path),
        "sources": {},
    }
    with store.transaction() as conn:
        store.put(conn, "native_conformance", row["reference"], row)
    return row


class DurableDeclaredRestrictions(test_lead_startup.DeclaredRestrictions):
    """Still an authored capability fixture, now using the production DB reader."""

    def __init__(self, store):
        self.enrollment = CaptureEnrollments(store)

    def require_installation(self, manifest):
        super().require_installation(manifest)
        row = self.enrollment.require("declared-contract", "d" * 64)
        if row["revoked"] or row["valid_until"] <= time.time():
            raise AccessDenied("Declared capability revoked or expired")


@pytest.mark.parametrize("lead", ["delegated"], indirect=True)
def test_LT01_delegated_lead_reads_capability_inside_actual_input_boundary(
    lead, monkeypatch, capsys
):
    p, b, store, manifest, service, event = lead
    declared_capability(store, b.root)

    def source(m, **kw):
        owner = NormalAuthoritySource(m, **kw)
        owner.conformance = DurableDeclaredRestrictions(owner.store)
        return owner

    monkeypatch.setattr(lead_startup, "NormalAuthoritySource", source)
    monkeypatch.setattr(delegated_lead, "NormalAuthoritySource", source)
    code = main(
        [
            "--config",
            str(p.config),
            "--installation",
            str(b.installation),
            "--json",
            "lead",
            "report",
        ]
    )
    output = capsys.readouterr()
    assert code == 0, output.err
    assert json.loads(output.out)["data"]["event_id"] == event.id
    with service.core.context.db.transaction() as conn:
        stages = {
            r[0] for r in conn.execute("SELECT stage FROM receipts WHERE event_id=?", (event.id,))
        }
    assert stages == {"queued", "delivered", "ai_acknowledged", "user_reported"}


def test_LT02_same_owned_transaction_reads_uncommitted_revocation_and_rolls_back(tmp_path):
    store = NativeStore.create(tmp_path / "normal.sqlite")
    row = declared_capability(store, tmp_path)
    enrollment = CaptureEnrollments(store)
    with pytest.raises(RuntimeError, match="rollback"):
        with store.transaction() as conn:
            store.put(conn, "native_conformance", row["reference"], {**row, "revoked": True})
            assert enrollment.require(row["reference"], row["profile_digest"])["revoked"] is True
            raise RuntimeError("rollback")
    assert enrollment.require(row["reference"], row["profile_digest"])["revoked"] is False


@pytest.mark.parametrize("boundary", ["other_store_instance", "other_thread"])
def test_LT03_owned_connection_is_not_shared_across_boundaries(tmp_path, boundary):
    store = NativeStore.create(tmp_path / "normal.sqlite")
    row = declared_capability(store, tmp_path)
    other = NativeStore(store.path, store.pointer)
    with store.transaction():
        if boundary == "other_store_instance":
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.conformance(row["reference"])
        else:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(store.conformance, row["reference"])
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    future.result(timeout=2)
        assert store.conformance(row["reference"]) == row
    assert other.conformance(row["reference"]) == row


@pytest.mark.parametrize(
    "boundary", ["missing", "foreign_profile", "replaced_key", "store_identity"]
)
def test_LT04_active_read_preserves_enrollment_and_store_refusals(tmp_path, boundary):
    store = NativeStore.create(tmp_path / "normal.sqlite")
    row = declared_capability(store, tmp_path)
    enrollment = CaptureEnrollments(store)
    error = Unavailable if boundary == "missing" else AccessDenied
    with pytest.raises(error):
        with store.transaction() as conn:
            if boundary == "missing":
                enrollment.require("not-enrolled", row["profile_digest"])
            elif boundary == "foreign_profile":
                enrollment.require(row["reference"], "foreign-profile")
            elif boundary == "replaced_key":
                key = tmp_path / "capability.key"
                key.rename(tmp_path / "original.key")
                key.write_bytes(b"different declared key")
                key.chmod(0o600)
                enrollment.require(row["reference"], row["profile_digest"])
            else:
                conn.execute("UPDATE native_meta SET version=99")
                enrollment.require(row["reference"], row["profile_digest"])
    if boundary == "store_identity":
        assert enrollment.require(row["reference"], row["profile_digest"])["revoked"] is False
