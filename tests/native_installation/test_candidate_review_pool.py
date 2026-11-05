"""Sealed candidate capacity uses actual scoped Core and signed SQLite slot facts."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.workflow_control.identity import file_binding
from ai_task_manager.adapters.workflow_reviews.models import Inspection
from ai_task_manager.config.files import digest, write_private
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.native import NativeManifest
from ai_task_manager.installation.native.manifest import installation_intent
from ai_task_manager.installation.native.reviewer import NormalReviewerFactory
from tests.native_installation.helpers import refresh_digest
from tests.native_installation.test_code_review_stages import configured
from tests.runtime.helpers import RuntimeRig


@pytest.fixture
def pooled(tmp_path):
    manifest, slots = configured(tmp_path)
    runtime = RuntimeRig(tmp_path / "runtime")
    try:
        r = runtime.rig
        task = r.queued(code=True)
        dispatch = runtime.start(task)
        runtime.admit(dispatch)
        runtime.supervisor.execute(
            r.handle, dispatch, runtime.command(dispatch), operation_key=r.key()
        )
        task = r.core.get_task(r.handle, task.id)
        reviewers = [r.queued(code=False) for _ in range(3)]
        paths = [
            slots["code_worker"].installation,
            slots["code_candidate"].installation,
            tmp_path / "candidate-second.json",
        ]
        pointers = []
        template = json.loads(paths[1].read_text())
        for i, (path, reviewer) in enumerate(zip(paths, reviewers)):
            stage = "code_worker" if i == 0 else "code_candidate"
            spec = json.loads(path.read_text()) if path.exists() else dict(template)
            spec.update(
                task_id=reviewer.id,
                prose_digest=reviewer.prose_digest,
                scope=asdict(r.scope),
                installation_path=str(path),
                state_root=str(tmp_path / ("review-state-" + str(i))),
                review_target={
                    "task_id": task.id,
                    "prose_digest": task.prose_digest,
                    "stage": stage,
                },
            )
            write_private(path, spec, replace=path.exists())
            pointers.append(file_binding(path))
        spec = json.loads(manifest.installation.path.read_text())
        spec.update(
            task_id=task.id,
            prose_digest=task.prose_digest,
            scope=asdict(r.scope),
            reviewer_installation={"code_worker": pointers[0], "code_candidate": pointers[1:]},
        )
        write_private(manifest.installation.path, spec, replace=True)
        budget = tmp_path / "sealed-attempts.json"
        write_private(budget, {"attempts": 2})
        data = json.loads(manifest.seal.path.read_text())
        data.update(
            installation=installation_intent(manifest.installation.path),
            code_manifest={"path": str(budget), "sha256": file_binding(budget)["sha256"]},
        )
        write_private(manifest.seal.path, data, replace=True)
        refresh_digest(manifest.installation.path, manifest.seal.path, data)
        manifest = NativeManifest(
            manifest.seal.path, manifest.config.path, manifest.installation.path
        )
        root = Path(spec["state_root"])
        root.mkdir(mode=0o700)
        journal = HostJournal(root / "host.sqlite", root / "host.key")
        service = SimpleNamespace(core=r.core, handle=r.handle, scope=r.scope)

        def inspection(name):
            return Inspection(
                json.dumps(
                    {
                        "review_kind": "code_candidate",
                        "task_id": task.id,
                        "prose_digest": task.prose_digest,
                        "scope": asdict(r.scope),
                        "context": name,
                    }
                ),
                (),
            )

        yield SimpleNamespace(
            runtime=runtime,
            manifest=manifest,
            service=service,
            journal=journal,
            inspection=inspection,
            pointers=pointers,
            r=r,
            task=task,
            budget=budget,
        )
    finally:
        runtime.close()


def test_pool_assigns_distinct_contexts_and_cold_replay_never_another_slot(pooled):
    a, b = pooled.inspection("target-a"), pooled.inspection("target-b")
    factory = NormalReviewerFactory(pooled.manifest)
    first = factory._selected(a, service=pooled.service)
    second = factory._selected(b, service=pooled.service)
    assert first[0] == pooled.pointers[1] and second[0] == pooled.pointers[2]
    restarted = NormalReviewerFactory(pooled.manifest)
    assert restarted._selected(a, service=pooled.service) == first
    assert restarted._selected(b, service=pooled.service) == second
    assert len(pooled.journal._intents()) == 2
    with pytest.raises(AccessDenied, match="exhausted"):
        restarted._selected(pooled.inspection("target-c"), service=pooled.service)
    assert len(pooled.journal._intents()) == 2 and restarted.producers == {}


def test_two_factory_owners_atomically_reserve_different_slots(pooled):
    factories = [NormalReviewerFactory(pooled.manifest) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(f._selected, pooled.inspection(str(i)), service=pooled.service)
            for i, f in enumerate(factories)
        ]
        values = [f.result(timeout=5) for f in futures]
    assert len({v[0]["path"] for v in values}) == 2
    assert len(pooled.journal._intents()) == 2


def test_same_context_concurrent_owners_keep_one_durable_slot(pooled):
    factories = [NormalReviewerFactory(pooled.manifest) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(f._selected, pooled.inspection("same"), service=pooled.service)
            for f in factories
        ]
        values = [f.result(timeout=5) for f in futures]
    assert values[0] == values[1]
    assert len(pooled.journal._intents()) == 1


def test_foreign_scope_refuses_before_any_slot_intent(pooled):
    scope = replace(pooled.r.scope, logical_session="foreign")
    service = SimpleNamespace(core=pooled.r.core, handle=pooled.r.session(scope), scope=scope)
    with pytest.raises(AccessDenied):
        NormalReviewerFactory(pooled.manifest)._selected(pooled.inspection("x"), service=service)
    assert not pooled.journal._intents()


def test_fenced_author_refuses_before_any_slot_intent(pooled):
    pooled.r.transition(pooled.task, "On Hold", "hold")
    with pytest.raises(AccessDenied):
        NormalReviewerFactory(pooled.manifest)._selected(
            pooled.inspection("x"), service=pooled.service
        )
    assert not pooled.journal._intents()


def test_signed_slot_with_wrong_physical_binding_is_not_adopted(pooled):
    pointer = pooled.pointers[1]
    binding = file_binding(pooled.manifest.installation.path)
    key = "code-candidate-slot:" + digest([binding, 0])
    pooled.journal.intend(
        key,
        {
            "version": 1,
            "author": binding,
            "pool": digest(pooled.pointers[1:]),
            "context_digest": "old",
            "pointer": dict(pointer, inode=pointer["inode"] + 1),
        },
    )
    with pytest.raises(AccessDenied, match="slot binding changed"):
        NormalReviewerFactory(pooled.manifest)._selected(
            pooled.inspection("x"), service=pooled.service
        )
    assert len(pooled.journal._intents()) == 1


def test_pool_exceeding_sealed_attempts_refuses_before_runtime(pooled):
    write_private(pooled.budget, {"attempts": 1}, replace=True)
    data = json.loads(pooled.manifest.seal.path.read_text())
    data["code_manifest"]["sha256"] = file_binding(pooled.budget)["sha256"]
    write_private(pooled.manifest.seal.path, data, replace=True)
    refresh_digest(pooled.manifest.installation.path, pooled.manifest.seal.path, data)
    manifest = NativeManifest(
        pooled.manifest.seal.path, pooled.manifest.config.path, pooled.manifest.installation.path
    )
    with pytest.raises(AccessDenied, match="sealed attempt budget"):
        NormalReviewerFactory(manifest)
    assert not pooled.journal._intents()


def test_active_prior_reviewer_blocks_another_context_without_slot_reuse(pooled):
    factory = NormalReviewerFactory(pooled.manifest)
    pointer, _ = factory._selected(pooled.inspection("old"), service=pooled.service)
    spec = json.loads(Path(pointer["path"]).read_text())
    reviewer = pooled.r.core.get_task(pooled.r.handle, spec["task_id"])
    # Honor the queue: explicitly dequeue the unused earlier worker-review
    # fixture task before admitting this isolated candidate role.
    worker_spec = json.loads(Path(pooled.pointers[0]["path"]).read_text())
    worker = pooled.r.core.get_task(pooled.r.handle, worker_spec["task_id"])
    pooled.r.transition(worker, "Backlog", "dequeue")
    pooled.runtime.start(reviewer)
    with pytest.raises(AccessDenied, match="active or unresolved"):
        factory._selected(pooled.inspection("new"), service=pooled.service)
    assert len(pooled.journal._intents()) == 1
