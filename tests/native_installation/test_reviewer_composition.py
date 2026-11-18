"""Default installed owner composition with authored provider and native contracts."""

import hashlib
import json
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from ai_task_manager.adapters.decisions.authority import HostGrant
from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.hosts.bridge.auth_probe import claude_account_hash
from ai_task_manager.adapters.proofs.router import EvidenceRoute, EvidenceRouter
from ai_task_manager.adapters.workflow_control.identity import file_binding
from ai_task_manager.adapters.workflow_reviews.capture_store import CaptureStore, NativeReviewSource
from ai_task_manager.adapters.workflow_reviews.native import review_prompt
from ai_task_manager.config.claim_store import enroll_claim_store
from ai_task_manager.config.files import write_private
from ai_task_manager.config.service import load_service
from ai_task_manager.config.sessions import SessionFile
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import ApprovalPolicy, CapacityPolicy, TaskProse
from ai_task_manager.installation.native import NativeManifest, normal, resolve
from ai_task_manager.installation.native.manifest import installation_intent
from ai_task_manager.installation.native.store import NativeStore
from ai_task_manager.installation.workflows import InstalledWorkflow
from ai_task_manager.presentation.cli.main import main
from tests.application.helpers import Observations
from tests.hosts.test_continuity import AuthoredDependencyCollection
from tests.native_installation.helpers import bundle, refresh_digest
from tests.native_installation.test_normal import AuthoredRestrictions
from tests.runtime.helpers import ProviderFixture
from tests.scheduling.helpers import known_declaration
from tests.workflow_reviews.helpers import ReviewRig


