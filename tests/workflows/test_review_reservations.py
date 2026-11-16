"""Reviewer crash and replacement-retry controls with explicit authored external ports."""

import json
import os
import sqlite3
from pathlib import Path

import pytest

from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.workflow_control.identity import process_start
from ai_task_manager.adapters.workflow_control.lineage import resolve_control
from ai_task_manager.config.files import read_private
from tests.workflows.test_cold_operator import cold_owner as cold_owner
from tests.workflows.test_review_corrections import run_and_interrupt, wait_closed


@pytest.mark.parametrize(
    "cold_owner,fault",
    [
        (("cold-reviewer", {"COLD_TEST_REVIEWER_CRASH": "after_reserve"}), None),
        (("cold-reviewer", {"COLD_TEST_REVIEWER_CRASH": "after_claim"}), None),
        (("cold-reviewer", {"COLD_TEST_REVIEWER_CRASH": "after_reserve"}), "missing_witness"),
        (("cold-reviewer", {"COLD_TEST_REVIEWER_CRASH": "after_claim"}), "ambiguous_launch"),
        (("cold-reviewer", {"COLD_TEST_REVIEWER_CRASH": "after_claim"}), "foreign_controller"),
    ],
    indirect=["cold_owner"],
)
def test_R02_parent_stop_reconciles_actual_reviewer_producer(cold_owner, fault):
    root, owner, call = cold_owner
    workflow = run_and_interrupt(root, owner, call, "reviewer-window", crash=True)
    attempt_id = read_private(root / "crashed.json")["attempt_id"]
    reviewer_task = read_private(root / "reviewer-task.json")["task_id"]
    runtime = root / "reviewer-state/runtime.sqlite"
    host = root / "reviewer-state/host.sqlite"
    with sqlite3.connect(runtime) as conn:
        claim = conn.execute(
            "SELECT data FROM runtime_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM runtime_attempts").fetchone()[0] == int(
            claim is not None
        )
    with sqlite3.connect(host) as conn:
        original = conn.execute(
            "SELECT identity,data,mac FROM host_intents WHERE identity LIKE 'managed-reservation:%'"
        ).fetchone()
        assert original[0].startswith("managed-reservation:review:")
        assert not conn.execute(
            "SELECT 1 FROM host_intents WHERE identity LIKE 'managed-owner:%'"
        ).fetchone()
    with sqlite3.connect(root / "core.sqlite") as conn:
        reservation = next(
            r
            for r in conn.execute("SELECT key,fingerprint,result FROM operations")
            if json.loads(r[2]).get("id") == attempt_id
        )
        before = [json.loads(r[0]) for r in conn.execute("SELECT data FROM attempts")]
        assert len(before) == 2
        assert next(a for a in before if a["id"] == attempt_id)["task_id"] == reviewer_task
    request = {"action": "stop", "workflow_ref": workflow, "operation_key": "reviewer-window"}
    if fault:
        if fault == "missing_witness":
            with sqlite3.connect(host) as conn:
                conn.execute("DELETE FROM host_intents WHERE identity=?", (original[0],))
        elif fault == "ambiguous_launch":
            with sqlite3.connect(runtime) as conn:
                conn.execute(
                    "INSERT INTO runtime_effects VALUES(?,?,?)",
                    (attempt_id, "launch", '{"authored_unknown_launch":true}'),
                )
        else:
            journal = HostJournal.open_existing(host, host.with_suffix(".key"))
            changed = json.loads(original[1])
            changed["controller"] = {"pid": os.getpid(), "start": process_start(os.getpid())}
            raw = json.dumps(changed, sort_keys=True, separators=(",", ":"))
            with sqlite3.connect(host) as conn:
                conn.execute(
                    "UPDATE host_intents SET data=?,mac=? WHERE identity=?",
                    (raw, journal._intent_mac(original[0], raw), original[0]),
                )
        refused, response = call("recover", request)
        assert refused.returncode == 3 and not response["ok"], response
        with sqlite3.connect(root / "core.sqlite") as conn:
            assert (
                conn.execute("SELECT active FROM attempts WHERE id=?", (attempt_id,)).fetchone()[0]
                == 1
            )
            assert not conn.execute(
                "SELECT 1 FROM events WHERE kind='reservation_stopped'"
            ).fetchone()
        # Restore only the authored fault, retaining the original signed evidence.
        if fault == "ambiguous_launch":
            with sqlite3.connect(runtime) as conn:
                conn.execute(
                    "DELETE FROM runtime_effects WHERE attempt_id=? AND kind='launch'",
                    (attempt_id,),
                )
        else:
            with sqlite3.connect(host) as conn:
                conn.execute("DELETE FROM host_intents WHERE identity=?", (original[0],))
                conn.execute("INSERT INTO host_intents VALUES(?,?,?)", original)
    for _ in range(2):
        stopped, response = call("recover", request)
        assert stopped.returncode == 0 and response["data"]["quiescent"], response
    with sqlite3.connect(root / "core.sqlite") as conn:
        after = [json.loads(r[0]) for r in conn.execute("SELECT data FROM attempts")]
        assert len(after) == 2
        assert next(a for a in after if a["id"] == attempt_id)["state"] == "paused"
        assert next(a for a in after if a["id"] != attempt_id) == next(
            a for a in before if a["id"] != attempt_id
        )
        assert (
            conn.execute(
                "SELECT key,fingerprint,result FROM operations WHERE key=?", (reservation[0],)
            ).fetchone()
            == reservation
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM events WHERE kind='reservation_stopped'").fetchone()[
                0
            ]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM resource_leases WHERE attempt_id=?", (attempt_id,)
            ).fetchone()[0]
            == 0
        )
    with sqlite3.connect(runtime) as conn:
        assert (
            conn.execute("SELECT data FROM runtime_attempts WHERE id=?", (attempt_id,)).fetchone()
            == claim
        )
        for table in ("runtime_effects", "runtime_observations", "runtime_receipts"):
            assert conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0
    with sqlite3.connect(host) as conn:
        assert (
            conn.execute(
                "SELECT identity,data,mac FROM host_intents WHERE identity=?", (original[0],)
            ).fetchone()
            == original
        )
        assert not conn.execute(
            "SELECT 1 FROM host_intents WHERE identity LIKE 'managed-owner:%'"
        ).fetchone()
    assert not list((root / "reviewer-state/resources/trees").glob("*"))


