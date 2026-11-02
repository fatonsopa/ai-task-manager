"""Producer ownership and pre-return failure controls use the actual ledger."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event

import pytest

from ai_task_manager.domains.work.errors import AccessDenied
from tests.hosts.test_accounting_adapter import calls
from tests.hosts.test_accounting_adapter import graph as graph


def test_real_clock_validation_latency_cannot_extend_relay_parent(graph):
    g = graph
    g.ledger.current.clock = lambda: datetime.now(UTC)
    g.adapter.plan = g.plan = replace(g.plan, host_seconds=0.4)
    g.adapter._bound = (*g.adapter._bound[:5], g.plan, g.admission)
    prior = g.admission.verify_policy

    def slow(*args):
        prior(*args)
        if "host" in g.adapter._calls and "relay" not in g.adapter._calls:
            time.sleep(0.12)

    g.admission.verify_policy = slow
    pair = g.adapter.authorize_initialization(g.policy, g.binding)
    assert pair.relay.binding.deadline <= pair.native.binding.deadline <= g.binding.deadline
    assert calls(g)["relay"]["metadata"]["parent_call_id"] == pair.native.reference


def fail_after_relay(g, monkeypatch):
    original = g.adapter._claim

    def refused(policy, binding, purpose, *args, **kwargs):
        result = original(policy, binding, purpose, *args, **kwargs)
        if purpose == "relay":
            raise AccessDenied("Authored failure before initialization return")
        return result

    monkeypatch.setattr(g.adapter, "_claim", refused)


def test_pre_return_failure_reconciles_exact_never_started_claims(graph, monkeypatch):
    g = graph
    fail_after_relay(g, monkeypatch)
    with pytest.raises(AccessDenied, match="before initialization return"):
        g.adapter.authorize_initialization(g.policy, g.binding)
    assert set(calls(g)) == {"host", "relay"}
    assert all(
        r["status"] == "failed" and r["process_stopped"] and r["worker_state"] == "released"
        for r in calls(g).values()
    )
    evidence = [
        r
        for r in g.journal._history("trace")
        if r["payload"].get("effect") == "initialization_authorization_aborted_before_return"
    ]
    assert len(evidence) == 1
    assert evidence[0]["payload"]["native_launch_capability_returned"] is False
    assert g.core.core.get_attempt(g.core.handle, g.attempt.id).state == "reserved"
    with pytest.raises(AccessDenied, match="already attempted"):
        g.adapter.authorize_initialization(g.policy, g.binding)
    assert len(calls(g)) == 2


def test_claim_failure_after_reservation_is_recorded_without_a_pid(graph, monkeypatch):
    g = graph

    def refused(*args, **kwargs):
        raise AccessDenied("Authored claim refusal")

    monkeypatch.setattr(g.ledger.current, "claim_launch", refused)
    with pytest.raises(AccessDenied, match="claim refusal"):
        g.adapter.authorize_initialization(g.policy, g.binding)
    assert calls(g)["host"]["status"] == "failed"
    assert calls(g)["host"]["process_stopped"]
    assert calls(g)["host"]["process_pid"] is None


@pytest.mark.parametrize("variant", ["started", "unknown_pid", "scope_drift"])
def test_changed_or_started_claims_are_not_reconciled(graph, monkeypatch, variant):
    g = graph
    fail_after_relay(g, monkeypatch)
    append = g.journal._append

    def changed(*args, **kwargs):
        ref = append(*args, **kwargs)
        if args[1].get("effect") == "initialization_authorization_aborted_before_return":
            host = calls(g)["host"]
            if variant == "started":
                g.ledger.current.mark_started(host["call_id"], worker_identity=g.binding.worker_id)
            else:
                with g.ledger.current._store.transaction() as transaction:
                    transaction.update_call(
                        host["call_id"],
                        **(
                            {"process_pid": os.getpid(), "process_start_identity": "unknown"}
                            if variant == "unknown_pid"
                            else {"worker_identity": "foreign-owner"}
                        ),
                    )
        return ref

    monkeypatch.setattr(g.journal, "_append", changed)
    with pytest.raises(AccessDenied, match="before initialization return") as error:
        g.adapter.authorize_initialization(g.policy, g.binding)
    assert error.value.initialization_abort_error == "ControlError"
    assert not any(row["process_stopped"] for row in calls(g).values())
    assert calls(g)["relay"]["status"] == "launching"


def test_required_abort_journal_failure_keeps_claims_and_original_error(graph, monkeypatch):
    g = graph
    fail_after_relay(g, monkeypatch)

    def unavailable(*args, **kwargs):
        raise OSError("Authored required journal failure")

    monkeypatch.setattr(g.journal, "_append", unavailable)
    with pytest.raises(AccessDenied, match="before initialization return") as error:
        g.adapter.authorize_initialization(g.policy, g.binding)
    assert error.value.initialization_abort_error == "OSError"
    assert not any(row["process_stopped"] for row in calls(g).values())


def test_abort_cannot_be_called_outside_its_original_catch(graph):
    g = graph
    with pytest.raises(AccessDenied):
        g.adapter._abort_unreturned_initialization(g.binding, None, "untrusted")
    assert calls(g) == {}


def test_concurrent_initialization_cannot_abort_the_winning_owner(graph, monkeypatch):
    g = graph
    entered, release = Event(), Event()
    prior = g.admission.verify_policy

    def wait(*args):
        entered.set()
        assert release.wait(3)
        prior(*args)

    monkeypatch.setattr(g.admission, "verify_policy", wait)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(g.adapter.authorize_initialization, g.policy, g.binding)
        assert entered.wait(3)
        second = pool.submit(g.adapter.authorize_initialization, g.policy, g.binding)
        with pytest.raises(AccessDenied, match="already in progress"):
            second.result(timeout=3)
        release.set()
        pair = first.result(timeout=3)
    assert pair is g.adapter._initialization
    assert all(
        row["status"] == "launching" and not row["process_stopped"] for row in calls(g).values()
    )
