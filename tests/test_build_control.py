import hashlib
import json
import multiprocessing
import sqlite3
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tools.build_control import BuildControl, ControlError, run_command

START = datetime(2026, 11, 1, 12, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.value = START

    def __call__(self):
        return self.value


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def control(tmp_path, clock):
    result = BuildControl(tmp_path / "ledger.sqlite", clock=clock)
    result.initialize(run_id="test-build", started_at=START)
    return result


def reserve_paid(control, call_id="call", cost="10", **kwargs):
    return control.reserve(call_id, label="test call", paid=True, max_cost_usd=cost, **kwargs)


@contextmanager
def expect_error(code):
    with pytest.raises(ControlError) as caught:
        yield caught
    assert caught.value.code == code


@pytest.mark.parametrize(
    "changes",
    [
        {"run_id": "another-run"},
        {"started_at": START + timedelta(seconds=1)},
        {"budget_usd": "99"},
        {"worker_cap": 3},
        {"duration_seconds": 170_000},
        {"admission_buffer_seconds": 100},
    ],
)
def test_reopening_cannot_reset_start_deadline_or_limits(control, clock, changes):
    clock.value += timedelta(hours=10)
    reopened = BuildControl(control.ledger, clock=clock)
    before = reopened.snapshot()
    configuration = {"run_id": "test-build", "started_at": START} | changes
    with expect_error("run_conflict"):
        reopened.initialize(**configuration)
    assert reopened.snapshot()["deadline"] == before["deadline"]
    assert reopened.snapshot()["started_at"] == START.isoformat()


def test_idempotent_initialization_preserves_reservations_after_restart(control, clock):
    reserve_paid(control)
    clock.value += timedelta(hours=2)
    reopened = BuildControl(control.ledger, clock=clock)
    snapshot = reopened.initialize(run_id="test-build", started_at=START)
    assert snapshot["reserved_usd"] == "10.000000"
    assert snapshot["active_workers"] == 1
    assert len([e for e in snapshot["events"] if e["kind"] == "run_initialized"]) == 1


@pytest.mark.parametrize("amount", [0, -1, 101, "NaN", "Infinity", "0.0000001", False])
def test_invalid_initial_budget_is_refused(tmp_path, clock, amount):
    control = BuildControl(tmp_path / "ledger.sqlite", clock=clock)
    with expect_error("invalid_input"):
        control.initialize(run_id="test", started_at=START, budget_usd=amount)
    assert not control.ledger.exists()


@pytest.mark.parametrize("cap", [0, -1, True, 1.5, float("inf")])
def test_worker_cap_is_a_finite_positive_integer(tmp_path, clock, cap):
    with expect_error("invalid_input"):
        BuildControl(tmp_path / "ledger.sqlite", clock=clock).initialize(
            run_id="test", started_at=START, worker_cap=cap
        )


@pytest.mark.parametrize(
    "arguments",
    [
        {"duration_seconds": 172801},
        {"duration_seconds": 0},
        {"admission_buffer_seconds": 172800},
        {"admission_buffer_seconds": -1},
        {"started_at": datetime(2026, 11, 1)},
    ],
)
def test_invalid_time_limits_are_refused(tmp_path, clock, arguments):
    with expect_error("invalid_input"):
        BuildControl(tmp_path / "ledger.sqlite", clock=clock).initialize(
            **({"run_id": "test", "started_at": START} | arguments)
        )


def test_unrelated_database_is_not_adopted(tmp_path, clock):
    path = tmp_path / "fixture.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE owner(task_id TEXT)")
        connection.execute("INSERT INTO owner VALUES ('original')")
    with expect_error("invalid_ledger"):
        BuildControl(path, clock=clock).initialize(run_id="test", started_at=START)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT * FROM owner").fetchall() == [("original",)]
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name='run'").fetchall()


def test_future_schema_is_not_changed(control):
    with sqlite3.connect(control.ledger) as connection:
        connection.execute("PRAGMA user_version=2")
    with expect_error("invalid_ledger"):
        control.snapshot()
    with sqlite3.connect(control.ledger) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_unknown_cost_and_invalid_unpaid_cost_are_refused(control):
    with expect_error("unknown_cost"):
        reserve_paid(control, cost=None)
    with expect_error("invalid_input"):
        control.reserve("free", label="bad", paid=False, max_cost_usd="1")
    assert control.snapshot()["calls"] == []


