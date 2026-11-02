import sqlite3

import pytest

from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.hosts.bridge.transcript import redacted
from ai_task_manager.domains.work.errors import AccessDenied, SchemaError


def test_receipts_retain_integrity_and_source_on_restart(journal):
    source = journal._append("trace", {"frame_hash": "a" * 64})
    ref = journal._append(
        "startup",
        {"native_session": "observed"},
        sources=(source,),
        expires_at=journal.clock() + 30,
    )
    restarted = HostJournal(journal.path, journal.key_path)
    assert restarted._read(ref, "startup")["sources"] == [source]
    with journal.transaction() as conn:
        conn.execute("UPDATE host_records SET data=? WHERE ref=?", ("{}", source))
    with pytest.raises(AccessDenied):
        restarted._read(ref)


@pytest.mark.parametrize("ref", [None, "", "runtime-proof:wrong", "host-record:missing"])
def test_unknown_receipts_refuse(journal, ref):
    with pytest.raises(AccessDenied):
        journal._read(ref)


def test_expiry_and_source_lifetime(journal):
    now = journal.clock()
    journal.clock = lambda: now
    source = journal._append("trace", {}, expires_at=now + 1)
    child = journal._append("startup", {}, sources=(source,), expires_at=now + 60)
    assert journal._read(child)["expires_at"] == now + 1
    journal.clock = lambda: now + 2
    with pytest.raises(AccessDenied):
        journal._read(child)
    assert journal._read(child, fresh=False)["sources"] == [source]


def test_intents_preserve_unknown_ownership(journal):
    assert journal.intend("attempt", {"deadline": 123, "stage": "launch_intent"})
    assert not journal.intend("attempt", {"deadline": 123, "stage": "launch_intent"})
    with pytest.raises(AccessDenied):
        journal.intend("attempt", {"deadline": 456, "stage": "launch_intent"})
    assert HostJournal(journal.path, journal.key_path).intent("attempt")["deadline"] == 123


def test_foreign_database_is_not_modified(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "foreign.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE provider_secret(value TEXT)")
    conn.close()
    path.chmod(0o600)
    before = path.read_bytes()
    with pytest.raises(SchemaError):
        HostJournal(path, private / "key")
    assert path.read_bytes() == before


def test_secret_fields_are_redacted_recursively():
    result = redacted(
        {"accessToken": "secret", "nested": [{"OPENAI_API_KEY": "key"}], "id": "safe"}
    )
    assert "secret" not in str(result) and "'key'" not in str(result)
    assert result["id"] == "safe"
