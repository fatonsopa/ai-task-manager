import json
import os
import sqlite3
import subprocess
from dataclasses import replace

import pytest

from ai_task_manager.adapters.decisions.authority import DecisionAuthority
from ai_task_manager.adapters.decisions.observations import SessionObservations
from ai_task_manager.application.service.models import SCHEMAS
from ai_task_manager.config.files import read_private, write_private
from ai_task_manager.config.service import load_service
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired, IdempotencyConflict
from ai_task_manager.installation.sessions import setup
from tests.session_observations.helpers import OwnedProvider


@pytest.fixture
def provider(tmp_path):
    value = OwnedProvider(tmp_path / "owned")
    try:
        yield value
    finally:
        value.close()


def install(p, **changes):
    args = dict(
        logical_session="lead-session",
        actions=("enqueue", "start", "non_code_complete"),
        deadline=p.deadline,
        operation_key="original-setup",
    )
    args.update(changes)
    return setup(p.config, components=p.components(), **args)


def service(p):
    return load_service(p.config, linear_components=p.components())


def task_control():
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class CurrentTask:
        id: str = "scoped-task"
        observed_status: str = "Backlog"
        decision_revision: int = 3
        prose_digest: str = "a" * 64

    return CurrentTask()


def test_SO01_SO02_real_http_setup_cold_default_consumes_scoped_grant(provider):
    p = provider
    result = install(p)
    before = read_private(p.root / "session.json")
    source = """import json,sys
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.config.service import load_service
from dataclasses import dataclass
HttpTransport.ENDPOINT=sys.argv[2]
s=load_service(sys.argv[1])
@dataclass
class Task:
 id:str="scoped-task"
 observed_status:str="Backlog"
 decision_revision:int=3
 prose_digest:str="a"*64
ref=s.decisions.decision(s.scope,Task(),"Todo","enqueue",grant_ref=s.grant_ref)
e=s.decisions.journal.verify_evidence(ref)
print(json.dumps({"scope":vars(e.scope),"kind":e.kind,"actor":e.payload["actor"],"revision":e.payload["revision"]}))
"""
    child = subprocess.run(
        [os.environ["SESSION_OBSERVATIONS_PYTHON"], "-I", "-c", source, str(p.config), p.endpoint],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode == 0, child.stderr
    actual = json.loads(child.stdout)
    assert actual["scope"] == result["scope"]
    assert actual["kind"] == "decision" and actual["actor"] == "agent" and actual["revision"] == 3
    assert read_private(p.root / "session.json") == before
    assert not any("enroll" in name or "grant" in name for name in SCHEMAS)
    assert p.requests >= 6


@pytest.mark.parametrize("field", ["viewer_id", "workspace_id", "project_id"])
def test_SO03_actual_changed_provider_refuses(provider, field):
    install(provider)
    s = service(provider)
    provider.actual = replace(provider.actual, **{field: "00000000-0000-0000-0000-000000000001"})
    with pytest.raises(Exception):
        s.decisions.observations.verify_enrollment(s.grant_ref)


def test_SO03_copy_config_and_key_storage_refuse(provider):
    install(provider)
    copied = provider.root / "copied.json"
    write_private(copied, read_private(provider.config))
    s = load_service(copied, linear_components=provider.components())
    with pytest.raises(AccessDenied, match="configuration"):
        s.decisions.observations.verify_enrollment(s.grant_ref)
    s = service(provider)
    p = provider.root / "authority.key"
    original = p.read_bytes()
    p.write_bytes(b"x" * 32)
    with pytest.raises(AccessDenied):
        s.decisions.observations.verify_enrollment(s.grant_ref)
    p.write_bytes(original)


@pytest.mark.parametrize("fault", ["revoked", "expired", "missing"])
def test_SO04_preserve_history_on_refusal(provider, fault):
    install(provider)
    s = service(provider)
    o = s.decisions.observations
    if fault == "revoked":
        o.revoke()
    elif fault == "expired":
        o.journal.clock = lambda: provider.deadline
    ref = "session-enrollment:missing" if fault == "missing" else s.grant_ref
    with o.journal.transaction() as conn:
        count = conn.execute("SELECT count(*) FROM records").fetchone()[0]
    with pytest.raises(AccessDenied):
        o.verify_enrollment(ref)
    with o.journal.transaction() as conn:
        assert count == conn.execute("SELECT count(*) FROM records").fetchone()[0]


def test_SO05_agent_grant_never_issues_human_or_unpermitted_decision(provider):
    install(provider)
    s = service(provider)
    with pytest.raises(AccessDenied):
        s.decisions.decision(s.scope, task_control(), "Canceled", "cancel", grant_ref=s.grant_ref)
    with pytest.raises(EvidenceRequired, match="human"):
        s.decisions.decision(
            s.scope,
            replace(task_control(), observed_status="In Review"),
            "Todo",
            "enqueue",
            grant_ref=s.grant_ref,
        )
    with pytest.raises(EvidenceRequired):
        s.decisions.observations.verify_instruction(s.grant_ref)
    with pytest.raises(EvidenceRequired):
        s.decisions.observations.verify_host_observation(s.grant_ref)


def test_SO06_reply_loss_after_publish_returns_same_session(provider, monkeypatch):
    from ai_task_manager.config.sessions import SessionFile

    original = SessionFile.save

    def lost(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise ConnectionError("lost setup reply after publish")

    monkeypatch.setattr(SessionFile, "save", lost)
    with pytest.raises(ConnectionError):
        install(provider)
    saved = read_private(provider.root / "session.json")
    monkeypatch.setattr(SessionFile, "save", original)
    install(provider)
    assert read_private(provider.root / "session.json") == saved
    with sqlite3.connect(provider.root / "core.sqlite") as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1


def test_SO07_uncertain_unpublished_commit_never_replays(provider, monkeypatch):
    from ai_task_manager.config.sessions import SessionFile

    monkeypatch.setattr(
        SessionFile,
        "save",
        lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("reply lost before publish")),
    )
    with pytest.raises(ConnectionError):
        install(provider)
    with pytest.raises(EvidenceRequired, match="reconcile"):
        install(provider)
    with sqlite3.connect(provider.root / "core.sqlite") as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1


def test_SO08_changed_setup_arguments_conflict(provider):
    install(provider)
    with pytest.raises(IdempotencyConflict):
        install(provider, actions=("cancel",))
    with pytest.raises(IdempotencyConflict):
        install(provider, logical_session="foreign")


def test_SO10_missing_default_stays_refused(provider):
    from ai_task_manager.adapters.decisions.authority import RejectObservations

    assert isinstance(DecisionAuthority(object()).observations, RejectObservations)
    with pytest.raises(EvidenceRequired):
        DecisionAuthority(object()).observations.verify_enrollment("JSON-is-not-authorization")


def test_SO09_real_core_retains_task_version_and_revision_guards(provider):
    from uuid import uuid4

    from ai_task_manager.application.intake import prose_digest
    from ai_task_manager.domains.work.errors import StaleDecision, StaleVersion
    from ai_task_manager.domains.work.models import Approval, Cause, TaskProse
    from tests.domains.helpers import ProofFixture

    install(provider)
    s = service(provider)
    proofs = ProofFixture()
    s.core.context.approvals = proofs
    prose = TaskProse("Verify scoped delegation", "Retain the exact task result.", False)
    rendered = s.core.context.renderer.render(prose)
    proofs.approvals["authored-approved-prose"] = Approval(
        prose_digest(prose, rendered), s.core.context.approval_policy, rendered.representation
    )
    task = s.core.intake.submit(s.handle, prose, "authored-approved-prose", "submit")
    operation = s.core.sync.list_operations(s.handle)[0]
    journal = s.core.context.principals
    with journal.transaction() as conn:
        proof = journal._proof(
            conn,
            "provider_status",
            s.scope,
            task.id,
            {
                "operation_id": operation.id,
                "status": "Backlog",
                "prose_digest": task.prose_digest,
                "title": task.title,
                "body": task.body,
                "representation": task.representation,
                "issue_id": str(uuid4()),
            },
        )
    task = s.core.intake.confirm_status(
        s.handle, task.id, task.version, task.decision_revision, proof, "confirm-backlog"
    )
    ref = s.decisions.decision(s.scope, task, "Todo", "enqueue", grant_ref=s.grant_ref)
    with pytest.raises(StaleVersion):
        s.core.transitions.transition(
            s.handle,
            task.id,
            "Todo",
            Cause.ENQUEUE,
            task.version - 1,
            task.decision_revision,
            ref,
            {},
            "stale-version",
        )
    with pytest.raises(StaleDecision):
        s.core.transitions.transition(
            s.handle,
            task.id,
            "Todo",
            Cause.ENQUEUE,
            task.version,
            task.decision_revision + 1,
            ref,
            {},
            "stale-revision",
        )
    updated = s.core.transitions.transition(
        s.handle,
        task.id,
        "Todo",
        Cause.ENQUEUE,
        task.version,
        task.decision_revision,
        ref,
        {},
        "actual-enqueue",
    )
    assert updated.desired_status == "Todo" and updated.observed_status == "Backlog"
    assert (
        updated.prose_digest == task.prose_digest
        and updated.decision_revision == task.decision_revision
    )
    assert s.core.sync.list_operations(s.handle)[-1].kind == "set_status"


def test_SO03_copied_journal_and_changed_source_owner_refuse(provider):
    import shutil

    from ai_task_manager.adapters.decisions.journal import AuthorityJournal

    install(provider)
    s = service(provider)
    o = s.decisions.observations
    copy = provider.root / "copied-authority.sqlite"
    shutil.copyfile(o.journal.path, copy)
    copy.chmod(0o600)
    foreign = AuthorityJournal(copy, o.journal.key_path)
    with pytest.raises(AccessDenied, match="storage"):
        SessionObservations(foreign, o.client, provider.config, o.pointer)
    o.pointer["reference"] = "session-enrollment:foreign"
    with pytest.raises(AccessDenied, match="owner"):
        o.verify_enrollment(s.grant_ref)


def test_SO11_unfinished_intent_cannot_be_selected_by_edited_configuration(provider, monkeypatch):
    from ai_task_manager.adapters.decisions.journal import AuthorityJournal
    from ai_task_manager.adapters.decisions.observations import PREFIX, identity
    from ai_task_manager.config.files import digest
    from ai_task_manager.config.sessions import SessionFile

    monkeypatch.setattr(
        SessionFile, "save", lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("unpublished"))
    )
    with pytest.raises(ConnectionError):
        install(provider)
    config = read_private(provider.config)
    journal = AuthorityJournal(config["authority_database"], config["authority_key"])
    reference = PREFIX + digest({"config": str(provider.config), "key": "original-setup"})
    pointer = {
        "reference": reference,
        "journal": identity(journal.path),
        "key": identity(journal.key_path, content=True),
    }
    config.update(grant_ref=reference, session_observations=pointer)
    write_private(provider.config, config, replace=True)
    source = SessionObservations(journal, provider.components().client, provider.config, pointer)
    with pytest.raises(AccessDenied, match="missing"):
        source.verify_enrollment(reference)