def test_reservation_is_idempotent_but_conflicting_inputs_are_refused(control):
    first = reserve_paid(control)
    assert reserve_paid(control) == first
    with expect_error("call_conflict"):
        reserve_paid(control, cost="11")
    assert control.snapshot()["reserved_usd"] == "10.000000"
    assert control.snapshot()["active_workers"] == 1


def test_admission_stops_at_buffer_boundary_and_clock_cannot_predate_run(control, clock):
    clock.value = START - timedelta(seconds=1)
    with expect_error("clock_conflict"):
        reserve_paid(control)
    clock.value = START + timedelta(hours=48) - timedelta(minutes=30)
    with expect_error("deadline_reached"):
        reserve_paid(control)
    assert not control.snapshot()["calls"]


def test_old_reservation_cannot_launch_after_hard_deadline(control, clock):
    reserve_paid(control)
    clock.value = START + timedelta(hours=48)
    with expect_error("deadline_reached"):
        control.claim_launch("call", worker_identity="worker")
    assert control.snapshot()["reserved_usd"] == "10.000000"


def test_launch_claim_is_atomic_across_different_callers(control):
    reserve_paid(control)

    def claim(identity):
        try:
            BuildControl(control.ledger, clock=lambda: START).claim_launch(
                "call", worker_identity=identity
            )
            return "claimed"
        except ControlError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(claim, [f"worker-{n}" for n in range(8)]))
    assert outcomes.count("claimed") == 1
    assert outcomes.count("already_executed") == 7
    assert control.snapshot()["active_workers"] == 1
    assert control.snapshot()["reserved_usd"] == "10.000000"


def test_start_requires_matching_launch_identity_and_pid_start_identity(control):
    reserve_paid(control)
    control.claim_launch("call", worker_identity="one")
    with expect_error("call_conflict"):
        control.mark_started("call", worker_identity="two")
    with expect_error("invalid_input"):
        control.mark_started("call", worker_identity="one", process_pid=123)
    started = control.mark_started(
        "call", worker_identity="one", process_pid=123, process_start_identity="observed-start"
    )
    assert started["status"] == "running"
    with expect_error("call_conflict"):
        control.mark_started(
            "call", worker_identity="one", process_pid=123, process_start_identity="a-later-process"
        )


def test_failed_or_interrupted_call_is_preserved_and_unreported_cost_stays_reserved(control):
    reserve_paid(control)
    control.mark_started("call", worker_identity="worker")
    call = control.record_outcome(
        "call",
        outcome="interrupted",
        process_stopped=True,
        evidence_ref="timeout-trace.json",
        reason="timeout",
    )
    assert call["status"] == "failed"
    assert call["outcome"] == "interrupted"
    assert call["reason"] == "timeout"
    assert not call["cost_settled"]
    snapshot = BuildControl(control.ledger).snapshot()
    assert snapshot["active_workers"] == 0
    assert snapshot["reserved_usd"] == "10.000000"
    assert snapshot["calls"][0]["status"] == "failed"
    with expect_error("call_conflict"):
        control.record_outcome(
            "call",
            outcome="succeeded",
            process_stopped=True,
            evidence_ref="a-later-attempt.json",
            actual_cost_usd="2",
        )


def test_cost_settlement_releases_only_unused_reservation_and_is_immutable(control):
    reserve_paid(control)
    control.mark_started("call", worker_identity="worker")
    control.record_outcome(
        "call", outcome="failed", process_stopped=True, evidence_ref="failed.json"
    )
    control.settle_cost("call", actual_cost_usd="2.125001", evidence_ref="usage.json")
    control.settle_cost("call", actual_cost_usd="2.125001", evidence_ref="usage.json")
    with expect_error("cost_conflict"):
        control.settle_cost("call", actual_cost_usd="1", evidence_ref="changed.json")
    snapshot = control.snapshot()
    assert snapshot["settled_usd"] == "2.125001"
    assert snapshot["reserved_usd"] == "0.000000"
    assert snapshot["available_usd"] == "97.874999"


