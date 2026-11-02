"""Confined actual Git component evidence; not Core/native or legacy acceptance."""

import json
import os
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from f1_fixtures import FaultOnce, Rig

from ai_task_manager.adapters.projects.ai_index.git_profile import GitProfile
from ai_task_manager.adapters.projects.ai_index.local_git import LocalGit
from ai_task_manager.domains.work.errors import AccessDenied, IdempotencyConflict


@pytest.fixture
def rig(tmp_path):
    binary = os.environ["F1_SELECTED_GIT"]
    r = Rig(tmp_path / "owned", binary)
    yield r
    r.verify_closed()


@pytest.fixture
def ff(tmp_path):
    r = Rig(tmp_path / "owned-ff", os.environ["F1_SELECTED_GIT"], mode="fast_forward")
    yield r
    r.verify_closed()


def test_actual_cas_and_journal_order(rig):
    result = rig.publish()
    assert result["publication"] == "committed" and result["quiescent"]
    assert rig.dev() == rig.new
    stages = [e["kind"] for e in rig.events()]
    assert (
        stages.index("git_intent")
        < stages.index("git_process")
        < stages.index("git_released")
        < stages.index("git_observed")
    )
    assert result["process_observation"]["returncode"] == 0
    assert result["output"]["bounded"]
    rig.operations.observations = rig.adapter
    op = rig.journal.current(rig.ref)
    rig.operations.observe(rig.ref, op["version"], "real-git-observation", result["evidence_ref"])
    assert rig.journal.current(rig.ref)["facts"]["publication"]["new"] == rig.new


def test_actual_fast_forward(ff):
    result = ff.publish()
    assert result["publication"] == "committed" and result["quiescent"]
    assert ff.dev() == ff.new
    assert (ff.repo / "a.txt").read_text() == "new\n"


@pytest.mark.parametrize(
    "kind",
    ["unrelated", "tracked-conflict", "untracked-collision", "staged-equal", "staged-conflict"],
)
def test_fast_forward_dirty_safeguards(ff, kind):
    if kind == "unrelated":
        (ff.repo / "other.txt").write_text("keep me\n")
    elif kind == "untracked-collision":
        # The candidate adds a path absent from dev.
        ff.git("checkout", "-q", "candidate")
        (ff.repo / "extra.txt").write_text("candidate\n")
        ff.git("add", "extra.txt")
        ff.git("commit", "-qm", "CG-0: add authored path")
        new = ff.git("rev-parse", "HEAD").stdout.decode().strip()
        ff.git("checkout", "-q", "dev")
        op = ff.journal.current(ff.ref)
        facts = dict(
            op["facts"],
            publication_intent=dict(op["facts"]["publication_intent"], new=new, checked=new),
        )
        ff.journal.commit(
            ff.ref, op["version"], "new-fixture-candidate", "fixture_candidate", facts
        )
        ff.request = replace(ff.request, new=new, checked=new)
        (ff.repo / "extra.txt").write_text("foreign work\n")
    else:
        (ff.repo / "a.txt").write_text("new\n" if kind == "staged-equal" else "keep me\n")
        if kind.startswith("staged"):
            ff.git("add", "a.txt")
    before = {p.name: p.read_bytes() for p in ff.repo.iterdir() if p.is_file()}
    result = ff.publish()
    if kind in {"unrelated", "staged-equal"}:
        assert result["publication"] == "committed", result
        if kind == "unrelated":
            assert (ff.repo / "other.txt").read_bytes() == before["other.txt"]
    else:
        assert result["publication"] == "refused", result
        assert ff.dev() == ff.old
        for name, body in before.items():
            assert (ff.repo / name).read_bytes() == body


