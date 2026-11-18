import json
from dataclasses import replace

import pytest

from ai_task_manager.adapters.workflow_reviews.capture_store import CaptureStore, NativeReviewSource
from ai_task_manager.adapters.workflow_reviews.models import Inspection
from ai_task_manager.adapters.workflow_reviews.native import ReadOnlyReviewGateway
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.installation.workflows.registry import NativeRegistry
from tests.workflow_review_capture.helpers import reviewer
from tests.workflow_reviews.helpers import ReviewRig


@pytest.fixture
def graph(tmp_path, request):
    author = ReviewRig(tmp_path / "author")
    try:
        mode = getattr(request, "param", None)
        if mode in {"large-plain", "escaped-input"}:
            author.report.write_text("a" * 150000 if mode == "large-plain" else '"' * 70000)
            author.saved = author.inspector.inspect(author.task, author.attempt, author.plan)
            from ai_task_manager.config.files import write_private

            write_private(author.store.path, author.saved, replace=True)
        inspected = author.artifacts.prepare(author.scope, author.task, author.saved["digest"])
        verdict = None
        if mode == "reject":
            verdict = {
                "context_digest": inspected.digest,
                "criteria": [
                    {"id": "request", "judgment": "reject", "reason": "Authored rejection"}
                ],
                "reason": "Actual authored rejection fixture",
            }
        elif mode == "large-plain":
            verdict = {
                "context_digest": inspected.digest,
                "criteria": [{"id": "request", "judgment": "accept", "reason": "r" * 150000}],
                "reason": "Authored large valid verdict",
            }
        elif mode == "malformed":
            verdict = {"unknown": True}
        inspection, producer = reviewer(author, tmp_path / "reviewer", verdict)
        yield author, inspection, producer, CaptureStore(tmp_path / "captures")
    finally:
        author.runtime.close()
        if "producer" in locals():
            import os
            from dataclasses import asdict
            from pathlib import Path

            report = os.environ.get("WORKFLOW_CAPTURE_REPORT")
            rows = []
            for native in producer.workers.hosts.owned.values():
                if native.process is not None:
                    if not native.process._closed:
                        assert native.stop().confirmed
                    rows.append(
                        {
                            "identity": asdict(native.process.identity),
                            "closed": native.process._closed,
                        }
                    )
            if report:
                with Path(report).open("a") as out:
                    out.write(json.dumps(rows) + "\n")


def test_RC01_actual_tracked_normal_native_capture_and_no_duplicate(graph):
    author, inspection, producer, store = graph
    captured = producer.capture(inspection, store)
    assert captured.accepted(("request",), store.resolve(inspection).expires_at - 1)
    assert captured.reviewer_id != inspection.context["author"]["worker_id"]
    assert captured.host == "claude" and captured.model == "claude-opus-5-5"
    assert captured == producer.capture(inspection, store)
    attempt = producer.workers.core.get_attempt(
        producer.handle, next(iter(producer.workers.dispatches))
    )
    assert attempt.state == "done"
    native = producer.workers.hosts.owned[attempt.id]
    assert native.process._closed
    trace = json.loads((store.directory(inspection) / "public-trace.json").read_text())
    assert trace["reviewer_attempt"]["task_id"] != author.task.id
    assert trace["requested_profile"]["model"] == trace["observed_model"]
    assert len(producer.workers.dispatches) == 1


def test_RC02_missing_registration_waits_and_arbitrary_factory_refuses(graph):
    _, inspection, _, store = graph
    assert NativeReviewSource(store).resolve(inspection) is None
    with pytest.raises(EvidenceRequired):
        NativeReviewSource(store, lambda value: {"accepted": True}).resolve(inspection)
    with pytest.raises(AccessDenied):
        NativeRegistry(reviewer_factory="arbitrary.import")


@pytest.mark.parametrize("fault", ["task", "scope", "prompt", "profile"])
def test_RC03_bound_registration_refuses_before_native(graph, fault):
    _, inspection, producer, store = graph
    if fault == "task":
        context = inspection.context
        context["task_id"] = producer.task_id
        inspection = Inspection(json.dumps(context), inspection.witnesses)
    elif fault == "scope":
        context = inspection.context
        context["scope"]["logical_session"] = "foreign"
        inspection = Inspection(json.dumps(context), inspection.witnesses)
    elif fault == "prompt":
        producer.workers.executions.plans["review"] = replace(
            producer.workers.executions.plans["review"], prompt="different"
        )
    else:
        producer.workers.hosts.accounts["claude"] = "0" * 64
    with pytest.raises(EvidenceRequired):
        producer.capture(inspection, store)
    assert not producer.workers.dispatches


