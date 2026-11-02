"""Real disposable migration/crash/old-writer boundaries with retained failures."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.test_build_control_policy_amendment import ROOT, raw, refused
from tests.test_build_control_policy_amendment import rig as rig
from tools.build_support import storage

LEGACY = ROOT / "tests/fixtures/build_control/legacy_v1"


def child_environment(legacy=False):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(map(str, [LEGACY if legacy else ROOT, ROOT / "src"]))
    return env


def wait_owned(path, process):
    deadline = time.monotonic() + 6
    while not path.exists():
        if process.poll() is not None or time.monotonic() >= deadline:
            stdout, stderr = process.communicate(timeout=2)
            pytest.fail(f"fixture did not reach barrier: {process.returncode} {stdout} {stderr}")
        time.sleep(0.01)


def authority_file(rig):
    target = rig.root / "authority.json"
    target.write_text(json.dumps({"format": 1, "bindings": rig.bindings}))
    return target, hashlib.sha256(target.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "stage",
    [
        "before_ddl",
        "policy_revision",
        "amendment_receipt",
        "batch",
        "invocation_bound",
        "admission_state",
        "operation_receipt",
        "baseline_policy",
        "schema_version",
        "new_policy",
        "event",
        "receipt",
    ],
)
def test_lp10_11_each_migration_write_rolls_back(rig, monkeypatch, stage):
    request = rig.prepare()
    before = raw(rig.base.ledger)
    original_connect = sqlite3.connect
    fired = []

    class FaultConnection(sqlite3.Connection):
        def execute(self, statement, parameters=()):
            matches = stage == "before_ddl" and statement.startswith("CREATE TABLE policy_revision")
            if matches and not fired:
                fired.append(True)
                raise OSError("authored failure before DDL")
            result = super().execute(statement, parameters)
            matches = (
                statement.startswith("CREATE TABLE " + stage + "(")
                or stage == "baseline_policy"
                and statement.startswith("INSERT INTO policy_revision")
                and parameters[0] == 0
                or stage == "new_policy"
                and statement.startswith("INSERT INTO policy_revision")
                and parameters[0] == 1
                or stage == "schema_version"
                and statement == "PRAGMA user_version=2"
                or stage == "event"
                and statement.startswith("INSERT INTO event")
                or stage == "receipt"
                and statement.startswith("INSERT INTO amendment_receipt")
            )
            if matches and not fired:
                fired.append(True)
                raise OSError("authored failure after migration write")
            return result

    def connect(*args, **kwargs):
        return original_connect(*args, **kwargs, factory=FaultConnection)

    monkeypatch.setattr(storage.sqlite3, "connect", connect)
    with pytest.raises(OSError):
        rig.control().amend_policy(**request)
    monkeypatch.undo()
    assert fired and raw(rig.base.ledger) == before
    rig.amend(request)
    assert rig.current.snapshot()["schema_version"] == 2


@pytest.mark.parametrize("stage", ["before_commit", "after_commit"])
def test_lp12_actual_process_crash_and_recovery(rig, stage):
    request = rig.prepare()
    config, sha = authority_file(rig)
    program = """
import json,os,sys
from contextlib import contextmanager
from tools.build_control import BuildControl
from tools.build_support.authorization import PinnedRecords
from tools.build_support.storage import LedgerStore,LedgerTransaction
if sys.argv[5] == 'before_commit':
    original=LedgerTransaction.upgrade_policy_schema
    def crash(self):
        original(self)
        os._exit(71)
    LedgerTransaction.upgrade_policy_schema=crash
else:
    original=LedgerStore.transaction
    @contextmanager
    def crash(self,**kwargs):
        with original(self,**kwargs) as tx:
            yield tx
        os._exit(72)
    LedgerStore.transaction=crash
control=BuildControl(sys.argv[1],records=PinnedRecords.from_config(sys.argv[2],sys.argv[3]))
control.amend_policy(**json.loads(sys.argv[4]))
"""
    before = raw(rig.base.ledger)
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            str(rig.base.ledger),
            str(config),
            sha,
            json.dumps(request),
            stage,
        ],
        cwd=ROOT,
        env=child_environment(),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert child.returncode == (71 if stage == "before_commit" else 72), child.stderr
    if stage == "before_commit":
        assert raw(rig.base.ledger) == before
    else:
        assert raw(rig.base.ledger)["version"] == 2
    receipt = rig.amend(request)
    assert rig.control().amend_policy(**request) == receipt
    assert len([e for e in rig.current.snapshot()["events"] if e["kind"] == "policy_amended"]) == 1


@pytest.mark.parametrize(
    "method,args,kwargs",
    [
        (
            "initialize",
            [],
            {
                "run_id": "policy-fixture",
                "started_at": "2026-11-01T12:00:00+00:00",
                "worker_cap": 10,
            },
        ),
        ("reserve", ["old"], {"label": "old", "paid": False, "max_cost_usd": None}),
        ("claim_launch", ["reserved"], {"worker_identity": "old"}),
        ("mark_started", ["reserved"], {"worker_identity": "old"}),
        (
            "record_outcome",
            ["reserved"],
            {"outcome": "failed", "process_stopped": True, "evidence_ref": "old"},
        ),
        ("settle_cost", ["reserved"], {"actual_cost_usd": "0", "evidence_ref": "old"}),
        ("cancel_unstarted", ["reserved"], {"confirmed_not_started": True, "evidence_ref": "old"}),
        ("set_worker_cap", [12], {"evidence_ref": "old"}),
    ],
)
def test_lp16_byte_pinned_old_writer_refuses_every_mutation(rig, method, args, kwargs):
    rig.base.reserve("reserved", label="retained", paid=False, max_cost_usd=None)
    rig.amend()
    before = raw(rig.base.ledger)
    program = """
