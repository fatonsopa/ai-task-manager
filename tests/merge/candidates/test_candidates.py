"""Scoped actual local Git/Core candidate controls; not full merge or host acceptance."""

from dataclasses import replace
from pathlib import Path

import pytest

from ai_task_manager.adapters.resources.git import git
from ai_task_manager.domains.work.errors import WorkError
from tests.merge.candidates.helpers import CandidateRig


@pytest.fixture
def rig(tmp_path):
    value = object.__new__(CandidateRig)
    try:
        value.__init__(tmp_path / "owned")
        yield value
    finally:
        if hasattr(value, "runtime"):
            value.close()


def test_completed_worker_prepares_guarded_isolated_candidate(rig):
    before = (rig.tree / "task.txt").read_bytes()
    database = rig.pair.database.read_bytes()
    result = rig.service.prepare(rig.grant)
    assert result.status == "ready", result
    assert git(rig.tree, "rev-parse", "HEAD") == rig.source_head
    assert (
        rig.tree / "task.txt"
    ).read_bytes() == before and rig.pair.database.read_bytes() == database
    parents = git(Path(result.path), "rev-list", "--parents", "-n1", result.commit).split()
    assert parents == [result.commit, rig.target_head, rig.source_head]
    assert (rig.root / "guard.log").read_text().splitlines() == ["pre-commit", "commit-msg"]
    assert git(rig.tree, "rev-parse", "refs/heads/" + rig.target) == rig.target_head
    with pytest.raises(WorkError):
        rig.r.core.execution.validate_work_lease(rig.r.handle, rig.attempt.id, rig.attempt.version)
    assert rig.r.core.get_task(rig.r.handle, rig.task.id).observed_status == "In Progress"


def test_independent_stable_target_preserves_both_lineages(tmp_path):
    value = object.__new__(CandidateRig)
    try:
        value.__init__(tmp_path / "unrelated", target="stable", diverge=True)
        result = value.service.prepare(value.grant)
        assert result.status == "ready", result
        assert (Path(result.path) / "target.txt").read_text() == "target change\n"
        assert (Path(result.path) / "task.txt").read_text() == "worker committed change\n"
        assert git(value.tree, "rev-parse", "HEAD") == value.source_head
    finally:
        if hasattr(value, "runtime"):
            value.close()


def test_required_precommit_guard_is_not_bypassed(tmp_path):
    value = object.__new__(CandidateRig)
    try:
        value.__init__(tmp_path / "denied", deny="pre-commit")
        result = value.service.prepare(value.grant)
        assert result.status == "refused", result
        assert git(value.tree, "rev-parse", "refs/heads/dev") == value.target_head
        assert (value.root / "guard.log").read_text().splitlines() == ["pre-commit"]
        assert Path(result.path).exists()
        with pytest.raises(WorkError):
            value.service.cleanup(result.operation_ref)
    finally:
        if hasattr(value, "runtime"):
            value.close()


def test_dirty_worker_and_historical_backup_refs_are_not_candidate_inputs(rig):
    (rig.tree / "task.txt").write_text("uncommitted worker draft\n")
    (rig.tree / "scratch.txt").write_text("keep untracked\n")
    git(rig.tree, "branch", "recovery/old", rig.old)
    before = git(rig.tree, "status", "--porcelain", "-z")
    result = rig.service.prepare(rig.grant)
    assert result.status == "ready", result
    assert (Path(result.path) / "task.txt").read_text() == "worker committed change\n"
    assert not (Path(result.path) / "scratch.txt").exists()
    assert git(rig.tree, "status", "--porcelain", "-z") == before
    assert (rig.tree / "scratch.txt").read_text() == "keep untracked\n"


def test_same_key_replay_and_canonical_task_claim_prevent_second_candidate(rig):
    first = rig.service.prepare(rig.grant)
    assert rig.service.prepare(rig.grant) == first
    assert len(list(rig.candidate_profile.root.iterdir())) == 1
    other = replace(rig.grant, binding=replace(rig.binding, operation_key="different-operation"))
    with pytest.raises(WorkError):
        rig.service.prepare(other)
    with pytest.raises(WorkError):
        rig.service.prepare(replace(rig.grant, source_head=rig.old))
    assert len(list(rig.candidate_profile.root.iterdir())) == 1