def test_RC04_stale_author_bytes_refuse(graph):
    author, inspection, producer, store = graph
    author.report.write_text("changed")
    with pytest.raises(EvidenceRequired):
        producer.capture(inspection, store)
    assert not producer.workers.dispatches


def test_RC09_existing_unknown_intent_never_relaunches(graph):
    _, inspection, producer, store = graph
    store.begin(inspection, {"task_id": producer.task_id})
    with pytest.raises(EvidenceRequired, match="reconciliation"):
        producer.capture(inspection, store)
    assert not producer.workers.dispatches


def test_RC10_readonly_gateway_keeps_authority_and_denies_every_tool():
    class Delegate:
        def check(self):
            pass

        def lease_boundary(self):
            return None

    gateway = ReadOnlyReviewGateway(Delegate(), Inspection("{}", ()))
    for name in ("read_file", "write_file", "run_command", "acknowledge_event"):
        with pytest.raises(AccessDenied):
            gateway.call(name, {})


def test_r04_transport_review_does_not_prepare_inside_model_guard(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from threading import Event, Thread, current_thread

    from ai_task_manager.adapters.linear.dependencies.source import DependencySource
    from tests.scheduling.provider_fixture import ProviderFixture

    p = ProviderFixture(tmp_path / "provider")
    author = ReviewRig(tmp_path / "author", scope=p.scope)
    proof = author.r.proof

    def canonical_provider(kind, subject, payload=None, **kwargs):
        if kind == "provider_status" and payload and payload.get("issue_id", "").startswith("TST:"):
            payload = {**payload, "issue_id": subject}
        return proof(kind, subject, payload, **kwargs)

    monkeypatch.setattr(author.r, "proof", canonical_provider)

    def dependencies(r, task):
        from ai_task_manager.domains.scheduling.resources import AdmissionResources

        p.rig = r
        for row in r.core.list_tasks(r.handle):
            p.sync(row)
        def current_provider(*_):
            for row in r.core.list_tasks(r.handle):
                p.sync(row)
        p.callback = current_provider
        p.receipts.clock = lambda: r.now
        r.core.scheduling.sources = p.receipts
        r.resource_sources.rows[task.id] = AdmissionResources(
            p.scope, task.id, task.prose_digest, task.decision_revision, "authored-project", (), ()
        )
        return DependencySource(r.core, r.handle, p.client, p.receipts)

    inspection, producer = reviewer(author, tmp_path / "reviewer", dependency_factory=dependencies)
    workers, errors, threads = producer.workers, [], []
    started = workers.start
    claimed, waiting_model, waiting_publication = Event(), Event(), Event()
    observed = []

    def instrument(*args, **kwargs):
        attempt = started(*args, **kwargs)
        native = workers.hosts.owned[attempt.id]
        owner = workers._freshness[attempt.id]
        source = owner.registry.dependencies
        guard = native.startup.model_guard
        current = guard.current_boundary
        prepare = guard.prepare_effect
        observe = guard.observation_boundary
        check = guard.check
        publication = source.publication_boundary

        def collect():
            try:
                owner.refresh()
            except BaseException as error:
                errors.append(error)

        def prepared(*args, **kwargs):
            if current_thread().name == "review-background-renewal":
                waiting_model.set()
            return prepare(*args, **kwargs)

        def checked():
            if current_thread().name == "review-background-renewal":
                waiting_model.set()
            return check()

        @contextmanager
        def observing(**kwargs):
            if current_thread().name == "review-background-renewal":
                waiting_model.set()
            with observe(**kwargs):
                yield

        @contextmanager
        def boundary(*args, **kwargs):
            if guard._lock._is_owned() and claimed.is_set():
                observed.append({"model_held": True, "cancelled": owner._stop.is_set(),
                                 "expired_before_wait": author.r.now >= owner._expires_at})
                waiting_publication.set()
                # Release this controlled circular wait at the ORIGINAL expiry.
                author.r.now = owner._expires_at
            with publication(*args, **kwargs):
                yield

        @contextmanager
        def guarded(value):
            with current(value):
                if native.process._model_frame and not claimed.is_set():
                    claimed.set()
                    thread = Thread(target=collect, name="review-background-renewal")
                    threads.append(thread)
                    thread.start()
                    assert waiting_model.wait(3)
                yield

        monkeypatch.setattr(source, "publication_boundary", boundary)
        monkeypatch.setattr(guard, "prepare_effect", prepared)
        monkeypatch.setattr(guard, "check", checked)
        monkeypatch.setattr(guard, "observation_boundary", observing)
        monkeypatch.setattr(guard, "current_boundary", guarded)
        return attempt

    monkeypatch.setattr(workers, "start", instrument)
    store = CaptureStore(tmp_path / "captures")
    try:
        captured = producer.capture(inspection, store)
        assert captured.accepted(("request",), store.resolve(inspection).expires_at - 1)
        assert claimed.is_set() and waiting_model.is_set()
        assert not waiting_publication.is_set(), observed
    finally:
        for thread in threads:
            thread.join(timeout=4)
            assert not thread.is_alive()
        (tmp_path / "r04-control-facts.json").write_text(json.dumps({
            "claimed": claimed.is_set(), "background_waiting_model": waiting_model.is_set(),
            "publication_inside_model_guard": observed,
            "background_errors": [type(error).__name__ for error in errors],
        }, indent=2) + "\n")
        author.runtime.close()


def test_RC07_changed_capture_and_revocation_refuse(graph):
    _, inspection, producer, store = graph
    captured = producer.capture(inspection, store)
    from ai_task_manager.config.files import write_private

    write_private(store.revoked, {"revoked": [captured.reference]}, replace=True)
    with pytest.raises(EvidenceRequired, match="revoked"):
        store.resolve(inspection)


@pytest.mark.parametrize("graph", ["reject", "malformed"], indirect=True)
def test_RC05_RC06_native_verdict_is_retained_without_inventing_acceptance(graph, request):
    _, inspection, producer, store = graph
    if request.node.callspec.params["graph"] == "reject":
        captured = producer.capture(inspection, store)
        assert not captured.accepted(("request",), store.resolve(inspection).expires_at - 1)
    else:
        with pytest.raises(EvidenceRequired):
            producer.capture(inspection, store)
        assert store.resolve(inspection) is None
        assert (store.directory(inspection) / "failure.json").exists()
        with pytest.raises(EvidenceRequired, match="reconciliation"):
            producer.capture(inspection, store)
    assert len(producer.workers.dispatches) == 1


def test_RC01_existing_workflow_issuer_and_core_consume_genuine_producer(graph):
    import time

    from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
    from ai_task_manager.adapters.workflow_reviews import KINDS, PREFIX, WorkflowReviews

    author, inspection, producer, store = graph
    producer.capture(inspection, store)
    issuer = WorkflowReviews(
        author.artifacts, author.issuer_key, NativeReviewSource(store), clock=time.time
    )
    refs = issuer.lookup(author.scope, author.task, author.saved["digest"])
    core = author.r.core
    core.context.evidence = EvidenceRouter(
        [*core.context.evidence.routes, EvidenceRoute(PREFIX, KINDS, issuer)]
    )
    updated = author.complete(refs["units"]["complete"])
    assert updated.completed_work == ("complete",)


def test_RC11_plain_json_cannot_publish_acceptance(graph):
    _, inspection, _, store = graph
    with pytest.raises(EvidenceRequired, match="actual"):
        store._publish(inspection, {"passed": True})
    assert store.resolve(inspection) is None


@pytest.mark.parametrize("published", [True, False])
def test_RC08_lost_reply_recovers_only_published_capture(graph, monkeypatch, published):
    _, inspection, producer, store = graph
    original = store._publish

    def lost(*args):
        if published:
            original(*args)
        raise ConnectionError("actual review result reply lost")

    monkeypatch.setattr(store, "_publish", lost)
    with pytest.raises(ConnectionError):
        producer.capture(inspection, store)
    monkeypatch.setattr(store, "_publish", original)
    if published:
        captured = NativeReviewSource(store).resolve(inspection)
        assert captured is not None
        assert producer.capture(inspection, store) == captured
    else:
        with pytest.raises(EvidenceRequired, match="reconciliation"):
            producer.capture(inspection, store)
    assert len(producer.workers.dispatches) == 1


def test_RC11_cold_installed_source_and_default_composition(graph, tmp_path):
    import json
    import os
    import subprocess
    from dataclasses import asdict

    from ai_task_manager.config.files import write_private
    from ai_task_manager.installation.workflows.composition import InstalledWorkflow

    _, inspection, producer, store = graph
    store = CaptureStore(tmp_path / "state" / "reviews")
    captured = producer.capture(inspection, store)
    input_path = tmp_path / "inspection-input.json"
    witnesses = []
    for witness in inspection.witnesses:
        item = asdict(witness)
        item["path"] = str(item["path"])
        witnesses.append(item)
    write_private(input_path, {"context_json": inspection.context_json, "witnesses": witnesses})
    code = """import json,sys,os
from pathlib import Path
from ai_task_manager.adapters.workflow_reviews.capture_store import CaptureStore
from ai_task_manager.adapters.workflow_reviews.models import Inspection,FileWitness
v=json.loads(Path(sys.argv[1]).read_text())
w=tuple(
 FileWitness(Path(i['path']),tuple(i['metadata']) if i['metadata'] else None,
             i['identity_only']) for i in v['witnesses'])
c=CaptureStore(sys.argv[2]).resolve(Inspection(v['context_json'],w))
print(json.dumps({'reference':c.reference,'model':c.model,'pid':os.getpid()}))
"""
    child = subprocess.run(
        [os.environ["WORKFLOW_CAPTURE_PYTHON"], "-I", "-c", code, str(input_path), str(store.root)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout)["reference"] == captured.reference
    # Use the actual installed composition's fixed source method; model requests
    # cannot choose another implementation through JSON.
    owner = object.__new__(InstalledWorkflow)
    owner.registry = NativeRegistry()
    default = owner._review_source(store.root.parent)
    assert default.factory is None
    assert default.resolve(inspection).reference == captured.reference


def test_RC13_cold_missing_revocation_never_reinitializes(graph):
    from ai_task_manager.config.files import write_private

    _, inspection, producer, store = graph
    captured = producer.capture(inspection, store)
    write_private(store.revoked, {"revoked": [captured.reference]}, replace=True)
    with pytest.raises(EvidenceRequired, match="revoked"):
        store.resolve(inspection)
    directory = store.directory(inspection)
    store.revoked.unlink()
    with pytest.raises(EvidenceRequired):
        CaptureStore(store.root).resolve(inspection)
    assert not store.revoked.exists()
    assert (directory / "binding.json").exists()


@pytest.mark.parametrize("graph", ["large-plain"], indirect=True)
def test_RC14_combined_serialization_refuses_before_binding_and_never_relaunches(graph):
    from ai_task_manager.adapters.hosts.transports.native_input import NativeInput
    from ai_task_manager.adapters.workflow_reviews.native import review_prompt
    from ai_task_manager.config.files import MAX_JSON_BYTES

    _, inspection, producer, store = graph
    prompt = review_prompt(inspection)
    NativeInput.frames(
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": prompt},
                "parent_tool_use_id": None,
                "session_id": "default",
            }
        )
        + "\n"
    )
    with pytest.raises(EvidenceRequired):
        producer.capture(inspection, store)
    directory = store.directory(inspection)
    native = next(iter(producer.workers.hosts.owned.values()))
    terminal = native.journal._read(native.startup.transcript._last, "trace", fresh=False)
    assert len(json.dumps(terminal["payload"]["frame"]).encode()) < MAX_JSON_BYTES
    assert native.process._closed
    assert not (directory / "binding.json").exists()
    assert not (directory / "capture.json").exists()
    assert not (directory / "public-trace.json").exists()
    assert (directory / "intent.json").exists() and (directory / "failure.json").exists()
    assert (
        json.loads((directory / "failure.json").read_text())["outcome"] == "completed_unpublished"
    )
    assert store.resolve(inspection) is None
    with pytest.raises(EvidenceRequired, match="reconciliation"):
        producer.capture(inspection, store)
    assert len(producer.workers.dispatches) == 1