import json,sys
import tools.build_control as module
from tools.build_control import BuildControl,ControlError
assert 'legacy_v1' in module.__file__
try:
 getattr(BuildControl(sys.argv[1]),sys.argv[2])(*json.loads(sys.argv[3]),**json.loads(sys.argv[4]))
except ControlError as error:
 print(error.code)
 raise SystemExit(23)
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            str(rig.base.ledger),
            method,
            json.dumps(args),
            json.dumps(kwargs),
        ],
        cwd=LEGACY,
        env=child_environment(True),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert child.returncode == 23 and child.stdout.strip() == "invalid_ledger", child.stderr
    assert raw(rig.base.ledger) == before


def test_lp17_old_transaction_and_amendment_serialize(rig):
    request = rig.prepare()
    ready, release = rig.root / "old-ready", rig.root / "release"
    program = """
import sys,time
from pathlib import Path
from tools.build_control import BuildControl
control=BuildControl(sys.argv[1])
with control._store.transaction() as tx:
 tx.run()
 Path(sys.argv[2]).write_text('locked')
 end=time.monotonic()+6
 while not Path(sys.argv[3]).exists():
  if time.monotonic()>end: raise SystemExit(74)
  time.sleep(.01)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", program, str(rig.base.ledger), str(ready), str(release)],
        cwd=LEGACY,
        env=child_environment(True),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_owned(ready, child)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(rig.control().amend_policy, **request)
            release.write_text("release owned transaction")
            receipt = future.result(timeout=10)
        assert receipt["policy_revision"] == 1
        assert child.wait(timeout=3) == 0
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=3)


def test_lp55_old_claim_before_spawn_blocks_migration(rig):
    ready, release = rig.root / "claimed", rig.root / "release"
    program = """
import sys,time
from pathlib import Path
from datetime import datetime,UTC
from tools.build_control import BuildControl,run_command
from tools.build_support import process as execution
original=execution.subprocess.Popen
command=[sys.executable,'-c',"print('legacy fixture')"]
def paused(argv,*args,**kwargs):
 if argv == command:
  Path(sys.argv[2]).write_text('claim committed')
  end=time.monotonic()+6
  while not Path(sys.argv[3]).exists():
   if time.monotonic()>end: raise RuntimeError('owned barrier expired')
   time.sleep(.01)
 return original(argv,*args,**kwargs)
execution.subprocess.Popen=paused
control=BuildControl(sys.argv[1],clock=lambda:datetime(2026,11,1,12,tzinfo=UTC))
run_command(control,call_id='legacy-launch',command=command,paid=False,max_cost_usd=None,
 timeout_seconds=2,output_directory=sys.argv[4])
