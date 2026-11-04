"""Capacity configuration cannot survive a refused enrollment change."""

import json
import os
import sqlite3
from pathlib import Path

import pytest

from ai_task_manager.config.files import read_private, write_private
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.installation.workflows.bootstrap import prepare
from tests.complete_cycle.test_bootstrap import boot  # noqa: F401


def rows(config):
    with sqlite3.connect(read_private(config)["core_database"]) as conn:
        return conn.execute(
            "SELECT host,host_limit,project_limit FROM capacity ORDER BY host"
        ).fetchall()


def test_CA01_refused_configuration_drift_cannot_insert_capacity(boot):  # noqa: F811
    demo, _ = boot
    before = rows(demo.config)
    original = read_private(demo.config)
    changed = {
        **original,
        "capacity_policies": original["capacity_policies"]
        + [{"host": "codex", "host_limit": 1, "project_limit": 2}],
    }
    write_private(demo.config, changed, replace=True)
    with pytest.raises((AccessDenied, EvidenceRequired)):
        prepare(demo.config, {"operation": "task_list", "data": {}})
    after = rows(demo.config)
    if os.environ.get("CAPACITY_AUTHORITY_EVIDENCE"):
        Path(os.environ["CAPACITY_AUTHORITY_EVIDENCE"]).write_text(
            json.dumps({"before": before, "after": after, "unchanged": before == after}, indent=2)
        )
    assert after == before


def _add_host(demo):
    original = read_private(demo.config)
    changed = {
        **original,
        "capacity_policies": original.get("capacity_policies", [])
        + [{"host": "codex", "host_limit": 1, "project_limit": 2}],
    }
    write_private(demo.config, changed, replace=True)


def test_CA02_ordinary_load_does_not_install_drift(boot):  # noqa: F811
    from ai_task_manager.config.service import load_service

    demo, _ = boot
    before = rows(demo.config)
    _add_host(demo)
    with pytest.raises(EvidenceRequired):
        load_service(demo.config)
    assert rows(demo.config) == before


def test_CA03_policy_free_enrollment_stays_policy_free(tmp_path, monkeypatch):
    from ai_task_manager.adapters.linear.transport import HttpTransport
    from ai_task_manager.config.service import load_service
    from tests.module_core.fixture import Demonstration

    root = tmp_path / "scenario"
    root.mkdir(mode=0o700)
    db = tmp_path / "core.sqlite"
    db.touch(mode=0o600)
    demo = Demonstration(root, db, Path("/Users/faton/Documents/ChatGPT/technical-writer"))
    try:
        monkeypatch.setattr(HttpTransport, "ENDPOINT", demo.endpoint)
        assert rows(demo.config) == []
        _add_host(demo)
        with pytest.raises(EvidenceRequired):
            load_service(demo.config)
        assert rows(demo.config) == []
    finally:
        demo.close()


@pytest.mark.parametrize("state", ["revoked", "expired"])
def test_CA04_lost_authority_preserves_capacity(boot, monkeypatch, state):  # noqa: F811
    import time

    from ai_task_manager.adapters.decisions.journal import AuthorityJournal
    from ai_task_manager.config.service import load_service

    demo, _ = boot
    before = rows(demo.config)
    if state == "revoked":
        load_service(demo.config).decisions.observations.revoke()
    else:
        original = AuthorityJournal.__init__
        future = time.time() + 3600

        def expired_clock(self, *args, **kwargs):
            return original(self, *args, **{**kwargs, "clock": lambda: future})

        monkeypatch.setattr(AuthorityJournal, "__init__", expired_clock)
    with pytest.raises(AccessDenied):
        prepare(demo.config, {"operation": "task_list", "data": {}})
    assert rows(demo.config) == before
    _add_host(demo)
    with pytest.raises((AccessDenied, EvidenceRequired)):
        prepare(demo.config, {"operation": "task_list", "data": {}})
    assert rows(demo.config) == before


def _grant(demo):
    from ai_task_manager.config.service import load_service
    from ai_task_manager.installation.sessions.recovery import history

    config = read_private(demo.config)
    service = load_service(demo.config)
    return history(service.decisions.journal, config["grant_ref"])


@pytest.mark.parametrize("renew", [False, True])
def test_CA05_trusted_replay_or_renewal_drift_cannot_install(boot, monkeypatch, renew):  # noqa: F811
    import importlib

    from ai_task_manager.domains.work.errors import IdempotencyConflict
    from ai_task_manager.installation.sessions import setup

    demo, _ = boot
    grant = _grant(demo)
    before = rows(demo.config)
    config = read_private(demo.config)
    _add_host(demo)
    deadline = grant["expires_at"]
    if renew:
        module = importlib.import_module("ai_task_manager.installation.sessions.setup")
        monkeypatch.setattr(module.time, "time", lambda: grant["expires_at"] + 1)
        deadline += 600
    with pytest.raises((AccessDenied, EvidenceRequired, IdempotencyConflict)):
        setup(
            demo.config,
            logical_session=grant["scope"]["logical_session"],
            actions=tuple(grant["actions"]),
            deadline=deadline,
            operation_key="capacity-renewal" if renew else "module-core-setup",
            components=demo.components,
            renew_from=config["grant_ref"] if renew else None,
        )
    assert rows(demo.config) == before


def test_CA06_valid_original_replay_and_reload_preserve_policy(boot):  # noqa: F811
    from ai_task_manager.config.service import load_service
    from ai_task_manager.installation.sessions import setup

    demo, _ = boot
    grant = _grant(demo)
    before = rows(demo.config)
    result = setup(
        demo.config,
        logical_session=grant["scope"]["logical_session"],
        actions=tuple(grant["actions"]),
        deadline=grant["expires_at"],
        operation_key="module-core-setup",
        components=demo.components,
    )
    assert result == demo.setup
    service = load_service(demo.config)
    service.decisions.observations.verify_enrollment(service.grant_ref)
    assert rows(demo.config) == before == [("claude", 2, 2)]