@pytest.mark.parametrize("which", ["target", "source"])
def test_current_ref_advance_invalidates_without_rewriting_candidate(rig, which):
    result = rig.service.prepare(rig.grant)
    old = (result.commit, result.tree)
    ref = "refs/heads/" + rig.target if which == "target" else rig.grant.source_ref
    git(
        rig.tree,
        "update-ref",
        ref,
        result.commit,
        rig.target_head if which == "target" else rig.source_head,
    )
    changed = rig.service.reconcile(result.operation_ref)
    assert changed.status == which + "_moved" and (changed.commit, changed.tree) == old
    assert git(Path(result.path), "rev-parse", "HEAD") == result.commit


def test_conflict_is_retained_and_cleanup_refuses(tmp_path):
    value = object.__new__(CandidateRig)
    try:
        value.__init__(tmp_path / "conflict", conflict=True)
        result = value.service.prepare(value.grant)
        assert result.status == "conflict", result
        assert value.service.prepare(value.grant) == result
        assert value.candidate_profile.query(Path(result.path), "ls-files", "--unmerged").stdout
        with pytest.raises(WorkError):
            value.service.cleanup(result.operation_ref)
        assert git(value.tree, "rev-parse", "HEAD") == value.source_head
    finally:
        if hasattr(value, "runtime"):
            value.close()


def test_ready_unique_output_is_retained_without_publication_verification(rig):
    first = rig.service.prepare(rig.grant)
    with pytest.raises(WorkError):
        rig.service.cleanup(first.operation_ref)
    assert Path(first.path).is_dir()
    assert git(rig.tree, "rev-parse", first.candidate_ref) == first.commit
    assert rig.tree.is_dir() and rig.pair.database.is_file()
    assert git(rig.tree, "rev-parse", "HEAD") == rig.source_head


def test_dirty_or_changed_candidate_cannot_be_reused_or_deleted(rig):
    result = rig.service.prepare(rig.grant)
    (Path(result.path) / "foreign.txt").write_text("retain\n")
    with pytest.raises(WorkError):
        rig.service.cleanup(result.operation_ref)
    invalid = rig.service.reconcile(result.operation_ref)
    assert invalid.status == "refused" and invalid.commit == result.commit
    assert (Path(result.path) / "foreign.txt").read_text() == "retain\n"


@pytest.mark.parametrize("stage", ["before:worktree", "observed:worktree", "released:commit"])
def test_interrupted_stages_never_duplicate_unknown_effects(rig, stage):
    from tests.merge.f1_fixtures import FaultOnce

    rig.service.runner.faults = FaultOnce(stage)
    first = rig.service.prepare(rig.grant)
    assert first.status == "uncertain", first
    repeated = rig.service.prepare(rig.grant)
    if stage == "before:worktree":
        assert repeated.status == "uncertain" and not list(rig.candidate_profile.root.iterdir())
    else:
        assert repeated.status == "ready", repeated
        assert len(list(rig.candidate_profile.root.iterdir())) == 1
        assert (rig.root / "guard.log").read_text().splitlines() == ["pre-commit", "commit-msg"]


def test_packed_refs_remain_supported(rig):
    git(rig.tree, "pack-refs", "--all", "--prune")
    result = rig.service.prepare(rig.grant)
    assert result.status == "ready", result