@pytest.mark.parametrize(
    "key,value",
    [
        ("filter.fixture.clean", "false"),
        ("merge.fixture.driver", "false"),
        ("core.fsmonitor", "/unadmitted"),
        ("maintenance.auto", "true"),
        ("merge.autoStash", "true"),
        ("include.path", "/unadmitted"),
        ("core.hooksPath", "/unadmitted"),
        ("remote.origin.url", "https://invalid.invalid/repo"),
        ("extensions.partialClone", "fixture"),
        ("core.sparseCheckout", "true"),
    ],
)
def test_unsupported_configuration_refuses_before_effect(rig, key, value):
    rig.git("config", key, value)
    with pytest.raises(AccessDenied):
        rig.publish()
    assert not rig.journal.current(rig.ref)["facts"].get("local_git")
    assert rig.dev() == rig.old


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "target",
        "dirty",
        "active-rebase",
        "symbolic-target",
        "symbolic-source",
        "attributes",
        "alternates",
    ],
)
def test_changed_or_unsupported_inputs(rig, change):
    if change == "source":
        rig.git("update-ref", "refs/heads/candidate", rig.old)
    elif change == "target":
        rig.git("update-ref", "refs/heads/dev", rig.new)
    elif change == "dirty":
        (rig.repo / "a.txt").write_text("unfinished\n")
    elif change == "active-rebase":
        (rig.repo / ".git/rebase-merge").mkdir()
    elif change.startswith("symbolic"):
        rig.git("branch", "foreign", rig.old)
        ref = "refs/heads/dev" if change.endswith("target") else "refs/heads/candidate"
        rig.git("symbolic-ref", ref, "refs/heads/foreign")
    elif change == "attributes":
        (rig.repo / ".gitattributes").write_text("* filter=fixture\n")
    else:
        (rig.repo / ".git/objects/info/alternates").write_text(str(rig.foreign) + "\n")
    with pytest.raises(AccessDenied):
        rig.publish()
    assert not rig.journal.current(rig.ref)["facts"].get("local_git")


def test_reference_hook_is_actually_excluded(rig):
    marker = rig.root / "hook-marker"
    hook = rig.repo / ".git/hooks/reference-transaction"
    hook.write_text('#!/bin/sh\nprintf x >> "' + str(marker) + '"\n')
    hook.chmod(0o700)
    rig.git("update-ref", "refs/heads/control", rig.old)
    baseline = marker.read_bytes()
    assert baseline
    result = rig.publish()
    assert result["publication"] == "committed"
    assert marker.read_bytes() == baseline


def test_lost_reply_then_recovery_and_no_duplicate(rig):
    rig.adapter.faults = FaultOnce("after_effect_observation")
    first = rig.publish()
    assert first["publication"] == "committed"
    op = rig.journal.current(rig.ref)
    marker = op["facts"]["local_git"]["marker"]
    history = (rig.repo / ".git/logs/refs/heads/dev").read_bytes()
    second = rig.publish()
    assert second["publication"] == "committed"
    assert (rig.repo / ".git/logs/refs/heads/dev").read_bytes() == history
    assert history.count(marker.encode()) == 1


def test_later_target_advance_preserves_exact_effect(rig):
    def advance():
        deadline = time.monotonic() + 5
        while rig.dev() != rig.new:
            assert time.monotonic() < deadline, "actual publication was not observed"
            time.sleep(0.01)
        rig.git("update-ref", "refs/heads/dev", rig.old, rig.new)

    rig.adapter.faults = FaultOnce("after_release", advance)
    result = rig.publish()
    assert result["publication"] == "committed"
    assert result["old"] == rig.old and result["new"] == rig.new
    assert result["current_dev"] == rig.old


def test_gate_closed_before_effect_on_controller_exception(rig):
    rig.adapter.faults = FaultOnce("before_release")
    result = rig.publish()
    assert result["publication"] == "refused" and result["quiescent"]
    assert result["process_observation"]["returncode"] == 97
    assert rig.dev() == rig.old


def test_target_move_at_final_validation(rig):
    rig.adapter.faults = FaultOnce(
        "before_final_validation", lambda: rig.git("update-ref", "refs/heads/dev", rig.new)
    )
    result = rig.publish()
    assert result["publication"] == "uncertain"
    assert not result["quiescent"]
    assert not rig.journal.current(rig.ref)["facts"]["local_git"].get("process")


