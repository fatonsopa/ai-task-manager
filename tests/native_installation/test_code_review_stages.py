"""Closed stage selection and Core-only lease checks; no model acceptance claim."""

import json
import time
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.workflow_control.identity import file_binding
from ai_task_manager.adapters.workflow_reviews.models import Inspection
from ai_task_manager.config.files import write_private
from ai_task_manager.domains.work.errors import AccessDenied, SchemaError
from ai_task_manager.domains.work.models import Scope
from ai_task_manager.installation.native import NativeManifest
from ai_task_manager.installation.native.manifest import installation_intent
from ai_task_manager.installation.native.review_stages import CodeStageSource
from ai_task_manager.installation.native.reviewer import NormalReviewerFactory
from tests.native_installation.helpers import bundle, refresh_digest
from tests.runtime.helpers import RuntimeRig


def configured(root, fault=None):
    author = bundle(root / "author", "claude", "claude-opus-5-5", task_id="author")
    scope = asdict(Scope("linear", "workspace", "principal", "project", "session"))
    deadline = time.time() + 120
    repository = root / "repository"
    repository.mkdir(mode=0o700)
    slots = {}
    for stage in ("code_worker", "code_candidate"):
        b = bundle(root / stage, "claude", "claude-opus-5-5", task_id=stage)
        spec = {
            "version": 1,
            "config": file_binding(author.config),
            "scope": scope,
            "task_id": stage,
            "prose_digest": "b" * 64,
            "native_ref": "normal-native",
            "native_digest": "c" * 64,
            "native_manifest": str(b.manifest),
            "repository": str(repository),
            "source_head": "1" * 40,
            "state_root": str(b.root / "state"),
            "deadline": deadline,
            "principal_ref": "installer",
            "plan_ref": stage,
            "files": ["owned.py"],
            "artifacts": ["owned.py"],
            "installation_path": str(b.installation),
            "role": "reviewer",
            "review_target": {"task_id": "author", "prose_digest": "a" * 64, "stage": stage},
        }
        if stage == "code_candidate":
            if fault == "task":
                spec["task_id"] = "code_worker"
            elif fault == "state":
                spec["state_root"] = str(root / "code_worker/state")
            elif fault == "nested":
                spec["state_root"] = str(root / "code_worker/state/nested")
            elif fault == "deadline":
                spec["deadline"] += 1
            elif fault == "role":
                spec["role"] = "worker"
            elif fault == "target":
                spec["review_target"]["stage"] = "code_worker"
            elif fault == "scope":
                spec["scope"] = dict(scope, principal="foreign")
        write_private(b.installation, spec, replace=True)
        slots[stage] = b
    spec = dict(
        spec,
        config=file_binding(author.config),
        task_id="author",
        prose_digest="a" * 64,
        state_root=str(author.root / "state"),
        deadline=deadline,
        role="worker",
        plan_ref="author",
        installation_path=str(author.installation),
        native_manifest=str(author.manifest),
        reviewer_installation={stage: file_binding(b.installation) for stage, b in slots.items()},
    )
    spec.pop("review_target")
    spec["scope"] = scope
    write_private(author.installation, spec, replace=True)
    author.data["installation"] = installation_intent(author.installation)
    write_private(author.manifest, author.data, replace=True)
    refresh_digest(author.installation, author.manifest, author.data)
    return NativeManifest(author.manifest, author.config, author.installation), slots


def test_stage_map_selects_only_its_exact_slot(tmp_path):
    manifest, slots = configured(tmp_path)
    factory = NormalReviewerFactory(manifest)
    for stage in slots:
        pointer, selected = factory._selected(Inspection(json.dumps({"review_kind": stage}), ()))
        assert pointer == file_binding(slots[stage].installation) and selected == stage
    assert factory.producers == {}
    for stage in (None, "foreign", "non_code"):
        with pytest.raises(AccessDenied):
            factory._selected(Inspection(json.dumps({"review_kind": stage}), ()))


@pytest.mark.parametrize(
    "fault", ["task", "state", "nested", "deadline", "role", "target", "scope"]
)
def test_invalid_code_stage_installation_refuses_before_runtime(tmp_path, fault):
    manifest, _ = configured(tmp_path, fault)
    with pytest.raises(AccessDenied):
        NormalReviewerFactory(manifest)
    assert not list(tmp_path.rglob("runtime.sqlite"))


