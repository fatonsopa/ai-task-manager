"""Installed R01/R02 controls with authored provider/native ports and actual owners."""

import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.workflow_control.identity import process_start
from ai_task_manager.adapters.workflow_control.lineage import resolve_control
from ai_task_manager.config.files import read_private
from ai_task_manager.installation.workflows.recovery import owner_state
from tests.workflows.test_cold_operator import cold_owner as cold_owner


def original_run(root, call, key):
    ready = read_private(root / "prepared.json")
    return call(
        "run",
        {"task_id": ready["task_id"], "plan_ref": ready["plan_ref"], "operation_key": key},
        installed=False,
    )


def run_and_interrupt(root, owner, call, key, *, crash=False):
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(original_run, root, call, key)
        marker = root / ("crashed.json" if crash else "native-entered")
        until = time.monotonic() + 12
        while not marker.exists() and owner.poll() is None and time.monotonic() < until:
            time.sleep(0.02)
        assert marker.exists(), (root / "owner.log").read_text()
        if not crash:
            owner.kill()
        owner.wait(timeout=5)
        pending.result(timeout=5)
    with sqlite3.connect(root / "installed-state/workflow.sqlite") as conn:
        return conn.execute("SELECT id FROM runs WHERE operation_key=?", (key,)).fetchone()[0]


def wait_closed(control):
    descriptor = read_private(control["record"])
    until = time.monotonic() + 8
    while time.monotonic() < until:
        if owner_state(
            descriptor, {"owner": {"pid": descriptor["pid"], "start": descriptor["start"]}}
        ) in {"dead", "reused"}:
            return descriptor
        time.sleep(0.04)
    pytest.fail("The owned replacement did not close through its finite lifecycle callback")


@pytest.mark.parametrize(
    "cold_owner", [("cold-lineage", {"COLD_TEST_REPLACEMENT_SECONDS": "2"})], indirect=True
)
@pytest.mark.parametrize("foreign", [None, "author", "reviewer"])
def test_R01_retained_author_and_reviewer_survive_repeated_replacement(cold_owner, foreign):
    root, owner, call = cold_owner
    config_bytes = (root / "service.json").read_bytes()
    installation_bytes = (root / "installation.json").read_bytes()
    a = resolve_control(root / "service.json")
    a_record = Path(a["record"]).read_bytes()
    workflow = run_and_interrupt(root, owner, call, "retained")
    peer = read_private(root / "reviewer-ready.json")
    journals = {
        "author": root / "installed-state/host.sqlite",
        "reviewer": root / "reviewer-state/host.sqlite",
    }
    original = {}
    for role, path in journals.items():
        with sqlite3.connect(path) as conn:
            original[role] = conn.execute(
                "SELECT identity,data,mac FROM host_intents WHERE identity LIKE 'managed-owner:%'"
            ).fetchone()
    owners = [a]
    request = {"action": "resume", "workflow_ref": workflow, "operation_key": "retained"}
    for _ in range(2):
        result, response = call("recover", request)
        assert response["ok"] and result.returncode in {4, 6}, response
        current = resolve_control(root / "service.json")
        assert current["instance"] not in {c["instance"] for c in owners}
        wait_closed(current)
        owners.append(current)
    assert len({read_private(c["record"])["pid"] for c in owners}) == 3
    target = journals.get(foreign)
    if target:
        host = HostJournal.open_existing(target, target.with_suffix(".key"))
        identity, raw, _ = original[foreign]
        changed = json.loads(raw)
        changed["controller"] = {"pid": os.getpid(), "start": process_start(os.getpid())}
        encoded = json.dumps(changed, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with sqlite3.connect(target) as conn:
            conn.execute(
                "UPDATE host_intents SET data=?,mac=? WHERE identity=?",
                (encoded, host._intent_mac(identity, encoded), identity),
            )
        refused, response = call("recover", dict(request, action="stop"))
        assert refused.returncode == 3 and not response["ok"], response
        with sqlite3.connect(root / "core.sqlite") as conn:
            assert conn.execute("SELECT COUNT(*) FROM attempts WHERE active=1").fetchone()[0] == 2
        with sqlite3.connect(target) as conn:
            conn.execute(
                "UPDATE host_intents SET data=?,mac=? WHERE identity=?",
                (original[foreign][1], original[foreign][2], original[foreign][0]),
            )
    stopped, response = call("recover", dict(request, action="stop"))
    assert stopped.returncode == 0 and response["data"]["state"] == "paused", response
    assert response["data"]["quiescent"] is True
    with sqlite3.connect(root / "core.sqlite") as conn:
        records = [json.loads(r[0]) for r in conn.execute("SELECT data FROM attempts")]
        assert len(records) == 2 and all(r["state"] == "paused" for r in records)
        assert peer["attempt_id"] in {r["id"] for r in records}
        assert conn.execute("SELECT COUNT(*) FROM resource_leases").fetchone()[0] == 0
    for role, path in journals.items():
        with sqlite3.connect(path) as conn:
            assert (
                conn.execute(
                    "SELECT identity,data,mac FROM host_intents WHERE identity=?",
                    (original[role][0],),
                ).fetchone()
                == original[role]
            )
    assert Path(a["record"]).read_bytes() == a_record
    assert (root / "service.json").read_bytes() == config_bytes
    assert (root / "installation.json").read_bytes() == installation_bytes


@pytest.mark.parametrize(
    "cold_owner",
    [
        ("cold", {"COLD_TEST_CRASH": "after_reserve"}),
        ("cold", {"COLD_TEST_CRASH": "after_claim"}),
    ],
    indirect=True,
)
def test_R02_prelaunch_reservation_settles_without_runtime_receipts(cold_owner):
    root, owner, call = cold_owner
    workflow = run_and_interrupt(root, owner, call, "prelaunch", crash=True)
    attempt_id = read_private(root / "crashed.json")["attempt_id"]
    runtime = root / "installed-state/runtime.sqlite"
    with sqlite3.connect(runtime) as conn:
        claim = conn.execute(
            "SELECT data FROM runtime_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
    with sqlite3.connect(root / "core.sqlite") as conn:
        reservation = next(
            tuple(r)
            for r in conn.execute("SELECT key,fingerprint,result FROM operations")
            if json.loads(r[2]).get("id") == attempt_id
        )
    with sqlite3.connect(root / "installed-state/host.sqlite") as conn:
        intent = conn.execute(
            "SELECT identity,data,mac FROM host_intents WHERE identity LIKE 'managed-reservation:%'"
        ).fetchone()
    request = {"action": "stop", "workflow_ref": workflow, "operation_key": "prelaunch"}
    for _ in range(2):
        stopped, response = call("recover", request)
        assert stopped.returncode == 0 and response["data"]["state"] == "paused", response
        assert response["data"]["components_started"] is False
        assert response["data"]["runtime_registered"] is (claim is not None)
    with sqlite3.connect(runtime) as conn:
        assert (
            conn.execute("SELECT data FROM runtime_attempts WHERE id=?", (attempt_id,)).fetchone()
            == claim
        )
        for table in ("runtime_effects", "runtime_observations", "runtime_receipts"):
            assert conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0
    with sqlite3.connect(root / "core.sqlite") as conn:
        attempt = json.loads(conn.execute("SELECT data FROM attempts").fetchone()[0])
        assert attempt["id"] == attempt_id and attempt["state"] == "paused"
        assert (
            attempt["worker_id"] is None
            and attempt["worktree_id"] is None
            and attempt["database_id"] is None
        )
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM events WHERE kind='reservation_stopped'").fetchone()[
                0
            ]
            == 1
        )
        assert (
            conn.execute(
                "SELECT key,fingerprint,result FROM operations WHERE key=?", (reservation[0],)
            ).fetchone()
            == reservation
        )
    with sqlite3.connect(root / "installed-state/host.sqlite") as conn:
        assert (
            conn.execute(
                "SELECT identity,data,mac FROM host_intents WHERE identity=?", (intent[0],)
            ).fetchone()
            == intent
        )
        assert not conn.execute(
            "SELECT 1 FROM host_intents WHERE identity LIKE 'managed-owner:%'"
        ).fetchone()
    assert not list((root / "installed-state/resources/trees").glob("*"))


