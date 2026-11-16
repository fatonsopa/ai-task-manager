"""Reopen original local Git/check evidence after an actual controller crash."""

import multiprocessing
import os
from dataclasses import replace

import pytest

from ai_task_manager.adapters.git.candidates import CandidateProfile
from ai_task_manager.adapters.git.candidates.process import CandidateProcesses
from ai_task_manager.adapters.resources.git import git
from ai_task_manager.application.merging.candidates import CandidateService, CoreCandidateAuthority
from ai_task_manager.storage.merging.journal import Journal
from tests.code_provisioning.helpers import Provisioned
from tests.merge_workloads.test_recovery import compose


def reopen(owned):
    owned.context = replace(
        owned.context, task=owned.r.core.get_task(owned.r.handle, owned.task.id)
    )
    owned.capability = owned.resolve().prepare(owned.context)
    old = owned.candidate_profile
    profile = owned.capability.profile
    profile.enroll(
        owned.pair.worktree,
        worktree_id=old.worktree_id,
        repository_id=old.repository_id,
        target_ref="refs/heads/integration",
    )
    owned.candidate_profile = CandidateProfile(
        profile, old.worktree_id, old.repository_id, old.root
    )
    owned.candidates = CandidateService(
        Journal(old.root.parent / "journal.sqlite"),
        owned.candidate_profile,
        CoreCandidateAuthority(owned.r.core, owned.r.handle, owned.runtime.resources, owned.pair),
        process_factory=CandidateProcesses,
    )
    from ai_task_manager.adapters.proofs.router import EvidenceRouter

    owned.r.core.context.evidence = EvidenceRouter(
        [
            route
            for route in owned.r.core.context.evidence.routes
            if route.prefix != "merge-publication:"
        ]
    )
    return compose(owned)


@pytest.mark.parametrize("stage", ["checks", "publication", "delivery"])
def test_crashed_controller_consumes_original_candidate_and_checks(tmp_path, stage):
    owned = Provisioned(tmp_path / "owned")
    first = resumed = None
    child = None
    try:
        owned.prepare()
        merge_store = owned.candidates.store.journal
        check_store = owned.capability.checks.checks.store
        history = {
            "merge": {
                "path": str(merge_store.path),
                "device": merge_store.identity[0],
                "inode": merge_store.identity[1],
                "store_id": merge_store.store_id,
            },
            "checks": {
                "path": str(check_store.path),
                "device": check_store.file_identity[0],
                "inode": check_store.file_identity[1],
                "store_id": check_store.store_id,
            },
        }
        owned.context = replace(owned.context, code_history=history)
        first = compose(owned)
        marker = tmp_path / "crash.json"

        class Crash:
            def hit(self, name):
                if name == "after_" + stage:
                    marker.write_text(name)
                    os._exit(71)

        first.faults = Crash()
        child = multiprocessing.get_context("fork").Process(
            target=lambda: first.run(owned.task.id, first.plan.id, "crashed-original")
        )
        child.start()
        child.join(45)
        assert not child.is_alive() and child.exitcode == 71
        assert marker.read_text() == "after_" + stage
        ref = next(
            row["ref"]
            for row in owned.candidates.store.journal.for_attempt(owned.attempt.id)
            if "merge_workflow" in row["facts"]
        )
        before = first.store.read(ref)
        before_task = owned.r.core.get_task(owned.r.handle, owned.task.id)
        if stage == "delivery":
            assert not before_task.delivered_once and before_task.sync_state == "pending"
            assert before_task.desired_status == "In Dev"
            assert before["steps"]["publication"]["result"]["publication"] == "committed"
            assert before["steps"]["delivery"]["result"] is None
        original_commit = git(owned.pair.worktree, "rev-parse", "HEAD")
        original_db = owned.pair.database.stat().st_ino
        original_checks = owned.journal._intents()
        assert original_checks
        resumed = reopen(owned)

        # This component port verifies the actual child handle. Installed controls
        # separately exercise the product's signed controller-lineage verifier.
        def dead(owner):
            assert owner["pid"] == child.pid and child.exitcode == 71
            assert before["owner"] == owner

        resumed.recover_claim = resumed._recover_claim_bound = dead
        result = resumed.resume(ref)
        assert result["data"]["state"] == "done", result
        after = resumed.store.read(ref)
        assert after["request"] == before["request"]
        assert after["owner_history"] == [before["owner"]]
        assert after["steps"]["candidate"] == before["steps"]["candidate"]
        assert after["steps"]["check_plan"] == before["steps"]["check_plan"]
        assert git(owned.pair.worktree, "rev-parse", "HEAD") == original_commit
        assert owned.pair.database.stat().st_ino == original_db
        assert git(owned.runtime.repo, "show", "integration:src/a.txt") == "4"
        assert owned.journal._intents() == original_checks
        assert len(owned.runtime.runtime.workers) == 1
    finally:
        if child is not None and child.is_alive():
            child.kill()
            child.join(5)
        if resumed is not None:
            resumed.close()
        if first is not None:
            first.close()
        owned.close()


def test_retained_code_stores_do_not_create_or_adopt_history(tmp_path):
    from ai_task_manager.domains.work.errors import AccessDenied
    from ai_task_manager.storage.checks import CheckStore

    for name, factory, identity_name in (
        ("merge", Journal, "identity"),
        ("checks", CheckStore, "file_identity"),
    ):
        path = tmp_path / (name + ".sqlite")
        store = factory(path)
        identity = getattr(store, identity_name)
        binding = {
            "path": str(path),
            "device": identity[0],
            "inode": identity[1],
            "store_id": store.store_id,
        }
        reopened = factory.open_existing(binding)
        assert reopened.store_id == store.store_id
        retained = path.with_suffix(".retained")
        path.rename(retained)
        with pytest.raises(AccessDenied, match="unavailable"):
            factory.open_existing(binding)
        assert not path.exists()
        foreign = factory(path)
        before = path.read_bytes()
        with pytest.raises(AccessDenied):
            factory.open_existing(binding)
        assert path.read_bytes() == before
        assert foreign.store_id != store.store_id and retained.exists()
