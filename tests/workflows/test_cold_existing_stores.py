"""Existing recovery stores cannot initialize missing or older history."""

import sqlite3

import pytest

from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.linear.receipts import ProviderJournal
from ai_task_manager.domains.work.errors import AccessDenied, WorkError
from ai_task_manager.supervisor.journal import RuntimeJournal
from ai_task_manager.supervisor.models import RuntimeRefused


@pytest.mark.parametrize("kind", ["authority", "host", "runtime", "provider"])
def test_existing_journal_preserves_current_then_refuses_legacy_and_missing(tmp_path, kind):
    path, key = tmp_path / "original.sqlite", tmp_path / "original.key"
    constructors = {
        "authority": (
            lambda: AuthorityJournal(path, key),
            lambda: AuthorityJournal.open_existing(path, key),
            "authority_version",
            1,
        ),
        "host": (
            lambda: HostJournal(path, key),
            lambda: HostJournal.open_existing(path, key),
            "host_version",
            1,
        ),
        "runtime": (
            lambda: RuntimeJournal(path),
            lambda: RuntimeJournal.open_existing(path),
            "runtime_metadata",
            0,
        ),
        "provider": (
            lambda: ProviderJournal(path),
            lambda: ProviderJournal.open_existing(path),
            "metadata",
            3,
        ),
    }
    create, opening, table, legacy = constructors[kind]
    create()
    before = path.read_bytes()
    store = opening()
    with store.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 1
    assert path.read_bytes() == before
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE " + table + " SET version=?", (legacy,))
    legacy_bytes = path.read_bytes()
    with pytest.raises((AccessDenied, WorkError, RuntimeRefused)):
        opening()
    assert path.read_bytes() == legacy_bytes
    path.rename(path.with_suffix(".retained"))
    with pytest.raises((OSError, sqlite3.Error, AccessDenied, RuntimeRefused)):
        opening()
    assert not path.exists()


def test_existing_core_uses_enrolled_identity_without_schema_migration(tmp_path):
    import shutil

    from ai_task_manager.config.claim_store import enroll_claim_store, open_claim_store
    from tests.domains.helpers import Rig

    rig = Rig(tmp_path / "core.sqlite")
    path = tmp_path / "core.sqlite"
    path.chmod(0o600)
    authority = AuthorityJournal(tmp_path / "authority.sqlite", tmp_path / "authority.key")
    enroll_claim_store(rig.db, authority)
    with sqlite3.connect(path) as conn:
        original = tuple(conn.iterdump())
    identity = path.stat().st_dev, path.stat().st_ino
    core, guard = open_claim_store(path, authority, existing_only=True)
    with core.transaction() as conn:
        guard.check(conn)
    with sqlite3.connect(path) as conn:
        assert tuple(conn.iterdump()) == original
    assert (path.stat().st_dev, path.stat().st_ino) == identity
    retained = path.with_suffix(".original")
    path.rename(retained)
    shutil.copyfile(retained, path)
    path.chmod(0o600)
    try:
        with pytest.raises(AccessDenied):
            open_claim_store(path, authority, existing_only=True)
    finally:
        path.unlink()
        retained.rename(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE metadata SET version=4")
    legacy = path.read_bytes()
    with pytest.raises(WorkError):
        open_claim_store(path, authority, existing_only=True)
    assert path.read_bytes() == legacy


def test_local_stop_composition_never_constructs_execution_provider(tmp_path, monkeypatch):
    from ai_task_manager.application.service import linear
    from ai_task_manager.config.files import read_private
    from ai_task_manager.domains.work.errors import EvidenceRequired
    from ai_task_manager.installation.workflows.cold import ColdRecovery
    from tests.application.helpers import ServiceRig

    fixture = ServiceRig(tmp_path)
    config_file = fixture.configuration()
    recovery = ColdRecovery.__new__(ColdRecovery)
    recovery.config = read_private(config_file)
    recovery.config["linear"] = {"credential_file": "MUST-NOT-READ"}
    recovery.journal = fixture.authority
    recovery.check = lambda: None  # Authored capability control; identity is tested separately.

    def unavailable(*args, **kwargs):
        pytest.fail("Stop composition must not authenticate an execution provider")

    monkeypatch.setattr(linear, "LinearComponents", unavailable)
    service = recovery.local_service()
    assert service.scope == fixture.rig.scope
    assert service.provider is None and service.workers is None and service.workflows is None
    with pytest.raises(EvidenceRequired):
        service.decisions.observations.verify_enrollment("expired-original-execution")