@pytest.fixture
def installed(tmp_path, monkeypatch, request):
    author = ReviewRig(tmp_path / "author")
    request.addfinalizer(author.runtime.close)
    r = author.r
    r.now = time.time()
    r.policy = ApprovalPolicy("a" * 64, "b" * 64, "c" * 64)
    r.capacity = CapacityPolicy("codex" if getattr(request, "param", None) else "claude", 2, 2)
    r.core = r.new_core()
    inspection = author.artifacts.prepare(author.scope, author.task, author.saved["digest"])
    prose = TaskProse("Independent review of the owned artifact", review_prompt(inspection), False)
    task = r.core.intake.submit(r.handle, prose, r.approval(prose), r.key())
    task = r.confirm(task)
    task = r.core.transitions.transition(
        r.handle,
        task.id,
        "Todo",
        "enqueue",
        task.version,
        task.decision_revision,
        r.decision(task, "Todo"),
        {},
        r.key(),
    )
    task = r.confirm(task)
    r.known_dependencies[task.id] = ((), "normal")
    known_declaration(r, task)
    root = tmp_path / "installed"
    root.mkdir(mode=0o700)
    journal = AuthorityJournal(root / "authority.sqlite", root / "authority.key")
    Path(r.db.path).chmod(0o600)
    enroll_claim_store(r.db, journal)
    SessionFile(root / "session.json").save(r.handle, r.scope)
    socket_root = tempfile.TemporaryDirectory(prefix="nr-", dir="/tmp")
    request.addfinalizer(socket_root.cleanup)
    sockets = Path(socket_root.name)
    config = root / "service.json"
    write_private(
        config,
        {
            "version": 1,
            "core_database": str(r.db.path),
            "authority_database": str(journal.path),
            "authority_key": str(journal.key_path),
            "session_file": str(root / "session.json"),
            "approval_policy": asdict(r.policy),
            "grant_ref": "declared-grant",
            "workflow_control": {
                "record": str(sockets / "owner.json"),
                "endpoint": str(sockets / "owner.sock"),
                "instance": "cc122ed9-6c8a-4670-8dcc-5f2fd0b901ce",
            },
        },
    )
    observations = Observations()
    observations.grants["declared-grant"] = HostGrant(
        r.scope,
        r.scope.logical_session,
        ("start", "non_code_complete"),
        "declared-operator",
        time.time() + 120,
    )

    def provider(core, handle, decisions, writer):
        r.core = core
        return ProviderFixture(r)

    options = {
        "observations": observations,
        "evidence_factory": lambda j: EvidenceRouter(
            [
                EvidenceRoute(
                    "fixture:", frozenset({"provider_status", "external_observation"}), r.proofs
                ),
                EvidenceRoute("decision-proof:", frozenset({"decision", "human_decision"}), j),
            ]
        ),
        "provider_factory": provider,
        "dependencies_factory": lambda c, h: AuthoredDependencyCollection(c, h),
        "scheduling_sources": r.scheduling_sources,
    }
    import ai_task_manager.installation.native.reviewer as reviewer_module
    import ai_task_manager.installation.workflows.composition as composition

    monkeypatch.setattr(
        reviewer_module, "load_service", lambda *a, **kw: load_service(*a, **{**options, **kw})
    )
    monkeypatch.setattr(
        composition, "load_service", lambda *a, **kw: load_service(*a, **{**options, **kw})
    )
    import ai_task_manager.installation.native as native_module

    monkeypatch.setattr(
        native_module, "resolve", lambda *a, **kw: resolve(*a, **{**kw, "service_options": options})
    )
    # The real resolver selects the factory; external service ports are declared contracts.
    codex_model = getattr(request, "param", None)

    class DeclaredReviewerRestrictions(AuthoredRestrictions):
        def require_native_conformance(self, manifest, *args):
            assert codex_model is not None and manifest.data["host"] == "codex"
            assert Path(manifest.data["executable"]).name == "declared-native"
            return "authored-codex-contract-not-genuine-conformance"

    original_source = normal.NormalAuthoritySource
    monkeypatch.setattr(
        normal,
        "NormalAuthoritySource",
        lambda *a, **kw: original_source(
            *a, **{**kw, "conformance": DeclaredReviewerRestrictions()}
        ),
    )
    deadline = time.time() + 60
    verdict = {
        "context_digest": inspection.digest,
        "criteria": [
            {"id": "request", "judgment": "accept", "reason": "Declared protocol verdict"}
        ],
        "reason": "Declared SDK contract, not genuine model judgment",
    }
    script = Path(__file__).parents[1].joinpath("hosts/fixture_claude_bootstrap.py").read_text()
    script = script.replace(
        '"result": "READY",',
        '"result": (json.dumps('
        + repr(verdict)
        + ') if "Independently review" in json.dumps(value) else "READY"),',
    )
    base = author.plan.base
    stores = {}

    def install(name, selected, role, extra):
        host = "codex" if codex_model is not None and role == "reviewer" else "claude"
        model = codex_model if host == "codex" else "claude-opus-5-5"
        b = bundle(root / name, host, model, task_id=selected.id)
        selected_script = script
        if host == "codex":
            from tests.workflow_review_capture.helpers import codex_script

            selected_script = codex_script(verdict, model)
        executable = b.root / "declared-native"
        executable.write_text("#!" + sys.executable + "\n" + selected_script)
        executable.chmod(0o700)
        store = NativeStore.create(b.root / "permission.sqlite")
        spec = {
            "version": 1,
            "config": file_binding(config),
            "scope": asdict(r.scope),
            "task_id": selected.id,
            "prose_digest": selected.prose_digest,
            "native_ref": "normal-native",
            "native_digest": "0" * 64,
            "native_manifest": str(b.manifest),
            "repository": str(author.runtime.repo),
            "source_head": base,
            "state_root": str(b.root / "state"),
            "deadline": deadline,
            "principal_ref": "declared-principal",
            "plan_ref": name,
            "files": ["report.txt"],
            "artifacts": ["report.txt"],
            "resource_locks": [],
            "installation_path": str(b.installation),
            "role": role,
            **extra,
        }
        write_private(b.installation, spec, replace=True)
        from ai_task_manager.config.files import digest
        from tests.hosts.fixture_cli import ACCOUNT

        b.data.update(
            authority_store=store.pointer,
            config=file_binding(config),
            installation=installation_intent(b.installation),
            executable=str(executable),
            executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
            account_hash=digest(ACCOUNT)
            if host == "codex"
            else claude_account_hash(
                {
                    "email": "fixture@example.invalid",
                    "orgId": "authored-org",
                    "userId": "authored-user",
                }
            ),
        )
        write_private(b.manifest, b.data, replace=True)
        refresh_digest(b.installation, b.manifest, b.data)
        manifest = NativeManifest(b.manifest, config, b.installation)
        normal.enroll(
            store,
            manifest,
            {"host": 35, "relay": 35, "auth": 5, "neutral": 8, "assignment": 8},
            identity_refreshes=(),
            conformance_ref="declared-conformance",
            role=role,
        )
        stores[name] = store
        return b

    reviewer = install(
        "reviewer",
        task,
        "reviewer",
        {
            "review_target": {
                "task_id": author.task.id,
                "prose_digest": author.task.prose_digest,
                "source_head": base,
            }
        },
    )
    primary = install(
        "author",
        author.task,
        "worker",
        {"reviewer_installation": file_binding(reviewer.installation)},
    )
    state = {
        "author": author,
        "inspection": inspection,
        "config": config,
        "primary": primary,
        "reviewer": reviewer,
        "stores": stores,
        "task": task,
        "owners": [],
    }
    try:
        yield state
    finally:
        for owner in state["owners"]:
            factory = owner.registry.reviewer_factory
            for producer in factory.producers.values():
                for native in producer.workers.hosts.owned.values():
                    if native.process is not None and not native.process._closed:
                        assert native.stop().confirmed
        # The immediate finalizer also covers setup refusals.


