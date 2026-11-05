"""Default resolver controls use production provisioning, never an injected code registry."""

import json
from dataclasses import replace

import pytest

from ai_task_manager.config.files import digest, write_private
from ai_task_manager.domains.checks.models import Scope
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.workflows.code_registration import CodeRegistration
from tests.code_provisioning.helpers import Provisioned


@pytest.fixture
def owned(tmp_path):
    fixture = Provisioned(tmp_path / "owned")
    try:
        yield fixture
    finally:
        fixture.close()


def test_default_resolver_provisions_actual_code_capabilities(owned):
    cap = owned.prepare()
    cap.require()
    owner = cap.checks.checks.source_guard
    assert owner.runner.root == owner.root / "containers"
    assert owner.runner.root != owned.context.host_journal.path.parent / "commands"
    ref = cap.checks.prepare(owned.candidate_profile, owned.grant, owned.candidate, "cold")
    snapshot = cap.checks.checks.run(
        ref,
        Scope(
            owned.r.scope.principal,
            owned.r.scope.project,
            owned.r.scope.logical_session,
            owned.task.id,
        ),
    )
    assert snapshot["outcome"] == "passed"


def test_legacy_registration_fingerprint_is_unchanged(owned):
    code = CodeRegistration(owned.policy, lambda context: None)
    assert code.fingerprint == digest({"policy": code.seal.original, "data": code.data})
    code.check()


@pytest.mark.parametrize("source", ["policy", "catalog", "commands", "manifest"])
def test_transitive_source_change_refuses_registered_consumption(owned, source):
    code = owned.resolve()
    path = getattr(owned, source)
    value = json.loads(path.read_text())
    value["untrusted_new_field"] = True
    write_private(path, value, replace=True)
    with pytest.raises(AccessDenied):
        code.check()
    assert not owned.journal._intents()


@pytest.mark.parametrize("fault", ["task", "prose", "scope", "deadline"])
def test_wrong_current_context_refuses_before_check_effects(owned, fault):
    code = owned.resolve()
    task, context = owned.task, owned.context
    if fault == "task":
        context = replace(context, task=replace(task, id="foreign"))
    elif fault == "prose":
        context = replace(context, task=replace(task, prose_digest="0" * 64))
    elif fault == "deadline":
        context = replace(context, deadline=context.deadline + 1)
    else:
        foreign = owned.r.session(replace(owned.r.scope, logical_session="foreign"))
        context = replace(context, handle=foreign)
    with pytest.raises(AccessDenied):
        code.prepare(context)
    assert not owned.journal._intents()


def test_default_provisioned_checks_deliver_to_trusted_target(owned):
    from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
    from ai_task_manager.adapters.resources.git import git
    from ai_task_manager.adapters.workflow_reviews.code_candidates import CodeCandidateReviews
    from ai_task_manager.application.merging.workflows import (
        MergeEvidence,
        MergePlan,
        MergeWorkflowService,
    )
    from tests.merge.workflows.test_candidate_review import Captures
    from tests.runtime.helpers import ProviderFixture

    cap = owned.prepare()
    evidence = MergeEvidence(clock=lambda: owned.r.now)
    owned.r.core.context.evidence = EvidenceRouter(
        [
            *owned.r.core.context.evidence.routes,
            EvidenceRoute(
                "merge-publication:", frozenset({"merge", "publication_intent"}), evidence
            ),
        ]
    )
    reviews = CodeCandidateReviews(
        owned.r.core,
        owned.r.handle,
        owned.candidates,
        ("verify.py", "src/a.txt"),
        Captures(lambda: owned.r.now),
    )
    plan = MergePlan("production-provisioning", owned.grant, owned.context.deadline)
    workflow = MergeWorkflowService(
        owned.r.core,
        owned.r.handle,
        plan,
        owned.candidates,
        cap.checks,
        ProviderFixture(owned.r),
        evidence,
        clock=lambda: owned.r.now,
        candidate_reviews=reviews,
    )
    try:
        result = workflow.run(owned.task.id, plan.id, "default-resolution-delivery")
        assert result["data"]["state"] == "done", result
        assert git(owned.runtime.repo, "show", "integration:src/a.txt") == "4"
        assert owned.r.core.get_task(owned.r.handle, owned.task.id).observed_status == "In Dev"
        assert result["data"]["cleanup"] == "pending_independent_retirement"
    finally:
        outcomes = workflow.close()
        assert all(
            row["candidate_children"] == 0 and row["publication_children"] == 0 for row in outcomes
        )


