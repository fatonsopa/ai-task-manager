"""Two task-scoped installed paths over authored native/provider controls."""

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import time
from pathlib import Path

import pytest

from tests.workflow_installation.test_installation import cli, installed  # noqa: F401
from tests.workflows.test_cold_operator import cold_owner  # noqa: F401


@pytest.mark.parametrize("installed", ["cold-concurrent"], indirect=True)
def test_installed_two_task_public_run_resume_and_reader(installed):  # noqa: F811
    root, _, _ = installed
    tasks = [json.loads((root / name).read_text()) for name in ("prepared.json", "second-prepared.json")]
    requests = [{"task_id": task["task_id"], "plan_ref": task["plan_ref"], "operation_key": "two-task:" + str(i)} for i, task in enumerate(tasks)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        active = [pool.submit(cli, installed, "workflow", "run", requests[0])]
        try:
            limit = time.monotonic() + 12
            while not list((root / "entered").glob("*")) and not active[0].done() and time.monotonic() < limit:
                time.sleep(0.03)
            assert len(list((root / "entered").glob("*"))) == 1, [json.loads(f.result().stdout) for f in active if f.done()]
            active.append(pool.submit(cli, installed, "workflow", "run", requests[1]))
            limit = time.monotonic() + 12
            while time.monotonic() < limit:
                if len(list((root / "entered").glob("*"))) == 2:
                    break
                if any(f.done() for f in active):
                    break
                time.sleep(0.03)
            assert len(list((root / "entered").glob("*"))) == 2, [json.loads(f.result().stdout) for f in active if f.done()]
            assert all(not f.done() for f in active)
            with sqlite3.connect(root / "core.sqlite") as db:
                rows = [json.loads(row[0]) for row in db.execute("SELECT data FROM attempts WHERE active=1")]
            assert len(rows) == 2 and {row["task_id"] for row in rows} == {t["task_id"] for t in tasks}
            for field in ("worker_id", "worktree_id", "database_id"):
                assert len({row[field] for row in rows}) == 2 and all(row[field] for row in rows)
            retries = [pool.submit(cli, installed, "workflow", "run", data) for data in requests]
            for retry in retries:
                refused = retry.result(timeout=5)
                body = json.loads(refused.stdout or refused.stderr)
                assert refused.returncode == 4 and body["error"]["message"] == "Current task request is still owned", body
            assert all(not f.done() for f in active)
            started = time.monotonic()
            for task in tasks:
                shown = cli(installed, "task", "show", {"task_id": task["task_id"]})
                body = json.loads(shown.stdout or shown.stderr)
                assert body["data"]["execution_state"] == "running", body
            for state in ("installed-state", "second-state"):
                with sqlite3.connect(root / state / "workflow.sqlite") as db:
                    reference = db.execute("SELECT id FROM runs").fetchone()[0]
                observed = cli(installed, "workflow", "inspect", {"workflow_ref": reference})
                body = json.loads(observed.stdout or observed.stderr)
                assert observed.returncode == 0 and body["data"]["workflow"] == reference, body
            assert time.monotonic() - started < 8
        finally:
            (root / "release-workers").write_text("release owned fixture barrier\n")
        results = [f.result(timeout=25) for f in active]
    references = []
    for data, result in zip(requests, results, strict=True):
        body = json.loads(result.stdout or result.stderr)
        assert body["ok"] and body["data"]["state"] == "awaiting_review", body
        reference = body["data"]["workflow"]
        references.append(reference)
        replay = cli(installed, "workflow", "run", data)
        assert json.loads(replay.stdout or replay.stderr)["data"] == body["data"]
        resumed = cli(installed, "workflow", "resume", {"workflow_ref": reference})
        assert json.loads(resumed.stdout or resumed.stderr)["data"]["workflow"] == reference
    assert references[0] != references[1]
    assert len(list((root / "installed-state/resources/trees").glob("*/owned.py"))) == 1
    assert len(list((root / "second-state/resources/trees").glob("*/owned.py"))) == 1


@pytest.mark.parametrize("installed", ["cold-concurrent"], indirect=True)
def test_closed_task_membership_refuses_changed_or_shared_inputs(installed):  # noqa: F811
    from ai_task_manager.adapters.workflow_control.identity import file_binding
    from ai_task_manager.config.files import write_private
    from ai_task_manager.domains.work.errors import AccessDenied
    from ai_task_manager.installation.workflows.spec import Installation

    root, _, _ = installed
    original = json.loads((root / "installation.json").read_text())
    child = json.loads((root / "second-installation.json").read_text())
    for change in ("task", "plan", "scope", "deadline", "state", "nested", "role", "binding"):
        member = root / (change + "-member.json")
        outer = root / (change + "-outer.json")
        data = dict(child, installation_path=str(member))
        if change == "task":
            data["task_id"] = original["task_id"]
        elif change == "plan":
            data["plan_ref"] = original["plan_ref"]
        elif change == "scope":
            data["scope"] = dict(data["scope"], principal="foreign")
        elif change == "deadline":
            data["deadline"] -= 1
        elif change == "state":
            data["state_root"] = str(Path(original["state_root"]) / "nested")
        elif change == "nested":
            data["task_installations"] = original["task_installations"]
        elif change == "role":
            data["role"] = "reviewer"
        write_private(member, data)
        pointer = file_binding(member)
        if change == "binding":
            pointer["sha256"] = "0" * 64
        write_private(outer, dict(original, installation_path=str(outer), task_installations=[pointer]))
        with pytest.raises(AccessDenied):
            Installation(outer, root / "service.json").members()
    refused = cli(installed, "workflow", "run", {
        "task_id": child["task_id"], "plan_ref": original["plan_ref"], "operation_key": "foreign-plan"
    })
    assert not json.loads(refused.stdout or refused.stderr)["ok"]
    with sqlite3.connect(root / "core.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


@pytest.mark.parametrize("cold_owner", [("cold-concurrent", {"COLD_TEST_REPLACEMENT_SECONDS": "4"})], indirect=True)
def test_stopped_owner_member_recovery_preserves_other_task(cold_owner):  # noqa: F811
    from ai_task_manager.adapters.workflow_control.lineage import resolve_control
    from ai_task_manager.config.files import read_private
    from tests.workflows.test_review_corrections import wait_closed

    root, owner, call = cold_owner
    (root / "release-workers").write_text("no execution barrier in recovery control\n")
    original = (root / "installation.json").read_bytes()
    tasks = [read_private(root / name) for name in ("prepared.json", "second-prepared.json")]
    references = []
    for i, task in enumerate(tasks):
        _, body = call("run", {"task_id": task["task_id"], "plan_ref": task["plan_ref"],
                               "operation_key": "recovery-member:" + str(i)}, installed=False)
        assert body["ok"] and body["data"]["state"] == "awaiting_review", body
        references.append(body["data"]["workflow"])
    owner.kill()
    owner.wait(timeout=5)
    for i, reference in enumerate(references):
        request = {"action": "inspect", "workflow_ref": reference,
                   "operation_key": "recovery-member:" + str(i)}
        observed, body = call("recover", request)
        assert observed.returncode == 0 and body["data"]["owner_state"] == "dead", body
    bad, body = call("recover", dict(request, operation_key="recovery-member:0"))
    assert bad.returncode == 3 and not body["ok"], body
    resumed, body = call("recover", dict(request, action="resume"))
    assert resumed.returncode == 4 and body["data"]["workflow"] == references[1], body
    control = resolve_control(root / "service.json")
    assert read_private(control["record"])["pid"] != owner.pid
    _, sibling = call("resume", {"workflow_ref": references[0]}, installed=False)
    assert sibling["ok"] and sibling["data"]["workflow"] == references[0], sibling
    wait_closed(control)
    for state in ("installed-state", "second-state"):
        with sqlite3.connect(root / state / "runtime.sqlite") as db:
            assert db.execute("SELECT COUNT(*) FROM runtime_attempts").fetchone()[0] == 1
    assert (root / "installation.json").read_bytes() == original


def test_recovery_core_guard_preserves_unknown_schema(tmp_path):
    from ai_task_manager.adapters.workflow_control.recovery import _existing, physical
    from ai_task_manager.domains.work.errors import AccessDenied
    from tests.domains.helpers import Rig

    path = tmp_path / "core.sqlite"
    Rig(path)
    path.chmod(0o600)
    binding = physical(path)
    before = path.read_bytes()
    with _existing(binding, "core") as db:
        assert db.execute("SELECT version FROM metadata").fetchall()[0][0] == 6
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        db.execute("UPDATE metadata SET version=99")
    unknown = path.read_bytes()
    with pytest.raises(AccessDenied, match="schema is unknown"):
        with _existing(binding, "core"):
            pytest.fail("Unknown Core schema admitted")
    assert path.read_bytes() == unknown
    path.rename(path.with_suffix(".retained"))
    with pytest.raises((OSError, sqlite3.Error, AccessDenied)):
        with _existing(binding, "core"):
            pytest.fail("Missing Core history recreated")
    assert not path.exists()


@pytest.mark.parametrize("cold_owner", [("cold-concurrent-claim", {"COLD_TEST_REPLACEMENT_SECONDS": "4"})], indirect=True)
@pytest.mark.parametrize("foreign", [False, True])
def test_stopped_owner_held_sibling_claim(cold_owner, foreign):  # noqa: F811
    from ai_task_manager.adapters.workflow_control.identity import process_start
    from ai_task_manager.adapters.workflow_control.lineage import resolve_control
    from ai_task_manager.config.files import read_private
    from tests.workflows.test_review_corrections import wait_closed
    import os

    root, owner, call = cold_owner
    tasks = [read_private(root / name) for name in ("prepared.json", "second-prepared.json")]
    requests = [{"task_id": task["task_id"], "plan_ref": task["plan_ref"],
                 "operation_key": "held-member:" + str(i)} for i, task in enumerate(tasks)]
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(call, "run", requests[0], installed=False)
        end = time.monotonic() + 12
        while not (root / "claim-entered").exists() and time.monotonic() < end:
            time.sleep(0.03)
        assert (root / "claim-entered").exists()
        _, second = call("run", requests[1], installed=False)
        assert second["ok"] and second["data"]["state"] == "awaiting_review", second
        owner.kill()
        owner.wait(timeout=5)
        pending.result(timeout=5)
    reference = (root / "claim-entered").read_text()
    path = root / "installed-state/workflow.sqlite"
    with sqlite3.connect(path) as db:
        saved_owner, raw = db.execute("SELECT owner,body FROM runs WHERE id=?", (reference,)).fetchone()
        assert saved_owner is not None
        if foreign:
            body = json.loads(raw)
            body["claim_controller"] = {"pid": os.getpid(), "start": process_start(os.getpid()),
                                        "instance": "foreign-live-controller"}
            db.execute("UPDATE runs SET body=? WHERE id=?", (json.dumps(body), reference))
    _, resumed = call("recover", {"action": "resume", "workflow_ref": second["data"]["workflow"],
                                 "operation_key": requests[1]["operation_key"]})
    assert resumed["ok"] and resumed["data"]["state"] == "awaiting_review", resumed
    control = resolve_control(root / "service.json")
    with sqlite3.connect(path) as db:
        retained = db.execute("SELECT owner FROM runs WHERE id=?", (reference,)).fetchone()[0]
    assert retained == (saved_owner if foreign else None)
    _, sibling = call("resume", {"workflow_ref": reference}, installed=False)
    if foreign:
        assert not sibling["ok"], sibling
    else:
        assert sibling["ok"] and sibling["data"]["workflow"] == reference, sibling
    wait_closed(control)
    for state in ("installed-state", "second-state"):
        with sqlite3.connect(root / state / "runtime.sqlite") as db:
            assert db.execute("SELECT COUNT(*) FROM runtime_attempts").fetchone()[0] == 1