"""
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            program,
            str(rig.base.ledger),
            str(ready),
            str(release),
            str(rig.root / "old-traces"),
        ],
        cwd=LEGACY,
        env=child_environment(True),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_owned(ready, child)
        # Prepare the actual claimed state, so the launch-specific guard rather
        # than stale-state rejection is what this variant must prove.
        claimed_request = rig.prepare()
        before = raw(rig.base.ledger)
        refused("launch_unresolved", rig.control().amend_policy, **claimed_request)
        assert raw(rig.base.ledger) == before
        release.write_text("allow only this owned harmless fixture")
        stdout, stderr = child.communicate(timeout=8)
        assert child.returncode == 0, stderr
        assert rig.base.snapshot()["calls"][0]["status"] == "succeeded"
        rig.amend(rig.prepare())
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=3)


def test_lp42_backup_is_private_and_exact(rig):
    request = rig.prepare()
    prepared = rig.bindings[request["preparation_ref"]]
    data = json.loads(Path(prepared["path"]).read_text())
    path = Path(data["backup"]["path"])
    assert path.stat().st_mode & 0o777 == 0o600
    assert hashlib.sha256(path.read_bytes()).hexdigest() == data["backup"]["sha256"]
    assert raw(path) == raw(rig.base.ledger)
    path.write_bytes(b"changed backup")
    before = raw(rig.base.ledger)
    refused("backup_conflict", rig.control().amend_policy, **request)
    assert raw(rig.base.ledger) == before


def test_lp43_retained_legacy_fixture_bytes_match_manifest():
    manifest = json.loads((LEGACY / "source-manifest.json").read_text())
    for row in manifest["files"]:
        assert hashlib.sha256((LEGACY / row["path"]).read_bytes()).hexdigest() == row["sha256"]


def planning_baseline():
    return json.loads(
        (ROOT / "tests/fixtures/build_control/planning-before-authorization.json").read_text()
    )


def test_lp44_requirements_preserve_ids_history_and_other_rules():
    current = json.loads((ROOT / "docs/config/requirements.json").read_text())
    old = planning_baseline()
    before = {r["id"]: r["text"] for r in old["requirements"]}
    after = {r["id"]: r["text"] for r in current["requirements"]}
    assert len(before) == len(after) == 38 and before.keys() == after.keys()
    assert {key for key in before if before[key] != after[key]} == {"R20", "R22"}
    revision = next(row for row in current["revision_history"] if row["revision"] == 4)
    assert {r["id"]: r["text"] for r in revision["previous_requirements"]} == {
        key: before[key] for key in ["R20", "R22"]
    }
    assert revision["prior_results_reinterpreted"] is False


def test_lp45_case_ids_and_previous_oracles_remain_retained():
    current = json.loads((ROOT / "docs/config/acceptance-cases.json").read_text())
    old = planning_baseline()
    assert [c["id"] for c in current["cases"]] == old["case_ids"]
    assert len(current["cases"]) == 248
    assert [w["id"] for w in current["workflows"]] == old["workflows"]
    assert len(current["workflows"]) == 14
    history = next(row for row in current["case_revision_history"] if row["revision"] == 4)
    assert history["previous_cases"] == old["prior_cases"]
    assert history["previous_matrix"] == old["prior_matrix"]
    assert history["prior_results_reinterpreted"] is False
    assert all(c["execution_status"] == "not_run" for c in current["cases"])
    assert (
        current["matrix"]["required_profiles_source"]
        == "execution-policy.json#/acceptance/required_profiles"
    )


def test_lp46_selected_profiles_remain_distinct_unrun_cells():
    policy = json.loads((ROOT / "docs/config/execution-policy.json").read_text())
    profiles = policy["acceptance"]["required_profiles"]
    assert {(p["host"], p["model"], p["reasoning_effort"]) for p in profiles} == {
        ("claude-code", "claude-opus-5-5", "high"),
        ("claude-code", "claude-sonnet-5-5", "high"),
        ("codex-cli", "gpt-6.1-sol", "high"),
        ("codex-cli", "gpt-6-astra", "high"),
    }
    assert len(profiles) == 4
    assert all(p["execution_status"] == "not_run" for p in profiles)


def handoff_checker():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "isolated_handoff_checker", ROOT / "docs/tools/check_handoff.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "variant",
    [
        "valid",
        "old_time_cap",
        "old_money_cap",
        "sentinel",
        "missing_null",
        "missing_authority",
        "missing_timeout",
        "missing_batch",
        "one_model",
        "fable",
        "wrong_effort",
        "claimed_pass",
    ],
)
def test_lp47_integrity_guard_rejects_stale_or_incomplete_policy(variant):
    policy = json.loads((ROOT / "docs/config/execution-policy.json").read_text())
    if variant == "old_time_cap":
        policy["limits"]["max_elapsed_seconds"] = 172800
    elif variant == "old_money_cap":
        policy["limits"]["max_incremental_spend_usd"] = 100
    elif variant == "sentinel":
        policy["limits"]["max_elapsed_seconds"] = 10**30
    elif variant == "missing_null":
        policy["limits"].pop("max_total_retry_rounds")
    elif variant == "missing_authority":
        policy.pop("limits_authorization")
    elif variant == "missing_timeout":
        policy["limits"].pop("per_invocation_timeout_required")
    elif variant == "missing_batch":
        policy["limits"]["reviewed_finite_batch_required"] = False
    elif variant == "one_model":
        policy["acceptance"]["required_profiles"].pop()
    elif variant == "fable":
        policy["acceptance"]["required_profiles"][0]["model"] = "Fable"
    elif variant == "wrong_effort":
        policy["acceptance"]["required_profiles"][0]["reasoning_effort"] = "low"
    elif variant == "claimed_pass":
        policy["acceptance"]["required_profiles"][0]["execution_status"] = "passed"
    failures = []
    handoff_checker().verify_execution_limits(
        policy, lambda value, reason: failures.append(reason) if not value else None
    )
    assert bool(failures) == (variant != "valid")


def test_lp48_narrow_edits_preserve_document_structure_and_commands():
    import re

    for name, expected in planning_baseline()["documentation_invariants"].items():
        text = (ROOT / "docs" / name).read_text()
        assert re.findall(r"^#{1,6} .+$", text, re.M) == expected["headings"]
        assert [
            hashlib.sha256(b.encode()).hexdigest() for b in re.findall(r"```.*?```", text, re.S)
        ] == expected["code_block_sha256"]
    # TST-85 branch integration is a separate lead gate; this test does not
    # claim inspection/preservation of additions not yet integrated here.
