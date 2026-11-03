"""Ordinary file loss and replacement cannot turn renewal refusal into authority."""

import hashlib
import hmac
import json
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.decisions.journal import canonical
from ai_task_manager.adapters.linear.progress_store import ProgressStore
from ai_task_manager.adapters.linear.receipts import ProviderJournal
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.config.files import digest, read_private
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.installation.sessions import setup
from tests.session_observations.test_observations import service
from tests.session_observations.test_renewal import expired, records
from tests.session_observations.test_renewal import provider as provider


def ordinary_renew(p, s, monkeypatch):
    # Exercise default composition with an owned HTTP endpoint, including its
    # ordinary journal initialization behavior. No external provider is called.
    monkeypatch.setattr(HttpTransport, "ENDPOINT", p.endpoint)
    return setup(
        p.config,
        logical_session="lead-session",
        actions=("enqueue", "start", "non_code_complete"),
        deadline=p.deadline + 120,
        operation_key="explicit-renewal",
        renew_from=s.grant_ref,
    )


def copy_in_place(path):
    retained = path.with_suffix(path.suffix + ".retained")
    path.rename(retained)
    shutil.copyfile(retained, path)
    path.chmod(0o600)
    assert path.stat().st_ino != retained.stat().st_ino
    return retained


@pytest.mark.parametrize("filename", ["authority.sqlite", "authority.key"])
def test_completed_retry_refuses_copied_authority(provider, monkeypatch, filename):
    p = provider
    s = expired(p, monkeypatch)
    result = ordinary_renew(p, s, monkeypatch)
    before = p.config.read_bytes(), p.config.stat().st_ino
    prior = records(s)
    copy_in_place(p.root / filename)
    with pytest.raises(AccessDenied):
        ordinary_renew(p, s, monkeypatch)
    assert (p.config.read_bytes(), p.config.stat().st_ino) == before
    assert records(s) == prior
    with pytest.raises(AccessDenied):
        service(p).decisions.observations.verify_enrollment(result["enrollment_ref"])


@pytest.mark.parametrize("replacement", ["missing", "copied", "empty"])
@pytest.mark.parametrize("effect", ["send", "progress"])
def test_provider_history_replacement_preserves_renewal_refusal(
    provider, monkeypatch, replacement, effect
):
    p = provider
    s = expired(p, monkeypatch)
    journal = s.core.context.principals
    if effect == "send":
        journal.begin_send(s.scope, SimpleNamespace(id="owned-unsettled-send", attempt_count=1))
    else:
        progress = ProgressStore(journal)
        publication = progress.submit(s.scope, "owned-task", "owned-progress", {"body": "ready"})
        progress.claim(s.scope, publication["id"], "owned-worker", False)
    with pytest.raises(EvidenceRequired, match="sends|progress"):
        ordinary_renew(p, s, monkeypatch)
    before = p.config.read_bytes(), records(s), (p.root / "session.json").read_bytes()
    path = p.root / "provider.sqlite"
    with sqlite3.connect(path) as conn:
        provider_history = list(conn.iterdump())
    with sqlite3.connect(p.root / "core.sqlite") as conn:
        core_history = list(conn.iterdump())
    retained = path.with_suffix(".retained")
    path.rename(retained)
    if replacement == "copied":
        shutil.copyfile(retained, path)
        path.chmod(0o600)
    elif replacement == "empty":
        ProviderJournal(path)
    with pytest.raises(AccessDenied, match="provider journal"):
        ordinary_renew(p, s, monkeypatch)
    assert (p.config.read_bytes(), records(s), (p.root / "session.json").read_bytes()) == before
    if replacement == "missing":
        assert not path.exists()
    with sqlite3.connect(retained) as conn:
        assert list(conn.iterdump()) == provider_history
    with sqlite3.connect(p.root / "core.sqlite") as conn:
        assert list(conn.iterdump()) == core_history


def test_completed_retry_preserves_published_configuration(provider, monkeypatch):
    p = provider
    s = expired(p, monkeypatch)
    result = ordinary_renew(p, s, monkeypatch)
    before = p.config.read_bytes(), p.config.stat().st_ino, records(s)
    assert ordinary_renew(p, s, monkeypatch) == result
    assert (p.config.read_bytes(), p.config.stat().st_ino, records(s)) == before
    assert read_private(p.config)["grant_ref"] == result["enrollment_ref"]


@pytest.mark.parametrize("origin_present", [False, True])
def test_legacy_provider_origin_requires_reconciliation(provider, monkeypatch, origin_present):
    p = provider
    s = expired(p, monkeypatch)
    # Construct the previous signed origin format, which had no provider identity.
    # This is a legacy-format fixture, separate from the ordinary file-loss cases.
    reference = "session-setup-origin:" + digest(s.grant_ref)
    with s.decisions.journal.transaction() as conn:
        if origin_present:
            raw = conn.execute("SELECT data FROM records WHERE ref=?", (reference,)).fetchone()[0]
            origin = json.loads(raw)
            del origin["provider_journal"]
            raw = canonical(origin)
            mac = hmac.new(s.decisions.journal._key, raw.encode(), hashlib.sha256).hexdigest()
            conn.execute("UPDATE records SET data=?,mac=? WHERE ref=?", (raw, mac, reference))
        else:
            conn.execute("DELETE FROM records WHERE ref=?", (reference,))
    before = p.config.read_bytes(), records(s)
    for _ in range(2):
        with pytest.raises(EvidenceRequired, match="reconciliation"):
            ordinary_renew(p, s, monkeypatch)
    assert (p.config.read_bytes(), records(s)) == before


def test_completed_retry_refuses_replaced_provider(provider, monkeypatch):
    p = provider
    s = expired(p, monkeypatch)
    ordinary_renew(p, s, monkeypatch)
    before = p.config.read_bytes(), records(s)
    copy_in_place(p.root / "provider.sqlite")
    with pytest.raises(AccessDenied, match="provider journal"):
        ordinary_renew(p, s, monkeypatch)
    assert (p.config.read_bytes(), records(s)) == before