@pytest.mark.parametrize("uses_worker", [False, True])
def test_uncertain_process_holds_cost_until_real_stop_is_confirmed(control, uses_worker):
    reserve_paid(control, uses_worker=uses_worker)
    control.mark_started("call", worker_identity="worker")
    control.record_outcome(
        "call", outcome="unknown", process_stopped=False, evidence_ref="connection-lost.json"
    )
    assert control.snapshot()["calls"][0]["finished_at"] is None
    with expect_error("unsettled_process"):
        control.settle_cost("call", actual_cost_usd="1", evidence_ref="not-final.json")
    assert control.snapshot()["reserved_usd"] == "10.000000"
    assert control.snapshot()["active_workers"] == int(uses_worker)
    control.record_outcome(
        "call",
        outcome="failed",
        process_stopped=True,
        evidence_ref="process-and-result-checked.json",
        actual_cost_usd="1",
    )
    assert control.snapshot()["active_workers"] == 0
    assert control.snapshot()["available_usd"] == "99.000000"
    with expect_error("call_conflict"):
        control.record_outcome(
            "call", outcome="failed", process_stopped=False, evidence_ref="stale-observation.json"
        )


def test_unstarted_call_needs_proof_before_reservation_is_released(control):
    reserve_paid(control)
    with expect_error("unconfirmed_start"):
        control.cancel_unstarted("call", confirmed_not_started=False, evidence_ref="unknown")
    control.cancel_unstarted(
        "call", confirmed_not_started=True, evidence_ref="launch-not-attempted"
    )
    assert control.snapshot()["available_usd"] == "100.000000"
    with expect_error("invalid_transition"):
        control.mark_started("call", worker_identity="too-late")


def test_outcome_cannot_invent_success_or_spend_while_process_is_running(control):
    reserve_paid(control)
    with expect_error("invalid_transition"):
        control.record_outcome(
            "call", outcome="succeeded", process_stopped=True, evidence_ref="invented-result"
        )
    control.mark_started("call", worker_identity="worker")
    with expect_error("unsettled_process"):
        control.record_outcome(
            "call",
            outcome="succeeded",
            process_stopped=False,
            actual_cost_usd="1",
            evidence_ref="still-running",
        )


def test_observed_overcharge_is_retained_and_halts_admission(control):
    reserve_paid(control, cost="1")
    control.mark_started("call", worker_identity="worker")
    control.record_outcome(
        "call",
        outcome="failed",
        process_stopped=True,
        actual_cost_usd="101",
        evidence_ref="actual-provider-charge",
    )
    snapshot = control.snapshot()
    assert snapshot["settled_usd"] == "101.000000"
    assert snapshot["budget_breached"] is True
    assert any(event["kind"] == "charge_bound_exceeded" for event in snapshot["events"])
    with expect_error("budget_integrity_failure"):
        control.reserve("next", label="next", paid=False, max_cost_usd=None)


def _reserve_in_process(arguments):
    path, identifier = arguments
    control = BuildControl(path, clock=lambda: START)
    try:
        reserve_paid(control, identifier, cost="20")
        return "reserved"
    except ControlError as error:
        return error.code


def test_parallel_processes_cannot_spend_the_same_remaining_dollars(tmp_path, clock):
    control = BuildControl(tmp_path / "ledger.sqlite", clock=clock)
    control.initialize(run_id="test", started_at=START, worker_cap=10)
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as pool:
        outcomes = list(
            pool.map(_reserve_in_process, [(str(control.ledger), f"call-{n}") for n in range(8)])
        )
    assert outcomes.count("reserved") == 5
    assert outcomes.count("budget_exceeded") == 3
    assert control.snapshot()["reserved_usd"] == "100.000000"
    assert control.snapshot()["active_workers"] == 5


def test_parallel_connections_respect_finite_worker_capacity(control):
    def reserve(number):
        try:
            BuildControl(control.ledger, clock=lambda: START).reserve(
                f"free-{number}", label="free", paid=False, max_cost_usd=None
            )
            return "reserved"
        except ControlError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(reserve, range(12)))
    assert outcomes.count("reserved") == 2
    assert outcomes.count("capacity_exceeded") == 10
    with expect_error("capacity_conflict"):
        control.set_worker_cap(1, evidence_ref="measurement")
    control.set_worker_cap(3, evidence_ref="measured-resource-capacity.json")
    assert control.snapshot()["worker_cap"] == 3


def test_checkpoint_is_atomic_private_and_preserves_failed_history(control, tmp_path):
    reserve_paid(control)
    control.record_outcome(
        "call",
        outcome="failed",
        process_stopped=True,
        actual_cost_usd="0",
        evidence_ref="startup-failed.json",
    )
    result = control.checkpoint(tmp_path / "checkpoint.json")
    content = Path(result["path"]).read_bytes()
    assert hashlib.sha256(content).hexdigest() == result["sha256"]
    assert Path(result["path"]).stat().st_mode & 0o777 == 0o600
    assert json.loads(content)["calls"][0]["status"] == "failed"
    assert not list(tmp_path.glob(".checkpoint-*"))


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_checkpoint_cannot_replace_ledger_or_sqlite_sidecars(control, suffix):
    destination = control.ledger.parent / "nested" / ".." / (control.ledger.name + suffix)
    with expect_error("invalid_input"):
        control.checkpoint(destination)
    assert control.snapshot()["run_id"] == "test-build"