@pytest.mark.parametrize("fault", ["target", "target_candidate", "source", "deadline", "hold"])
def test_changed_candidate_or_authority_refuses_before_container_effect(owned, fault):
    from ai_task_manager.adapters.resources.git import git

    cap = owned.prepare()
    ref = cap.checks.prepare(owned.candidate_profile, owned.grant, owned.candidate, "pending")
    if fault == "target":
        git(
            owned.runtime.repo,
            "update-ref",
            "refs/heads/integration",
            owned.grant.source_head,
            owned.base,
        )
    elif fault == "target_candidate":
        git(
            owned.runtime.repo,
            "update-ref",
            "refs/heads/integration",
            owned.candidate.commit,
            owned.base,
        )
    elif fault == "source":
        from pathlib import Path

        (Path(owned.candidate.path) / "src/a.txt").write_text("foreign changed bytes")
    elif fault == "deadline":
        owned.r.now = owned.context.deadline + 1
    else:
        owned.runtime.hold(owned.dispatch)
    with pytest.raises(AccessDenied):
        cap.checks.checks.run(
            ref,
            Scope(
                owned.r.scope.principal,
                owned.r.scope.project,
                owned.r.scope.logical_session,
                owned.task.id,
            ),
        )
    assert not owned.journal._intents()


@pytest.mark.parametrize("fault", ["command", "group", "catalog", "git", "factory"])
def test_invalid_provisioning_is_refused_by_default_resolution(owned, fault):
    from ai_task_manager.domains.work.errors import ValidationError

    manifest = json.loads(owned.manifest.read_text())
    if fault == "command":
        manifest["units"][0]["command_ref"] = "uninstalled"
    elif fault == "group":
        manifest["units"][0]["group"] = "unmapped"
    elif fault == "git":
        manifest["git"]["sha256"] = "0" * 64
    elif fault == "factory":
        manifest["factory"] = "arbitrary.import:prepare"
    else:
        catalog = json.loads(owned.catalog.read_text())
        catalog["groups"][0]["runner"] = "unknown"
        write_private(owned.catalog, catalog, replace=True)
        from tests.code_provisioning.helpers import pointer

        manifest["catalog"] = pointer(owned.catalog)
    write_private(owned.manifest, manifest, replace=True)
    owned.reseal()
    with pytest.raises((AccessDenied, ValidationError)):
        owned.resolve()
    assert not owned.journal._intents()


def test_changed_plan_scope_and_inventory_refuse_before_effects(owned):
    cap = owned.prepare()
    ref = cap.checks.prepare(owned.candidate_profile, owned.grant, owned.candidate, "scope")
    scope = Scope(
        owned.r.scope.principal, owned.r.scope.project, owned.r.scope.logical_session, owned.task.id
    )
    plan = cap.checks.checks.store.read(ref, scope)["plan"]
    guard = cap.checks.checks.source_guard
    for changed in (
        replace(plan, scope=replace(scope, session="foreign")),
        replace(plan, inventory_digest="0" * 64),
        replace(plan, producer_digest="0" * 64),
    ):
        with pytest.raises(AccessDenied):
            guard.verify(changed)
    assert not owned.journal._intents()


