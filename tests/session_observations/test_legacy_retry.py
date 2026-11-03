"""Completed setup cannot manufacture missing legacy provider continuity."""

import hashlib
import hmac
import importlib
import json
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.decisions.journal import canonical
from ai_task_manager.adapters.linear.receipts import ProviderJournal
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import EvidenceRequired
from ai_task_manager.installation.sessions import setup
from tests.session_observations.test_observations import install, service
from tests.session_observations.test_observations import provider as provider
from tests.session_observations.test_renewal import records

session_setup_module = importlib.import_module("ai_task_manager.installation.sessions.setup")


def legacy_origin(s, present):
    reference = "session-setup-origin:" + digest(s.grant_ref)
    with s.decisions.journal.transaction() as conn:
        if present:
            raw = conn.execute("SELECT data FROM records WHERE ref=?", (reference,)).fetchone()[0]
            origin = json.loads(raw)
            del origin["provider_journal"]
            raw = canonical(origin)
            mac = hmac.new(s.decisions.journal._key, raw.encode(), hashlib.sha256).hexdigest()
            conn.execute("UPDATE records SET data=?,mac=? WHERE ref=?", (raw, mac, reference))
        else:
            conn.execute("DELETE FROM records WHERE ref=?", (reference,))


def ordinary_setup(p, **changes):
    arguments = dict(
        logical_session="lead-session",
        actions=("enqueue", "start", "non_code_complete"),
        deadline=p.deadline,
        operation_key="original-setup",
    )
    arguments.update(changes)
    return setup(p.config, **arguments)


@pytest.mark.parametrize("origin_present", [False, True])
@pytest.mark.parametrize("replacement", ["original", "missing", "copied", "empty"])
def test_legacy_retry_refuses_before_constructors(
    provider, monkeypatch, origin_present, replacement
):
    p = provider
    install(p)
    s = service(p)
    legacy_origin(s, origin_present)
    path = p.root / "provider.sqlite"
    if replacement != "original":
        retained = path.with_suffix(".retained")
        path.rename(retained)
        if replacement == "copied":
            shutil.copyfile(retained, path)
            path.chmod(0o600)
        elif replacement == "empty":
            ProviderJournal(path)
    before = {file.name: file.read_bytes() for file in p.root.iterdir() if file.is_file()}
    requests = p.requests

    def forbidden(*args, **kwargs):
        pytest.fail("Legacy continuity must be checked before constructors")

    for name in ("AuthorityJournal", "LinearComponents", "Database"):
        monkeypatch.setattr(session_setup_module, name, forbidden)
    for _ in range(2):
        with pytest.raises(EvidenceRequired, match="reconciliation"):
            ordinary_setup(p)
    assert p.requests == requests
    assert {file.name: file.read_bytes() for file in p.root.iterdir() if file.is_file()} == before


@pytest.mark.parametrize("origin_present", [False, True])
def test_legacy_retry_cannot_enable_renewal_with_retained_unsettled_send(
    provider, monkeypatch, origin_present
):
    p = provider
    install(p)
    s = service(p)
    legacy_origin(s, origin_present)
    s.core.context.principals.begin_send(
        s.scope, SimpleNamespace(id="owned-unsettled-send", attempt_count=1)
    )
    path = p.root / "provider.sqlite"
    retained = path.with_suffix(".retained")
    path.rename(retained)
    before = p.config.read_bytes(), records(s), (p.root / "session.json").read_bytes()
    monkeypatch.setattr(HttpTransport, "ENDPOINT", p.endpoint)
    for _ in range(2):
        with pytest.raises(EvidenceRequired, match="reconciliation"):
            ordinary_setup(p)
    monkeypatch.setattr(session_setup_module, "time", SimpleNamespace(time=lambda: p.deadline + 1))
    with pytest.raises(EvidenceRequired, match="reconciliation"):
        ordinary_setup(
            p, deadline=p.deadline + 120, operation_key="explicit-renewal", renew_from=s.grant_ref
        )
    assert not path.exists()
    assert (p.config.read_bytes(), records(s), (p.root / "session.json").read_bytes()) == before
    with sqlite3.connect(retained) as conn:
        assert (
            conn.execute(
                "SELECT state FROM sends WHERE operation_id=?", ("owned-unsettled-send",)
            ).fetchone()[0]
            == "sending"
        )


def test_fresh_setup_and_completed_same_binding_retry_preserve_result(provider, monkeypatch):
    p = provider
    assert not (p.root / "authority.sqlite").exists()
    assert not (p.root / "provider.sqlite").exists()
    monkeypatch.setattr(HttpTransport, "ENDPOINT", p.endpoint)
    first = ordinary_setup(p)
    s = service(p)
    before = p.config.read_bytes(), p.config.stat().st_ino, records(s)
    assert ordinary_setup(p) == first
    assert (p.config.read_bytes(), p.config.stat().st_ino, records(s)) == before