@pytest.mark.parametrize(
    "cold_owner,fault",
    [
        (("cold", {"COLD_TEST_CRASH": "after_claim"}), "ambiguous_launch"),
        (("cold", {"COLD_TEST_CRASH": "after_reserve"}), "missing_witness"),
    ],
    indirect=["cold_owner"],
)
def test_R02_ambiguous_launch_and_missing_forward_witness_stay_protected(cold_owner, fault):
    root, owner, call = cold_owner
    workflow = run_and_interrupt(root, owner, call, "protected-reservation", crash=True)
    attempt_id = read_private(root / "crashed.json")["attempt_id"]
    runtime = root / "installed-state/runtime.sqlite"
    host = root / "installed-state/host.sqlite"
    original = None
    if fault == "ambiguous_launch":
        with sqlite3.connect(runtime) as conn:
            conn.execute(
                "INSERT INTO runtime_effects VALUES(?,?,?)",
                (attempt_id, "launch", '{"authored_unknown_launch":true}'),
            )
    else:
        with sqlite3.connect(host) as conn:
            original = conn.execute(
                "SELECT identity,data,mac FROM host_intents "
                "WHERE identity LIKE 'managed-reservation:%'"
            ).fetchone()
            conn.execute("DELETE FROM host_intents WHERE identity=?", (original[0],))
    request = {"action": "stop", "workflow_ref": workflow, "operation_key": "protected-reservation"}
    refused, response = call("recover", request)
    assert refused.returncode == 3 and response["ok"] is False, response
    with sqlite3.connect(root / "core.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts WHERE active=1").fetchone()[0] == 1
        assert not conn.execute("SELECT 1 FROM events WHERE kind='reservation_stopped'").fetchone()
    with sqlite3.connect(runtime) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runtime_receipts").fetchone()[0] == 0
    # Restore only the exact test fault. Recovery itself must not invent this witness.
    if fault == "ambiguous_launch":
        with sqlite3.connect(runtime) as conn:
            conn.execute(
                "DELETE FROM runtime_effects WHERE attempt_id=? AND kind='launch'", (attempt_id,)
            )
    else:
        with sqlite3.connect(host) as conn:
            assert not conn.execute(
                "SELECT 1 FROM host_intents WHERE identity=?", (original[0],)
            ).fetchone()
            conn.execute("INSERT INTO host_intents VALUES(?,?,?)", original)
    stopped, response = call("recover", request)
    assert stopped.returncode == 0 and response["data"]["components_started"] is False, response