def test_changed_stage_pointer_is_not_rebound(tmp_path):
    manifest, slots = configured(tmp_path)
    factory = NormalReviewerFactory(manifest)
    path = slots["code_candidate"].installation
    path.write_text(path.read_text() + "\n")
    with pytest.raises(AccessDenied):
        factory._check_pointer()
    assert factory.producers == {}


@pytest.fixture
def core_stage(tmp_path):
    runtime = RuntimeRig(tmp_path / "owned")
    try:
        dispatch = runtime.start()
        runtime.admit(dispatch)
        runtime.supervisor.execute(
            runtime.rig.handle, dispatch, runtime.command(dispatch), operation_key=runtime.rig.key()
        )
        r = runtime.rig
        task = r.core.get_task(r.handle, runtime.journal.attempt(dispatch.attempt_id)["task_id"])
        attempt = r.core.get_attempt(r.handle, dispatch.attempt_id)
        # Only the Core-only predicate is isolated here. Constructor provenance
        # and actual two-stage native composition have separate controls.
        source = object.__new__(CodeStageSource)
        source.core, source.handle, source.scope = r.core, r.handle, r.scope
        source.task, source.attempt, source.pair = task, attempt, dispatch.resources
        source.row = {
            "prose_digest": task.prose_digest,
            "decision_revision": task.decision_revision,
        }
        source.spec = SimpleNamespace(data={"deadline": r.now + 100})
        source.inspection = Inspection(
            json.dumps(
                {
                    "scope": asdict(r.scope),
                    "task_id": task.id,
                    "prose_digest": task.prose_digest,
                    "author": {
                        "attempt_id": attempt.id,
                        "worker_id": attempt.worker_id,
                        "host": attempt.host,
                    },
                }
            ),
            (),
        )
        yield r, source
    finally:
        runtime.close()


def test_stage_lease_check_stays_inside_original_core_without_file_io(core_stage, monkeypatch):
    from pathlib import Path

    r, source = core_stage
    with r.db.transaction() as connection:
        monkeypatch.setattr(
            Path, "open", lambda *a, **k: pytest.fail("No file content inside Core lease")
        )
        source.check(connection)


@pytest.mark.parametrize("change", ["task", "attempt", "scope", "context", "decision", "deadline"])
def test_stage_lease_rejects_changed_snapshot(core_stage, change):
    r, source = core_stage
    if change == "task":
        source.task = replace(source.task, version=source.task.version + 1)
    elif change == "attempt":
        source.attempt = replace(source.attempt, version=source.attempt.version + 1)
    elif change == "scope":
        source.scope = replace(source.scope, logical_session="foreign")
    elif change == "decision":
        source.row["decision_revision"] += 1
    elif change == "deadline":
        source.spec.data["deadline"] = r.now
    else:
        context = source.inspection.context
        context["author"]["worker_id"] = "foreign"
        source.inspection = Inspection(json.dumps(context), ())
    with r.db.transaction() as connection:
        with pytest.raises(AccessDenied):
            source.check(connection)


def test_stage_lease_refuses_foreign_connection(core_stage, tmp_path):
    from ai_task_manager.storage.database import Database

    _, source = core_stage
    foreign = Database(tmp_path / "foreign.sqlite")
    with foreign.transaction() as connection:
        with pytest.raises(SchemaError):
            source.check(connection)


def test_stage_refuses_replaced_canonical_head_before_history_consumption(core_stage, tmp_path):
    from ai_task_manager.domains.checks.models import canonical
    from ai_task_manager.storage.workflows import WorkflowStore

    r, source = core_stage
    source.author = SimpleNamespace(check=lambda: None)
    source.spec.check = lambda: None
    source.prepared = SimpleNamespace(check=lambda: None)
    source.store = WorkflowStore(tmp_path / "workflow.sqlite")
    source.row["id"] = "workflow:original"
    with source.store.transaction() as connection:
        connection.execute(
            "INSERT INTO workflow_heads VALUES(?,?,?)",
            (canonical(asdict(r.scope)), source.task.id, "workflow:successor"),
        )
    with pytest.raises(AccessDenied, match="replaced during review"):
        source.check()