def test_NR01_default_installed_cli_constructs_real_reviewer_and_cold_capture(
    installed, monkeypatch
):
    s = installed
    captured = []
    import importlib
    import traceback

    cli_module = importlib.import_module("ai_task_manager.presentation.cli.main")
    failures = []
    original_failure = cli_module.failure

    def observe_failure(error):
        failures.append(traceback.format_exc())
        return original_failure(error)

    monkeypatch.setattr(cli_module, "failure", observe_failure)

    def bounded_serve(owner, stop=None, ready=None):
        s["owners"].append(owner)
        source = owner._review_source(Path(owner.spec.data["state_root"]))
        captured.append(source.resolve(s["inspection"]))
        assert captured[-1].reviewer_id != s["inspection"].context["author"]["worker_id"]
        owner.owner.shutdown_result = owner.service.workflows.close()

    monkeypatch.setattr(InstalledWorkflow, "serve", bounded_serve)
    code = main(
        [
            "--config",
            str(s["config"]),
            "--installation",
            str(s["primary"].installation),
            "--json",
            "workflow",
            "owner",
        ]
    )
    assert code == 0, "\n".join(failures)
    assert captured[0].accepted(("request",), time.time())
    registry = resolve(s["config"], s["primary"].installation)
    root = Path(json.loads(s["primary"].installation.read_text())["state_root"])
    again = NativeReviewSource(CaptureStore(root / "reviews"), registry.reviewer_factory).resolve(
        s["inspection"]
    )
    assert again == captured[0] and not registry.reviewer_factory.producers
    producer = next(iter(s["owners"][0].registry.reviewer_factory.producers.values()))
    declaration = producer.workers.core.execution.declared_resources(
        producer.workers.core.session_binding(producer.handle)[1],
        producer.workers.core.get_task(producer.handle, producer.task_id),
    )
    assert declaration.locks == () and declaration.files == ("report.txt",)
    assert declaration.witnesses
    assert len(producer.workers.hosts.owned) == 1
    assert all(n.process._closed for n in producer.workers.hosts.owned.values())


@pytest.mark.parametrize("fault", ["revoked", "target", "source", "role"])
def test_NR02_reviewer_authority_refuses_before_new_native(installed, fault):
    s = installed
    registry = resolve(s["config"], s["primary"].installation)
    if fault == "revoked":
        s["stores"]["reviewer"].revoke("operator-permission")
    elif fault == "source":
        s["reviewer"].manifest.write_text("changed")
    elif fault == "role":
        with pytest.raises(AccessDenied):
            resolve(s["config"], s["reviewer"].installation)
        return
    else:
        from ai_task_manager.adapters.workflow_reviews.models import Inspection

        value = s["inspection"].context
        value["task_id"] = "foreign"
        s["inspection"] = Inspection(json.dumps(value), s["inspection"].witnesses)
    with pytest.raises(Exception):
        registry.reviewer_factory(s["inspection"])
    assert not registry.reviewer_factory.producers


