"""Authored result-state controls and one actual isolated-container verification."""

import hashlib
import json

import pytest

from ai_task_manager.adapters.hosts.bridge.container_plan import ContainerPlan
from ai_task_manager.adapters.hosts.bridge.containers import DockerCommands
from ai_task_manager.adapters.hosts.bridge.evaluation_reports import REPORT_KINDS, EvaluationReports
from ai_task_manager.adapters.hosts.bridge.workspace import Workspace
from ai_task_manager.adapters.process.groups import GroupObservation, GroupStop
from ai_task_manager.config.evaluation_records import PURPOSES, EvaluationRecords
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import Attempt
from ai_task_manager.supervisor.models import ResourcePair
from tests.hosts.bootstrap_helpers import bootstrap_graph as bootstrap_graph
from tests.hosts.bootstrap_helpers import grant_graph
from tests.hosts.test_containers import DOCKER, IMAGE
from tests.hosts.test_containers import command_fixture as command_fixture
from tests.hosts.test_evaluation_enrollment import enroll, task


def authored_report(g, monkeypatch):
    """Populate an explicitly authored terminal state; no Docker/native work is claimed."""
    handle = enroll(g)
    item = task(g, handle)
    report = {
        "row_count": 1,
        "total_requests": 5,
        "total_cost_cents": 2,
        "services": [{"service": "search", "requests": 5, "cost_cents": 2}],
    }
    workspace = g.item.root / "report-tree"
    workspace.mkdir()
    text = json.dumps(report)
    Workspace(workspace).write("report.json", text, None)
    sha = hashlib.sha256(text.encode()).hexdigest()
    pair = ResourcePair(
        "authored-attempt",
        workspace,
        g.item.root / "report-db",
        "authored-tree",
        "authored-db",
        "authored-resource",
    )
    attempt = Attempt(
        pair.attempt_id,
        item.id,
        item.session_id,
        "claude",
        "done",
        0,
        "authored-worker",
        pair.worktree_id,
        pair.database_id,
    )
    monkeypatch.setattr(g.core, "get_attempt", lambda current_handle, identity: attempt)
    manifest = {"files": {"report.json": sha}, "digest": digest({"report.json": sha})}
    result = {
        "outcome": "succeeded",
        "confirmed_stop": True,
        "container_removed": True,
        "input": manifest,
    }
    source = g.item.journal._append("trace", {"effect": "authored-container-stop-control"})
    ref = g.item.journal._append(
        "trace",
        {
            "effect": "container_command_verified",
            "attempt_id": attempt.id,
            "command_ref": "verify-report",
            "container_name": "authored-container",
            "result": result,
        },
        sources=(source,),
        expires_at=g.grant.data["expires_at"],
    )
    runner = DockerCommands.__new__(DockerCommands)
    runner.journal = g.item.journal
    runner.jobs = {
        "authored": {
            "record": {
                "attempt_id": attempt.id,
                "command_ref": "verify-report",
                "name": "authored-container",
                "input": manifest,
            },
            "stop": GroupStop(True, GroupObservation(True)),
            "verified_ref": ref,
            "verified_result": digest(result),
        }
    }
    oracle = {
        "grant_ref": g.ref,
        "case_id": g.grant.data["case_id"],
        "scope": g.body["scope"],
        "claim_store": g.claim.context,
        "task_id": item.id,
        "issue_id": item.issue_id,
        "prose_digest": item.prose_digest,
        "unit": "complete",
        "report_name": "report.json",
        "canonical_sha256": digest(report),
        "command_ref": "verify-report",
        "expires_at": g.grant.data["expires_at"],
    }
    oracle_ref = g.item.accounting.rig.pin("evaluation_report_oracle", oracle)
    records = EvaluationRecords(
        {k: v for k, v in g.item.accounting.rig.bindings.items() if v["kind"] in PURPOSES}
    )
    return EvaluationReports(g.grant, records, runner), oracle_ref, pair, runner


def test_report_facts_bind_actual_bytes_and_only_owned_completion_kinds(
    bootstrap_graph, monkeypatch
):
    g = grant_graph(bootstrap_graph())
    issuer, oracle, pair, _ = authored_report(g, monkeypatch)
    facts = issuer.observe(oracle, pair)
    assert set(facts) == REPORT_KINDS
    with g.core.context.db.transaction():
        for kind, ref in facts.items():
            evidence = issuer.verify_evidence(ref)
            assert evidence.kind == kind and evidence.subject == g.grant._task_id
    with pytest.raises(AccessDenied):
        issuer.verify_evidence("evaluation-report:unobserved")
    with pytest.raises(AccessDenied):
        issuer.observe(oracle, pair)


@pytest.mark.parametrize("change", ["report", "manifest", "result", "unknown", "ambiguous"])
def test_changed_or_unconfirmed_report_result_refuses(bootstrap_graph, monkeypatch, change):
    g = grant_graph(bootstrap_graph())
    issuer, oracle, pair, runner = authored_report(g, monkeypatch)
    job = runner.jobs["authored"]
    if change == "report":
        (pair.worktree / "report.json").write_text("{}")
    elif change == "manifest":
        job["record"]["input"]["files"]["report.json"] = "foreign"
    elif change == "result":
        job["verified_result"] = "foreign"
    elif change == "unknown":
        job["stop"] = GroupStop(False, GroupObservation(False))
    else:
        runner.jobs["other"] = dict(job)
    with pytest.raises(AccessDenied):
        issuer.observe(oracle, pair)


def test_report_mutation_after_preparation_refuses_core_fact_consumption(
    bootstrap_graph, monkeypatch
):
    g = grant_graph(bootstrap_graph())
    issuer, oracle, pair, _ = authored_report(g, monkeypatch)
    facts = issuer.observe(oracle, pair)
    (pair.worktree / "report.json").write_text("{}")
    with g.core.context.db.transaction(), pytest.raises(AccessDenied):
        issuer.verify_evidence(facts["non_code_completion"])


@DOCKER
def test_typed_result_retains_actual_success_and_owned_cleanup(command_fixture):
    _, dispatch, gateway, boundary, effect, create, _ = command_fixture
    gateway.call(
        "write_file",
        {
            "path": "report.json",
            "text": "{}",
            "expected_hash": None,
            "operation_key": "report-fixture",
        },
    )
    runner = create(
        {
            "verify-report": ContainerPlan(
                IMAGE, ("python", "-I", "-c", "print('REPORT_VERIFIED')"), ("report.json",)
            )
        }
    )
    returned = runner.execute_registered(
        "verify-report", dispatch.resources, boundary, effect, "report-verify"
    )
    retained = runner.verified_result(dispatch.attempt_id, "verify-report")
    assert returned["outcome"] == "succeeded"
    assert dict(retained.files)["report.json"] == hashlib.sha256(b"{}").hexdigest()
    assert runner.reconcile_stopped(dispatch.attempt_id)