@pytest.mark.parametrize(
    "cold_owner",
    [("cold", {"COLD_TEST_CRASH": "before_reserve", "COLD_TEST_REPLACEMENT_SECONDS": "8"})],
    indirect=True,
)
def test_R03_replacement_retries_zero_reservation_without_rewriting_first_owner(cold_owner):
    root, owner, call = cold_owner
    workflow = run_and_interrupt(root, owner, call, "zero-reservation", crash=True)
    original_control = resolve_control(root / "service.json")
    original_descriptor = Path(original_control["record"]).read_bytes()
    original_config = (root / "service.json").read_bytes()
    original_installation = (root / "installation.json").read_bytes()
    with sqlite3.connect(root / "installed-state/host.sqlite") as conn:
        witness = conn.execute(
            "SELECT identity,data,mac FROM host_intents WHERE identity LIKE 'managed-reservation:%'"
        ).fetchone()
    with sqlite3.connect(root / "installed-state/workflow.sqlite") as conn:
        saved_step = conn.execute("SELECT payload FROM steps WHERE name='start'").fetchone()[0]
    with sqlite3.connect(root / "core.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
        assert not conn.execute("SELECT 1 FROM events WHERE kind='attempt_reserved'").fetchone()
    request = {"action": "resume", "workflow_ref": workflow, "operation_key": "zero-reservation"}
    resumed, response = call("recover", request)
    assert resumed.returncode == 4 and response["data"]["state"] == "awaiting_review", response
    replacement = resolve_control(root / "service.json")
    descriptor = read_private(replacement["record"])
    assert descriptor["pid"] != owner.pid
    replay, repeated = call("recover", request)
    assert replay.returncode == 4 and repeated["data"] == response["data"], repeated
    with sqlite3.connect(root / "core.sqlite") as conn:
        attempts = [json.loads(r[0]) for r in conn.execute("SELECT data FROM attempts")]
        assert len(attempts) == 1 and attempts[0]["state"] == "done"
        assert (
            conn.execute("SELECT COUNT(*) FROM events WHERE kind='attempt_reserved'").fetchone()[0]
            == 1
        )
        keys = [r[0] for r in conn.execute("SELECT key FROM operations")]
        assert workflow + ":start:reserve" in keys
    with sqlite3.connect(root / "installed-state/host.sqlite") as conn:
        assert (
            conn.execute(
                "SELECT identity,data,mac FROM host_intents WHERE identity=?", (witness[0],)
            ).fetchone()
            == witness
        )
        owned = json.loads(
            conn.execute(
                "SELECT data FROM host_intents WHERE identity=?",
                ("managed-owner:" + attempts[0]["id"],),
            ).fetchone()[0]
        )
        assert owned["controller"] == {"pid": descriptor["pid"], "start": descriptor["start"]}
        retries = [
            json.loads(r[0])
            for r in conn.execute(
                "SELECT data FROM host_intents WHERE identity LIKE 'managed-reservation-retry:%'"
            )
        ]
        assert len(retries) == 1 and retries[0]["controller"] == owned["controller"]
        assert retries[0]["original"] == witness[0]
    with sqlite3.connect(root / "installed-state/workflow.sqlite") as conn:
        assert (
            conn.execute("SELECT payload FROM steps WHERE name='start'").fetchone()[0] == saved_step
        )
    assert Path(original_control["record"]).read_bytes() == original_descriptor
    assert (root / "service.json").read_bytes() == original_config
    assert (root / "installation.json").read_bytes() == original_installation
    wait_closed(replacement)