@pytest.mark.parametrize(
    "change",
    [
        "hold",
        "task_version",
        "attempt_version",
        "decision",
        "foreign_scope",
        "foreign_task",
        "active_attempt",
    ],
)
def test_completed_merge_snapshot_refuses_without_current_authority(rig, change):
    grant = rig.grant
    if change == "hold":
        task = rig.runtime.hold(rig.dispatch)
        grant = replace(
            grant,
            task_version=task.version,
            decision_revision=task.decision_revision,
            decision_ref=rig.r.decision(task, "In Dev"),
        )
    elif change == "task_version":
        grant = replace(grant, task_version=grant.task_version - 1)
    elif change == "attempt_version":
        grant = replace(grant, attempt_version=grant.attempt_version - 1)
    elif change == "decision":
        grant = replace(grant, decision_ref=rig.r.decision(rig.task, "In Staging"))
    elif change == "foreign_scope":
        grant = replace(grant, binding=replace(grant.binding, principal_id="foreign"))
    elif change == "foreign_task":
        grant = replace(grant, binding=replace(grant.binding, task_id="foreign"))
    else:
        from tests.scheduling.helpers import known_declaration

        known_declaration(rig.r, rig.task)
        rig.r.core.execution.reserve(
            rig.r.handle,
            rig.task.id,
            "fixture",
            rig.task.version,
            rig.task.decision_revision,
            rig.r.decision(rig.task, "In Progress"),
            rig.r.key(),
        )
        task = rig.r.core.get_task(rig.r.handle, rig.task.id)
        grant = replace(
            grant, task_version=task.version, decision_ref=rig.r.decision(task, "In Dev")
        )
    with pytest.raises(WorkError):
        rig.service.prepare(grant)
    assert not list(rig.candidate_profile.root.iterdir())


def test_noncode_worker_cannot_mint_merge_authority(tmp_path):
    value = object.__new__(CandidateRig)
    try:
        value.__init__(tmp_path / "noncode", code=False)
        with pytest.raises(WorkError):
            value.service.prepare(value.grant)
        assert not list(value.candidate_profile.root.iterdir())
    finally:
        if hasattr(value, "runtime"):
            value.close()


def test_required_hook_change_is_rejected_before_git_effects(rig):
    (rig.runtime.repo / ".git/hooks/pre-commit").write_text("#!/bin/sh\nexit 0\n")
    with pytest.raises(WorkError):
        rig.service.prepare(rig.grant)
    assert not list(rig.candidate_profile.root.iterdir())


def test_concurrent_candidate_contenders_have_one_canonical_effect_owner(rig):
    import threading

    results, errors = [], []
    grants = [
        replace(rig.grant, binding=replace(rig.binding, operation_key="contender:" + str(n)))
        for n in range(2)
    ]
    barrier = threading.Barrier(2, timeout=3)

    def call(grant):
        try:
            barrier.wait()
            results.append(rig.service.prepare(grant))
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=call, args=(g,)) for g in grants]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
        assert not thread.is_alive()
    assert len(results) == 1 and results[0].status == "ready"
    assert len(errors) == 1 and isinstance(errors[0], WorkError)
    assert len(list(rig.candidate_profile.root.iterdir())) == 1


def test_lost_original_observer_cannot_be_replaced_with_fresh_process_guess(rig):
    from ai_task_manager.adapters.git.candidates.process import CandidateProcesses
    from ai_task_manager.application.merging.candidates import CandidateService

    original = rig.service.runner.observe

    def missing(child):
        raise RuntimeError("Authored lost child observer")

    rig.service.runner.observe = missing
    try:
        with pytest.raises(RuntimeError):
            rig.service.prepare(rig.grant)
        fresh = CandidateService(
            rig.journal, rig.candidate_profile, rig.authority, process_factory=CandidateProcesses
        )
        result = fresh.prepare(rig.grant)
        assert result.status == "uncertain" and not fresh.runner.live
        with pytest.raises(WorkError):
            fresh.cleanup(result.operation_ref)
    finally:
        rig.service.runner.observe = original
        for ref in rig.service.grants:
            rig.service.runner.reconcile(ref)
    result = rig.service.prepare(rig.grant)
    assert result.status == "ready", result
    assert len(list(rig.candidate_profile.root.iterdir())) == 1


def test_already_integrated_source_reports_same_commit_without_inventing_commit(rig):
    git(rig.tree, "update-ref", "refs/heads/" + rig.target, rig.source_head, rig.target_head)
    grant = replace(rig.grant, target_head=rig.source_head)
    result = rig.service.prepare(grant)
    assert result.status == "ready" and result.commit == rig.source_head
    assert "commit" not in rig.service.store.get(result.operation_ref)["effects"]
    assert not (rig.root / "guard.log").exists()