def test_missing_boundary_is_rejected(rig):
    adapter = LocalGit(rig.journal, rig.profile)
    with pytest.raises(AccessDenied):
        adapter.publish(
            rig.ref, rig.journal.current(rig.ref)["version"], "missing-boundary", rig.request
        )
    assert rig.dev() == rig.old


def test_conflicting_request_and_foreign_evidence(rig):
    result = rig.publish()
    with pytest.raises(IdempotencyConflict):
        rig.adapter.publish(
            rig.ref,
            rig.journal.current(rig.ref)["version"],
            "other",
            replace(rig.request, range_ref="other"),
        )
    with pytest.raises(AccessDenied):
        rig.adapter.read(result["evidence_ref"], replace(rig.binding, principal_id="foreign"))


def test_profile_rejects_foreign_repository_and_changed_binary(rig):
    with pytest.raises(AccessDenied):
        rig.profile.enroll(rig.foreign.parent, worktree_id="foreign", common_git_id="foreign")
    rig.profile.executable = replace(rig.executable, sha256="0" * 64)
    with pytest.raises(AccessDenied):
        rig.publish()
    rig.profile.executable = rig.executable
    assert rig.dev() == rig.old


def test_actual_contending_cas_writers(rig):
    from uuid import uuid4

    from ai_task_manager.adapters.projects.ai_index.operations import Operations
    from ai_task_manager.adapters.projects.ai_index.registrations import Registrations

    other_binding = replace(rig.binding, attempt_id=str(uuid4()), operation_key=str(uuid4()))
    other_ref = Registrations(rig.journal, rig.authority).register(other_binding)["ref"]
    other_op = rig.journal.current(other_ref)
    original_facts = {
        k: v for k, v in rig.journal.current(rig.ref)["facts"].items() if k != "local_git"
    }
    rig.journal.commit(
        other_ref,
        other_op["version"],
        "other-preconditions",
        "fixture_preconditions",
        original_facts,
    )
    other = LocalGit(rig.journal, rig.profile, Operations(rig.journal, rig.authority))
    barrier = threading.Barrier(2, timeout=10)
    rig.adapter.faults = FaultOnce("before_release", barrier.wait)
    other.faults = FaultOnce("before_release", barrier.wait)
    results = []

    def call(adapter, ref):
        results.append(
            adapter.publish(
                ref, rig.journal.current(ref)["version"], "contending-publication", rig.request
            )
        )

    threads = [
        threading.Thread(target=call, args=(rig.adapter, rig.ref)),
        threading.Thread(target=call, args=(other, other_ref)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
        assert not t.is_alive()
    assert len(results) == 2
    assert sum(r["publication"] == "committed" for r in results) == 1, results
    assert all(r["quiescent"] for r in results), results
    assert not other._live
    assert rig.dev() == rig.new
    history = (rig.repo / ".git/logs/refs/heads/dev").read_bytes()
    assert history.count(b"ai-task-manager:") == 1


def test_interrupted_intent_does_not_relaunch(rig):
    rig.adapter.faults = FaultOnce("after_intent")
    with pytest.raises(RuntimeError):
        rig.publish()
    assert rig.dev() == rig.old
    assert rig.journal.current(rig.ref)["facts"]["local_git"]["stage"] == "intent"
    result = rig.publish()
    assert result["publication"] == "uncertain" and not result["quiescent"]
    assert rig.dev() == rig.old


def test_numeric_hash_format_and_missing_object(rig):
    from ai_task_manager.adapters.projects.ai_index.git_profile import oid

    assert oid("1" * 40) == "1" * 40
    request = replace(rig.request, new="1" * 40, checked="1" * 40)
    with pytest.raises(AccessDenied):
        rig.adapter.publish(
            rig.ref, rig.journal.current(rig.ref)["version"], "missing-object", request
        )
    assert rig.dev() == rig.old


@pytest.mark.parametrize("pointer", ["gitdir", "commondir"])
def test_current_linked_worktree_routing_is_rebound(rig, pointer):
    import shutil

    linked = rig.root / "linked"
    rig.git("worktree", "add", "-q", "--detach", str(linked), rig.new)
    rig.profile.enroll(
        linked, worktree_id=rig.binding.worktree_id, common_git_id=rig.binding.common_git_id
    )
    original_metadata = Path((linked / ".git").read_text()[8:].strip())
    original_common = rig.repo / ".git"
    alternate = rig.root / "alternate-git"
    shutil.copytree(original_common, alternate)
    if pointer == "gitdir":
        # The old enrolled metadata stays present and has matching refs.
        other_metadata = alternate / "worktrees" / original_metadata.name
        (other_metadata / "commondir").write_text("../..\n")
        (linked / ".git").write_text("gitdir: " + str(other_metadata) + "\n")
    else:
        (original_metadata / "commondir").write_text(str(alternate) + "\n")
    assert original_metadata.is_dir() and original_common.is_dir()
    with pytest.raises(AccessDenied, match="routing changed"):
        rig.publish()
    assert not rig.journal.current(rig.ref)["facts"].get("local_git")
    assert (original_common / "refs/heads/dev").read_text().strip() == rig.old
    assert (alternate / "refs/heads/dev").read_text().strip() == rig.old


def test_routing_rewrite_after_spawn_cannot_redirect_effect(rig):
    import shutil

    original = rig.repo / ".git"
    # Move metadata without changing its identity, and enroll its explicit pointer.
    metadata = rig.root / "original-metadata"
    original.rename(metadata)
    original.write_text("gitdir: " + str(metadata) + "\n")
    rig.profile = GitProfile(root=rig.root, executable=rig.executable, home=rig.home)
    rig.profile.enroll(
        rig.repo, worktree_id=rig.binding.worktree_id, common_git_id=rig.binding.common_git_id
    )
    rig.adapter.profile = rig.profile
    alternate = rig.root / "alternate-metadata"
    shutil.copytree(metadata, alternate)
    rig.adapter.faults = FaultOnce(
        "before_release", lambda: original.write_text("gitdir: " + str(alternate) + "\n")
    )
    result = rig.publish()
    assert result["quiescent"] and result["profile_error"] == "AccessDenied"
    assert result["publication"] == "uncertain"
    assert (metadata / "refs/heads/dev").read_text().strip() == rig.new
    assert (alternate / "refs/heads/dev").read_text().strip() == rig.old
    assert not rig.adapter._live


def test_checked_out_second_writer_before_prepare_cannot_publish(ff):
    # A real independent Git child changes dev after validation, while the gated
    # transaction child exists but has not run its expected-old prepare.
    ff.adapter.faults = FaultOnce(
        "before_release", lambda: ff.git("update-ref", "refs/heads/dev", ff.new, ff.old)
    )
    before = (ff.repo / "a.txt").read_bytes()
    result = ff.publish()
    assert result["publication"] != "committed" and result["quiescent"]
    assert result["checkout_state"] == "not_started" and not result["repair_required"]
    assert (ff.repo / "a.txt").read_bytes() == before
    data = ff.journal.current(ff.ref)["facts"]["local_git"]
    assert [c["kind"] for c in data["children"]] == ["ref_transaction"]
    assert "cannot lock ref" in data["children"][0]["output"]["stderr_excerpt"]
    assert data["marker"].encode() not in (ff.repo / ".git/logs/refs/heads/dev").read_bytes()


def test_prepared_transaction_holds_actual_ref_lock(ff):
    def contend():
        child = ff.git("update-ref", "refs/heads/dev", ff.new, ff.old, ok=(0, 128))
        assert child.returncode == 128 and b"lock" in child.stderr
        assert ff.dev() == ff.old

    ff.adapter.faults = FaultOnce("after_prepare", contend)
    result = ff.publish()
    assert result["publication"] == "committed" and result["quiescent"]
    assert not result["repair_required"]
    assert ff.dev() == ff.new and (ff.repo / "a.txt").read_text() == "new\n"
    data = ff.journal.current(ff.ref)["facts"]["local_git"]
    assert len(data["children"]) == 2
    assert all(c["quiescence"]["quiescent"] for c in data["children"])


@pytest.mark.parametrize("stage", ["after_checkout_release", "after_checkout_before_commit"])
def test_interrupted_checkout_keeps_repair_obligation(ff, stage):
    ff.adapter.faults = FaultOnce(stage)
    result = ff.publish()
    assert result["quiescent"] and result["publication"] != "committed"
    assert result["repair_required"]
    assert result["checkout_state"] in {"begun", "updated"}
    assert ff.dev() == ff.old
    if stage == "after_checkout_before_commit":
        assert (ff.repo / "a.txt").read_text() == "new\n"
        assert ff.git("diff", "--cached", "--name-only").stdout.strip() == b"a.txt"
    # No automatic cleanup/reset/relaunch on replay.
    index = (ff.repo / ".git/index").read_bytes()
    assert ff.publish()["repair_required"]
    assert (ff.repo / ".git/index").read_bytes() == index


def test_real_large_checkout_error_drains_while_running(ff):
    ff.git("checkout", "-q", "candidate")
    names = ["collision-" + str(n).zfill(4) + "-" + "x" * 180 for n in range(1200)]
    for name in names:
        (ff.repo / name).write_text("candidate\n")
    ff.git("add", ".")
    ff.git("commit", "-qm", "CG-0: authored large rejection")
    new = ff.git("rev-parse", "HEAD").stdout.decode().strip()
    ff.git("checkout", "-q", "dev")
    op = ff.journal.current(ff.ref)
    facts = dict(
        op["facts"],
        publication_intent=dict(op["facts"]["publication_intent"], new=new, checked=new),
    )
    ff.journal.commit(ff.ref, op["version"], "large-fixture-candidate", "fixture_candidate", facts)
    ff.request = replace(ff.request, new=new, checked=new)
    for name in names:
        (ff.repo / name).write_text("owned sentinel\n")
    result = ff.publish()
    assert result["quiescent"] and result["publication"] == "refused", result
    assert result["output"]["bounded"] and result["repair_required"]
    assert ff.dev() == ff.old
    assert all((ff.repo / name).read_text() == "owned sentinel\n" for name in names)
    # These selected publication primitives report bounded conflict lists. An
    # authored read-only apply check supplies the actual large-error drain control;
    # it does not invoke the supported publication path or apply the patch.
    patch = ff.root / "authored-check.patch"
    chunks = []
    for n, name in enumerate(names):
        path = "missing-" + name
        chunks.append(
            "diff --git a/"
            + path
            + " b/"
            + path
            + "\n--- a/"
            + path
            + "\n+++ b/"
            + path
            + "\n@@ -1 +1 @@\n-old\n+new\n"
        )
    patch.write_text("".join(chunks))
    control = ff.drain_control(["apply", "--check", str(patch)])
    assert control["quiescence"]["quiescent"] and control["quiescence"]["returncode"] != 0
    assert control["stderr"]["bytes"] > 65536, "large real Git error was not exercised"
    assert control["stderr"]["retained_bytes"] <= 1024 * 1024
    assert ff.dev() == ff.old


def test_bounded_capture_counts_discarded_bytes():
    from ai_task_manager.adapters.projects.ai_index.git_profile import MAX_OUTPUT
    from ai_task_manager.adapters.projects.ai_index.local_git import _Drain

    read, write = os.pipe()
    drain = _Drain(os.fdopen(read, "rb", buffering=0))
    size = MAX_OUTPUT + 131072

    def emit():
        try:
            remaining = size
            while remaining:
                chunk = b"x" * min(32768, remaining)
                count = os.write(write, chunk)
                remaining -= count
        finally:
            os.close(write)

    thread = threading.Thread(target=emit)
    thread.start()
    thread.join(5)
    drain.thread.join(5)
    assert not thread.is_alive() and not drain.thread.is_alive()
    assert drain.snapshot() == {
        "bytes": size,
        "retained_bytes": MAX_OUTPUT,
        "complete": True,
        "error": None,
    }


def test_head_switch_before_prepare_prevents_checkout(ff):
    ff.git("branch", "foreign", ff.old)
    ff.adapter.faults = FaultOnce(
        "before_release", lambda: ff.git("symbolic-ref", "HEAD", "refs/heads/foreign")
    )
    before = (ff.repo / "a.txt").read_bytes()
    result = ff.publish()
    assert result["quiescent"] and result["publication"] != "committed"
    assert result["checkout_state"] == "not_started" and not result["repair_required"]
    assert (ff.repo / "a.txt").read_bytes() == before and ff.dev() == ff.old
    assert ff.git("symbolic-ref", "HEAD").stdout.strip() == b"refs/heads/foreign"


def test_prepared_head_lock_refuses_real_head_switch(ff):
    ff.git("branch", "foreign", ff.old)
    positive = ff.git("symbolic-ref", "HEAD", "refs/heads/foreign")
    assert positive.returncode == 0
    assert ff.git("symbolic-ref", "HEAD").stdout.strip() == b"refs/heads/foreign"
    ff.git("symbolic-ref", "HEAD", "refs/heads/dev")
    observed = []

    def switch():
        attempted = ff.git("symbolic-ref", "HEAD", "refs/heads/foreign", ok=(0, 1, 128))
        observed.append(
            (attempted.returncode, attempted.stderr, ff.git("symbolic-ref", "HEAD").stdout.strip())
        )

    ff.adapter.faults = FaultOnce("after_prepare", switch)
    result = ff.publish()
    assert len(observed) == 1
    code, stderr, head = observed[0]
    assert code != 0 and b"HEAD.lock" in stderr and b"File exists" in stderr, observed
    assert head == b"refs/heads/dev"
    assert result["publication"] == "committed" and result["quiescent"]
    assert ff.git("symbolic-ref", "HEAD").stdout.strip() == b"refs/heads/dev"
    (ff.root / "head-lock-control.json").write_text(
        json.dumps(
            {
                "positive_returncode": positive.returncode,
                "identical_positive_command": True,
                "returncode": code,
                "stderr": stderr[:4096].decode("utf-8", "replace"),
                "head": head.decode(),
                "selected_git": ff.executable.version,
            },
            indent=2,
        )
        + "\n"
    )


def test_fast_forward_preserves_orig_head_undo_value(ff):
    (ff.repo / ".git/ORIG_HEAD").write_text(ff.new + "\n")
    result = ff.publish()
    assert result["publication"] == "committed" and result["quiescent"]
    assert ff.git("rev-parse", "ORIG_HEAD").stdout.decode().strip() == ff.old
    assert ff.git("rev-parse", "HEAD").stdout.decode().strip() == ff.new


def test_orig_head_second_writer_before_prepare_prevents_checkout(ff):
    # The previous undo value is read before child release; its exact old value is
    # checked by the same prepared transaction rather than overwritten blindly.
    ff.adapter.faults = FaultOnce(
        "before_release", lambda: ff.git("update-ref", "ORIG_HEAD", ff.new)
    )
    before = (ff.repo / "a.txt").read_bytes()
    result = ff.publish()
    assert result["quiescent"] and result["publication"] != "committed"
    assert result["checkout_state"] == "not_started" and not result["repair_required"]
    assert (ff.repo / "a.txt").read_bytes() == before and ff.dev() == ff.old
    assert ff.git("rev-parse", "ORIG_HEAD").stdout.decode().strip() == ff.new