@pytest.mark.parametrize("graph", ["escaped-input"], indirect=True)
def test_RC15_known_native_input_bound_refuses_before_dispatch(graph):
    from ai_task_manager.adapters.hosts.transports.native_input import NativeInput
    from ai_task_manager.adapters.workflow_reviews.native import review_prompt

    _, inspection, producer, store = graph
    prompt = review_prompt(inspection)
    with pytest.raises(AccessDenied):
        NativeInput.frames(
            json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": prompt},
                    "parent_tool_use_id": None,
                    "session_id": "default",
                }
            )
            + "\n"
        )
    with pytest.raises((EvidenceRequired, AccessDenied)):
        producer.capture(inspection, store)
    assert not producer.workers.dispatches
    assert not (store.directory(inspection) / "intent.json").exists()


@pytest.mark.parametrize("fault", ["key", "invalid-revocations"])
def test_RC16_cold_partial_or_invalid_store_refuses(tmp_path, fault):
    from ai_task_manager.config.files import write_private

    store = CaptureStore(tmp_path / "state")
    assert CaptureStore(store.root).key.read_bytes() == store.key.read_bytes()
    if fault == "key":
        store.key.unlink()
    else:
        write_private(store.revoked, {"unknown": []}, replace=True)
    with pytest.raises(EvidenceRequired):
        CaptureStore(store.root)
    if fault == "key":
        assert not store.key.exists()