@pytest.mark.parametrize(
    "code,expected", [("print('ok')", "succeeded"), ("raise SystemExit(7)", "failed")]
)
def test_actual_bounded_subprocess_records_success_and_failure(control, tmp_path, code, expected):
    result = run_command(
        control,
        call_id="subprocess",
        command=[sys.executable, "-c", code],
        paid=False,
        max_cost_usd=None,
        timeout_seconds=3,
        output_directory=tmp_path / "traces",
    )
    assert result["call"]["status"] == expected
    assert result["call"]["cost_settled"] is True
    assert result["call"]["process_stopped"] is True
    assert control.snapshot()["active_workers"] == 0
    assert Path(result["stdout"]).stat().st_mode & 0o777 == 0o600


def test_real_timeout_is_failed_and_does_not_release_unreported_paid_cost(control, tmp_path):
    result = run_command(
        control,
        call_id="timeout",
        command=[sys.executable, "-c", "import time; time.sleep(10)"],
        paid=True,
        max_cost_usd="5",
        timeout_seconds=0.05,
        output_directory=tmp_path / "traces",
    )
    assert result["call"]["status"] == "failed"
    assert result["call"]["outcome"] == "interrupted"
    assert result["call"]["reason"] == "timeout"
    assert result["call"]["actual_usd"] is None
    assert control.snapshot()["reserved_usd"] == "5.000000"
    assert control.snapshot()["active_workers"] == 0


def test_two_runners_in_different_trace_directories_launch_one_process_only(control, tmp_path):
    marker = tmp_path / "executed.txt"
    code = (
        "from pathlib import Path; import sys,time; "
        "Path(sys.argv[1]).open('a').write('once\\n'); time.sleep(.15)"
    )

    def execute(number):
        try:
            return run_command(
                control,
                call_id="same-invocation",
                command=[sys.executable, "-c", code, str(marker)],
                paid=False,
                max_cost_usd=None,
                timeout_seconds=3,
                output_directory=tmp_path / f"trace-{number}",
            )["call"]["status"]
        except ControlError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(execute, range(2)))
    assert outcomes.count("succeeded") == 1
    assert outcomes.count("already_executed") == 1
    assert marker.read_text().splitlines() == ["once"]


def test_failed_process_launch_records_zero_cost_without_erasing_failure(control, tmp_path):
    with pytest.raises(FileNotFoundError):
        run_command(
            control,
            call_id="missing-command",
            command=[str(tmp_path / "absent")],
            paid=True,
            max_cost_usd="2",
            timeout_seconds=1,
            output_directory=tmp_path / "traces",
        )
    call = control.snapshot()["calls"][0]
    assert call["status"] == "failed"
    assert call["reason"] == "FileNotFoundError"
    assert call["actual_usd"] == "0.000000"


def test_cli_outputs_structured_errors_and_checkpoint(tmp_path):
    script = Path(__file__).parents[1] / "tools/build_control.py"
    ledger = tmp_path / "ledger.sqlite"
    command = [sys.executable, str(script), "--ledger", str(ledger)]
    started = datetime.now(UTC).isoformat()
    initialized = subprocess.run(
        command + ["init", "--run-id", "cli", "--started-at", started],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(initialized.stdout)["run_id"] == "cli"
    refused = subprocess.run(
        command + ["reserve", "call", "--label", "model", "--paid"], capture_output=True, text=True
    )
    assert refused.returncode == 2
    assert json.loads(refused.stderr)["error"]["code"] == "unknown_cost"
    invoked = subprocess.run(
        command
        + [
            "run",
            "actual-call",
            "--timeout-seconds",
            "2",
            "--output-directory",
            str(tmp_path / "traces"),
            "--command-json",
            json.dumps([sys.executable, "-c", "print('actual')"]),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(invoked.stdout)["call"]["status"] == "succeeded"
    result = subprocess.run(
        command + ["checkpoint", str(tmp_path / "checkpoint.json")],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["bytes"] > 0
