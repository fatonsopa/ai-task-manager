"""Expired delegation recovery preserves history and never adopts refused setup."""

import importlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_task_manager.adapters.decisions.observations import PREFIX, bind_record
from ai_task_manager.config.files import digest, read_private, write_private
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired, IdempotencyConflict
from tests.session_observations.helpers import OwnedProvider
from tests.session_observations.test_observations import install, service

session_setup_module = importlib.import_module("ai_task_manager.installation.sessions.setup")


@pytest.fixture
def provider(tmp_path):
    value = OwnedProvider(tmp_path / "owned")
    with tempfile.TemporaryDirectory(prefix="aitm-renewal-", dir=Path("/tmp").resolve()) as path:
        value.socket_root = Path(path)
        try:
            yield value
        finally:
            value.close()


def expired(p, monkeypatch):
    install(p)
    s = service(p)
    monkeypatch.setattr(session_setup_module, "time", SimpleNamespace(time=lambda: p.deadline + 1))
    s.decisions.journal.clock = lambda: p.deadline + 1
    return s


def renew(p, s, **changes):
    args = dict(operation_key="explicit-renewal", deadline=p.deadline + 120, renew_from=s.grant_ref)
    args.update(changes)
    return install(p, **args)


def records(s):
    with s.decisions.journal.transaction() as conn:
        return dict(conn.execute("SELECT ref,data FROM records"))


def test_expired_same_session_gets_new_delegation_without_rewriting_history(provider, monkeypatch):
    p = provider
    s = expired(p, monkeypatch)
    before, capability = records(s), read_private(p.root / "session.json")
    with pytest.raises(AccessDenied, match="stale"):
        s.decisions.observations.verify_enrollment(s.grant_ref)
    result = renew(p, s)
    assert result["enrollment_ref"] != s.grant_ref
    current = service(p)
    assert current.scope == s.scope
    grant = current.decisions.observations.verify_enrollment(current.grant_ref)
    assert grant.expires_at > p.deadline
    assert read_private(p.root / "session.json") == capability
    assert all(records(s)[key] == value for key, value in before.items())
    assert renew(p, s) == result
    with sqlite3.connect(p.root / "core.sqlite") as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1


def test_new_setup_refused_twice_before_any_new_origin_or_grant(provider):
    install(provider)
    s = service(provider)
    before = records(s)
    for _ in range(2):
        with pytest.raises(AccessDenied, match="adopt"):
            install(provider, operation_key="unapproved-adoption")
    assert records(s) == before


def test_old_refused_partial_record_cannot_become_adoption_on_retry(provider):
    p = provider
    install(p)
    s = service(p)
    config = read_private(p.config)
    ref = PREFIX + digest({"config": str(p.config), "key": "old-refused-setup"})
    from ai_task_manager.adapters.decisions.observations import intent

    bind_record(
        s.decisions.journal,
        ref,
        {
            "scope": asdict(s.scope),
            "provider": asdict(p.expected),
            "config_path": str(p.config),
            "config_intent": intent(config),
            "actions": ["enqueue", "start", "non_code_complete"],
            "expires_at": p.deadline,
        },
    )
    before = records(s)
    for _ in range(2):
        with pytest.raises(EvidenceRequired, match="legacy"):
            install(p, operation_key="old-refused-setup")
    assert records(s) == before


@pytest.mark.parametrize("fault", ["unexpired", "revoked", "foreign", "missing-result"])
def test_renewal_refuses_invalid_previous_authority(provider, monkeypatch, fault):
    p = provider
    s = expired(p, monkeypatch)
    if fault == "unexpired":
        monkeypatch.setattr(
            session_setup_module, "time", SimpleNamespace(time=lambda: p.deadline - 1)
        )
    elif fault == "revoked":
        s.decisions.observations.revoke()
    elif fault == "foreign":
        s.grant_ref = "session-enrollment:another"
    else:
        with s.decisions.journal.transaction() as conn:
            conn.execute(
                "DELETE FROM records WHERE ref=?", ("session-setup-result:" + digest(s.grant_ref),)
            )
    before = read_private(p.config)
    with pytest.raises(AccessDenied):
        renew(p, s)
    assert read_private(p.config) == before


def test_same_renewal_key_cannot_change_explicit_owner_arguments(provider, monkeypatch):
    p = provider
    s = expired(p, monkeypatch)
    renew(p, s)
    with pytest.raises(IdempotencyConflict):
        renew(p, s, workflow_control={"invalid": "changed"})