@pytest.mark.parametrize("text", ["plain", '"\\\n', "é"])
def test_RC17_serializer_matches_actual_writer_and_reader_limit(tmp_path, text):
    from ai_task_manager.adapters.workflow_reviews.source import InstalledReviews
    from ai_task_manager.config.files import MAX_JSON_BYTES, write_private

    value = {"text": text}
    raw = CaptureStore._serialized(value)
    path = tmp_path / "serialized.json"
    write_private(path, value)
    assert raw == path.read_bytes()
    assert InstalledReviews._read(path)[0] == raw
    overhead = len(CaptureStore._serialized({"text": ""}))
    exact = {"text": "x" * (MAX_JSON_BYTES - overhead)}
    bounded = CaptureStore._serialized(exact)
    assert len(bounded) == MAX_JSON_BYTES
    write_private(path, exact, replace=True)
    assert InstalledReviews._read(path)[0] == bounded
    with pytest.raises(EvidenceRequired, match="reader bound"):
        CaptureStore._serialized({"text": exact["text"] + "x"})


@pytest.mark.parametrize("change", ["valid", "revoked", "deleted"])
def test_RC18_native_prepared_consumption_has_no_source_content_io(graph, monkeypatch, change):
    import time
    from contextlib import contextmanager
    from pathlib import Path

    from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
    from ai_task_manager.adapters.workflow_reviews import (
        KINDS,
        PREFIX,
        WorkflowReviews,
        capture_store,
    )
    from ai_task_manager.config.files import write_private

    author, inspection, producer, store = graph
    captured = producer.capture(inspection, store)
    issuer = WorkflowReviews(
        author.artifacts, author.issuer_key, NativeReviewSource(store), clock=time.time
    )
    refs = issuer.lookup(author.scope, author.task, author.saved["digest"])
    core = author.r.core
    core.context.evidence = EvidenceRouter(
        [*core.context.evidence.routes, EvidenceRoute(PREFIX, KINDS, issuer)]
    )
    before = core.get_task(author.r.handle, author.task.id)
    if change == "revoked":
        write_private(store.revoked, {"revoked": [captured.reference]}, replace=True)
    elif change == "deleted":
        store.revoked.unlink()
    transaction, active, content_reads = core.context.db.transaction, [], []

    @contextmanager
    def observed_transaction():
        with transaction() as conn:
            assert conn.in_transaction
            active.append(conn)
            try:
                yield conn
            finally:
                active.pop()

    def forbid(*args, **kwargs):
        assert active and active[-1].in_transaction
        content_reads.append(True)
        raise AssertionError("No source content or preparation I/O inside Core")

    with monkeypatch.context() as guard:
        guard.setattr(core.context.db, "transaction", observed_transaction)
        for obj, name in [
            (capture_store, "read_private"),
            (Path, "open"),
            (Path, "read_bytes"),
            (Path, "read_text"),
            (author.artifacts.core, "get_task"),
            (author.artifacts.core, "session_binding"),
            (author.store, "verified"),
            (author.inspector, "inspect"),
        ]:
            guard.setattr(obj, name, forbid)
        if change == "valid":
            updated = author.complete(refs["units"]["complete"], key="metadata-consumption")
            assert updated.completed_work == ("complete",)
        else:
            with pytest.raises(EvidenceRequired, match="source changed"):
                author.complete(refs["units"]["complete"], key="metadata-consumption")
    assert not content_reads
    if change != "valid":
        assert core.get_task(author.r.handle, author.task.id) == before