def test_NR03_existing_transaction_rejects_foreign_inactive_and_closed_connection(
    installed, tmp_path
):
    from ai_task_manager.domains.work.errors import SchemaError
    from ai_task_manager.storage.database import Database

    s = installed
    core = s["author"].r.core
    handle, task = s["author"].r.handle, s["author"].task
    other = Database(tmp_path / "foreign.sqlite")
    with other.transaction() as conn:
        with pytest.raises(SchemaError, match="active Core transaction"):
            core.get_task(handle, task.id, connection=conn)
        with pytest.raises(SchemaError, match="active Core transaction"):
            core.external.observations(handle, task.id, connection=conn)
    conn = core.context.db.connect()
    try:
        with pytest.raises(SchemaError, match="active Core transaction"):
            core.get_task(handle, task.id, connection=conn)
    finally:
        conn.close()
    with core.context.db.transaction() as owned:
        assert core.get_task(handle, task.id, connection=owned).id == task.id
        assert core.external.observations(handle, task.id, connection=owned) == []
    with pytest.raises(SchemaError, match="active Core transaction"):
        core.get_task(handle, task.id, connection=owned)


@pytest.mark.parametrize("change", ["assignee", "hold"])
def test_NR04_current_author_change_at_model_input_fences_review_and_stops(
    installed, monkeypatch, change
):
    from contextlib import contextmanager

    from ai_task_manager.adapters.hosts.bridge.gateway import ManagedToolGateway

    s = installed
    owner = InstalledWorkflow(s["config"], s["primary"].installation)
    s["owners"].append(owner)
    factory = owner.registry.reviewer_factory
    producer = factory(s["inspection"])
    core, handle = producer.workers.core, producer.handle
    original = ManagedToolGateway.lease_boundary
    injected = []

    @contextmanager
    def changed(gateway, *, inspect=None):
        if inspect is not None and not injected:
            task = core.get_task(handle, s["author"].task.id)
            proof = s["author"].r.proof(
                "external_observation",
                task.id,
                {
                    "issue_id": task.issue_id,
                    "source_revision": "2026-11-04T08:00:00Z",
                    "status": "On Hold" if change == "hold" else task.observed_status,
                    "title": task.title,
                    "body": task.body,
                    "assigned_to_owner": change != "assignee",
                    "actor_kind": "human",
                    "actor_authorized": True,
                },
            )
            core.external.observe(
                handle, task.id, task.version, task.decision_revision, proof, "input-race-" + change
            )
            injected.append(True)
        with original(gateway, inspect=inspect):
            yield

    monkeypatch.setattr(ManagedToolGateway, "lease_boundary", changed)
    store = CaptureStore(Path(factory.author.installation.path).parent / "race-captures")
    try:
        with pytest.raises(AccessDenied):
            producer.capture(s["inspection"], store)
        assert injected and store.resolve(s["inspection"]) is None
        assert len(producer.workers.hosts.owned) == 1
        for native in producer.workers.hosts.owned.values():
            assert native.process._closed
            outcome = native.process.native_input.last_outcome
            assert outcome.written_frames == 0 and outcome.confirmed_boundaries == 0
            assert outcome.uncertain  # Incomplete staged prefix is retained, not claimed absent.
    finally:
        for native in producer.workers.hosts.owned.values():
            if not native.process._closed:
                assert native.stop().confirmed


def test_NR05_lease_inspection_uses_core_only_and_physical_check_stays_outside(
    installed, monkeypatch
):
    from ai_task_manager.adapters.workflow_reviews.models import Inspection

    s = installed
    producer = resolve(s["config"], s["primary"].installation).reviewer_factory(s["inspection"])
    db = producer.workers.core.context.db
    original = Inspection.check
    counts = []

    def physical(value):
        assert not db._active_transactions, "Physical artifact inspection entered Core lease"
        counts.append(True)
        return original(value)

    monkeypatch.setattr(Inspection, "check", physical)
    producer.current_author()
    assert counts
    before = len(counts)
    with db.transaction() as connection:
        producer.current_author(connection)
    assert len(counts) == before


@pytest.mark.parametrize("change", ["revoked", "source"])
def test_NR06_cached_factory_authority_change_cannot_start_reviewer(installed, change):
    s = installed
    factory = resolve(s["config"], s["primary"].installation).reviewer_factory
    producer = factory(s["inspection"])
    if change == "revoked":
        s["stores"]["reviewer"].revoke("operator-permission")
    else:
        s["reviewer"].manifest.write_text("changed-after-composition")
    store = CaptureStore(Path(factory.author.installation.path).parent / "changed-captures")
    try:
        with pytest.raises(Exception):
            producer.capture(s["inspection"], store)
        assert not producer.workers.hosts.owned
        assert store.resolve(s["inspection"]) is None
    finally:
        for native in producer.workers.hosts.owned.values():
            if not native.process._closed:
                assert native.stop().confirmed