def test_unpublished_initial_intent_cannot_be_adopted_by_another_setup(provider, monkeypatch):
    from ai_task_manager.config.sessions import SessionControl

    original = SessionControl.establish
    monkeypatch.setattr(
        SessionControl,
        "establish",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("stopped before establishment")),
    )
    with pytest.raises(ConnectionError):
        install(provider)
    monkeypatch.setattr(SessionControl, "establish", original)
    with pytest.raises(IdempotencyConflict):
        install(provider, operation_key="competing-setup")


def owner_config(p):
    value = {
        "record": str(p.root / "old-owner.json"),
        "endpoint": str(p.socket_root / "old-owner.sock"),
        "instance": str(uuid4()),
    }
    config = read_private(p.config)
    write_private(p.config, {**config, "workflow_control": value}, replace=True)
    return value


@pytest.mark.parametrize("live", [False, True])
def test_owner_history_preserved_and_live_process_refused(provider, monkeypatch, live):
    from ai_task_manager.adapters.workflow_control.identity import file_binding, process_start

    p = provider
    old = owner_config(p)
    s = expired(p, monkeypatch)
    child = subprocess.Popen([sys.executable, "-I", "-c", "import time;time.sleep(10)"])
    try:
        start = process_start(child.pid)
        descriptor = {
            "scope": asdict(s.scope),
            "instance": old["instance"],
            "config": file_binding(p.config),
            "pid": child.pid,
            "start": start,
        }
        write_private(old["record"], descriptor)
        if not live:
            child.terminate()
            child.wait(timeout=3)
        fresh = {
            "record": str(p.root / "new-owner.json"),
            "endpoint": str(p.socket_root / "new-owner.sock"),
            "instance": str(uuid4()),
        }
        if live:
            before = read_private(p.config)
            with pytest.raises(EvidenceRequired, match="process"):
                renew(p, s, workflow_control=fresh)
            assert read_private(p.config) == before
        else:
            result = renew(p, s, workflow_control=fresh)
            assert result["capability_saved"]
            assert read_private(p.config)["workflow_control"] == fresh
            assert read_private(old["record"]) == descriptor
            assert not os.path.exists(fresh["record"])
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=3)


def test_interrupted_renewal_before_config_publication_is_idempotent(provider, monkeypatch):
    p = provider
    s = expired(p, monkeypatch)
    original = session_setup_module.write_private
    monkeypatch.setattr(
        session_setup_module,
        "write_private",
        lambda *a, **kw: (_ for _ in ()).throw(
            ConnectionError("lost before new configuration publication")
        ),
    )
    with pytest.raises(ConnectionError):
        renew(p, s)
    assert read_private(p.config)["grant_ref"] == s.grant_ref
    monkeypatch.setattr(session_setup_module, "write_private", original)
    result = renew(p, s)
    assert service(p).grant_ref == result["enrollment_ref"]


@pytest.mark.parametrize(
    "fault", ["unknown-attempt", "invalid-attempt", "resources", "leased", "uncertain"]
)
def test_unsettled_core_work_prevents_renewal(provider, monkeypatch, fault):
    from tests.session_observations.test_observations import _confirmed_task_for_retry

    p = provider
    s = expired(p, monkeypatch)
    task = _confirmed_task_for_retry(s)
    sid, _ = s.core.session_binding(s.handle)
    with s.core.context.db.transaction() as conn:
        if fault in {"leased", "uncertain"}:
            conn.execute("UPDATE outbox SET state=? WHERE session_id=?", (fault, sid))
        else:
            aid = str(uuid4())
            conn.execute(
                "INSERT INTO attempts VALUES(?,?,?,?,?,?)",
                (
                    aid,
                    task.id,
                    sid,
                    "owned-test-host",
                    0,
                    json.dumps(
                        {
                            "state": {
                                "unknown-attempt": "unknown",
                                "invalid-attempt": "invalid",
                                "resources": "paused",
                            }[fault]
                        }
                    ),
                ),
            )
            if fault == "resources":
                conn.execute(
                    "INSERT INTO resource_leases VALUES(?,?,?)",
                    (
                        "worktree",
                        "owned-test-worktree",
                        aid,
                    ),
                )
    before = read_private(p.config)
    with pytest.raises(EvidenceRequired):
        renew(p, s)
    assert read_private(p.config) == before


def test_unsettled_provider_send_prevents_renewal(provider, monkeypatch):
    from types import SimpleNamespace

    p = provider
    s = expired(p, monkeypatch)
    s.core.context.principals.begin_send(s.scope, SimpleNamespace(id="owned-send", attempt_count=1))
    with pytest.raises(EvidenceRequired, match="sends"):
        renew(p, s)


def test_setup_lock_rejects_competing_installer(provider):
    from ai_task_manager.installation.sessions.recovery import setup_lock

    with setup_lock(provider.config):
        with pytest.raises(EvidenceRequired, match="Another"):
            install(provider)