def test_required_commit_message_guard_is_not_bypassed(tmp_path):
    value = object.__new__(CandidateRig)
    try:
        value.__init__(tmp_path / "message-denial", deny="commit-msg")
        result = value.service.prepare(value.grant)
        assert result.status == "refused", result
        assert (value.root / "guard.log").read_text().splitlines() == ["pre-commit", "commit-msg"]
        assert git(value.tree, "rev-parse", "refs/heads/dev") == value.target_head
    finally:
        if hasattr(value, "runtime"):
            value.close()


@pytest.mark.parametrize("change", ["source_ref", "repository_alias"])
def test_unowned_source_ref_or_repository_alias_is_refused(rig, change):
    if change == "source_ref":
        git(rig.tree, "branch", "foreign-task", rig.old)
        grant = replace(rig.grant, source_ref="refs/heads/foreign-task", source_head=rig.old)
    else:
        grant = replace(rig.grant, binding=replace(rig.binding, repository_id="foreign-repository"))
    with pytest.raises(WorkError):
        rig.service.prepare(grant)
    assert not list(rig.candidate_profile.root.iterdir())


def test_configured_profile_cannot_reuse_owned_alias_for_different_physical_worktree(rig):
    from ai_task_manager.adapters.git.candidates import CandidateProfile
    from ai_task_manager.adapters.git.candidates.process import CandidateProcesses
    from ai_task_manager.adapters.git.profile import GitProfile
    from ai_task_manager.application.merging.candidates import CandidateService

    foreign = rig.root / "other-worktree"
    git(rig.tree, "worktree", "add", "-b", "other-task", str(foreign), rig.source_head)
    home = rig.root / "other-git-home"
    home.mkdir(mode=0o700)
    profile = GitProfile(root=rig.root, executable=rig.profile.executable, home=home)
    record = profile.enroll(
        foreign,
        worktree_id=rig.binding.worktree_id,
        repository_id=rig.binding.repository_id,
        target_ref="refs/heads/" + rig.target,
    )
    configured = CandidateProfile(
        profile,
        rig.binding.worktree_id,
        rig.binding.repository_id,
        rig.candidate_profile.root,
        required_hooks=("pre-commit", "commit-msg"),
    )
    service = CandidateService(
        rig.journal, configured, rig.authority, process_factory=CandidateProcesses
    )
    grant = replace(
        rig.grant,
        source_ref="refs/heads/other-task",
        binding=replace(rig.binding, configuration_digest=record["configuration_digest"]),
    )
    with pytest.raises(WorkError):
        service.prepare(grant)
    assert not list(rig.candidate_profile.root.iterdir())


def test_delivered_cleanup_waits_for_publication_verifier_without_granting_merge_work(rig):
    from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
    from ai_task_manager.adapters.proofs.store import RUNTIME_KINDS
    from ai_task_manager.domains.work.errors import EvidenceRequired

    result = rig.service.prepare(rig.grant)
    # Authored lifecycle evidence exercises actual Core state only. It is not an
    # actual publication receipt and must never authorize resource deletion.
    rig.r.core.context.evidence = EvidenceRouter(
        [
            EvidenceRoute("runtime-proof:", RUNTIME_KINDS, rig.runtime.proofs),
            EvidenceRoute(
                "fixture:",
                frozenset(
                    {"provider_status", "decision", "human_decision", "work_unit_complete", "merge"}
                ),
                rig.r.proofs,
            ),
        ]
    )
    task = rig.r.complete(rig.r.current(rig.task))
    delivered = rig.r.transition(task, "In Dev", "merge", {"merge": rig.r.delivery(task)["merge"]})
    assert delivered.delivered_once and delivered.observed_status == "In Dev"
    with pytest.raises(EvidenceRequired):
        rig.service.cleanup(result.operation_ref)
    with pytest.raises(WorkError):
        rig.authority.check_current(rig.grant)
    assert Path(result.path).is_dir()
    assert git(rig.tree, "rev-parse", result.candidate_ref) == result.commit