def test_recorded_publication_cannot_reauthorize_old_target(owned, monkeypatch):
    cap = owned.prepare()
    ref = cap.checks.prepare(
        owned.candidate_profile, owned.grant, owned.candidate, "rolled-back-target"
    )
    scope = Scope(
        owned.r.scope.principal, owned.r.scope.project, owned.r.scope.logical_session, owned.task.id
    )
    plan = cap.checks.checks.store.read(ref, scope)["plan"]
    guard = cap.checks.checks.source_guard
    # Negative predicate fault: an already-confirmed publication with the old
    # target restored must never look like a fresh pre-publication source.
    # Genuine receipt matching is exercised by the actual publication control.
    monkeypatch.setattr(guard, "_published", lambda *args: True)
    with pytest.raises(AccessDenied, match="source or target"):
        guard.verify(plan)
    assert not owned.journal._intents()


@pytest.mark.parametrize("operation", ["discover", "execute"])
def test_pending_publication_refuses_new_checks_but_allows_evidence_validation(owned, operation):
    from dataclasses import asdict, replace

    cap = owned.prepare()
    ref = cap.checks.prepare(
        owned.candidate_profile, owned.grant, owned.candidate, "pending-intent"
    )
    scope = Scope(
        owned.r.scope.principal, owned.r.scope.project, owned.r.scope.logical_session, owned.task.id
    )
    plan = cap.checks.checks.store.read(ref, scope)["plan"]
    guard = cap.checks.checks.source_guard
    candidate, grant, _, _, store = guard.selected
    binding = replace(grant.binding, operation_key="owned-pending-publication")
    saved = store.journal.register(asdict(binding), binding.fingerprint)
    store.journal.commit(
        saved["ref"],
        0,
        "intent",
        "publication_intent",
        {"publication_intent": {"new": candidate.commit}},
    )
    # The original target has not moved and prior input facts still validate.
    guard.verify(plan)
    assert not guard.runner.jobs
    with pytest.raises(AccessDenied, match="Frozen publication intent"):
        if operation == "discover":
            guard.discover(candidate)
        else:
            guard.execute(plan, "checks", ("verify",))
    assert not guard.runner.jobs
    assert not owned.journal._intents()


@pytest.mark.parametrize("maximum", [0, -1, True, 65, 1.5, "8"])
def test_retained_budget_invalid_types_refuse_before_constructor_effects(maximum):
    from ai_task_manager.adapters.hosts.bridge.containers import DockerCommands

    with pytest.raises(AccessDenied, match="budget"):
        DockerCommands(None, None, None, None, None, deadline=0, maximum_jobs=maximum)


def test_more_than_four_declared_commands_have_one_pinned_phase_budget(owned):
    commands = json.loads(owned.commands.read_text())
    template = commands["commands"][0]
    commands["commands"] = [dict(template, reference=f"verify-{i}") for i in range(5)]
    write_private(owned.commands, commands, replace=True)
    manifest = json.loads(owned.manifest.read_text())
    manifest["units"] = [
        {"name": f"unit-{i}", "group": "checks", "command_ref": f"verify-{i}"} for i in range(5)
    ]
    manifest["attempts"] = 1
    write_private(owned.manifest, manifest, replace=True)
    owned.reseal()
    cap = owned.prepare()
    native = owned.registry.require("normal-native").prepare(owned.context)
    assert native.confined_commands.runner.maximum_jobs == 5
    owner = cap.checks.checks.source_guard
    assert owner.runner.maximum_jobs == 5
    ref = cap.checks.prepare(owned.candidate_profile, owned.grant, owned.candidate, "five-commands")
    scope = Scope(
        owned.r.scope.principal, owned.r.scope.project, owned.r.scope.logical_session, owned.task.id
    )
    result = cap.checks.checks.run(ref, scope)
    assert result["outcome"] == "passed", result
    assert result["groups"]["checks"]["result"]["physical_count"] == 5
    assert len(owner.runner.jobs) == 5
    assert all(job["stop"].confirmed for job in owner.runner.jobs.values())