def test_recovery_cannot_swap_registered_candidate_grant(rig):
    result = rig.service.prepare(rig.grant)
    with pytest.raises(WorkError):
        rig.service.reconcile(result.operation_ref, replace(rig.grant, owner_ref="different-owner"))
    assert rig.service.reconcile(result.operation_ref) == result


def test_unsupported_premerge_guard_is_explicitly_refused(rig):
    from ai_task_manager.adapters.git.candidates import CandidateProfile

    path = rig.runtime.repo / ".git/hooks/pre-merge-commit"
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o700)
    with pytest.raises(WorkError):
        CandidateProfile(
            rig.profile,
            rig.binding.worktree_id,
            rig.binding.repository_id,
            rig.candidate_profile.root,
            required_hooks=("pre-commit", "commit-msg"),
        )
    assert not list(rig.candidate_profile.root.iterdir())


def test_two_coherent_profile_labels_share_one_physical_task_candidate_owner(rig):
    from dataclasses import asdict

    from ai_task_manager.adapters.git.candidates import CandidateProfile
    from ai_task_manager.adapters.git.candidates.process import CandidateProcesses
    from ai_task_manager.adapters.git.profile import GitProfile
    from ai_task_manager.application.merging.candidates import CandidateService
    from ai_task_manager.domains.work.errors import AccessDenied

    home = rig.root / "second-profile-home"
    home.mkdir(mode=0o700)
    second = GitProfile(root=rig.root, executable=rig.profile.executable, home=home)
    label = "second-configured-repository-label"
    record = second.enroll(
        rig.tree,
        worktree_id=rig.binding.worktree_id,
        repository_id=label,
        target_ref="refs/heads/" + rig.target,
    )
    candidate_root = rig.root / "second-candidates"
    candidate_root.mkdir(mode=0o700)
    configured = CandidateProfile(
        second,
        rig.binding.worktree_id,
        label,
        candidate_root,
        required_hooks=("pre-commit", "commit-msg"),
    )
    for field in ("path", "identity", "common", "common_identity"):
        assert configured.source[field] == rig.candidate_profile.source[field]
    assert configured.repository_id != rig.candidate_profile.repository_id
    grant = replace(
        rig.grant,
        owner_ref="lead-service:second-profile",
        binding=replace(
            rig.binding,
            repository_id=label,
            configuration_digest=record["configuration_digest"],
            operation_key="second-profile-operation",
        ),
    )
    rig.authority.check_current(grant)
    rig.authority.check_source(grant, configured.source)
    assert configured.refs(grant) == (rig.source_head, rig.target_head)
    service = CandidateService(
        rig.journal, configured, rig.authority, process_factory=CandidateProcesses
    )
    first = rig.service.prepare(rig.grant)
    assert first.status == "ready"
    try:
        with pytest.raises(AccessDenied, match="different or unresolved candidate"):
            service.prepare(grant)
        assert not list(candidate_root.iterdir())
        assert not service.grants and not service.runner.live
        original = rig.journal.current(first.operation_ref)
        assert original["binding"]["repository_id"] == rig.binding.repository_id
        assert original["binding"]["configuration_digest"] == rig.binding.configuration_digest
        assert original["facts"]["candidate"]["request"]["grant"]["binding"] == asdict(rig.binding)
        rows = rig.journal.for_attempt(rig.binding.attempt_id)
        refused = [
            row for row in rows if row["binding"]["operation_key"] == grant.binding.operation_key
        ]
        assert len(refused) == 1 and refused[0]["facts"] == {}
        assert refused[0]["binding"] == asdict(grant.binding)
        claims = [row for row in rows if "candidate_owner" in row["facts"]]
        assert len(claims) == 1
        assert claims[0]["binding"]["repository_id"].startswith("physical-repository:")
        assert rig.service.store.owner(first.operation_ref)["operation"] == first.operation_ref
        assert rig.service.reconcile(first.operation_ref) == first
    finally:
        for reference in service.grants:
            service.runner.reconcile(reference)
        assert not service.runner.live
