"""Authored loopback GraphQL controls for the installed operator port, not live acceptance."""

import copy
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.decisions.observations import bind_record
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.adapters.writing.package import WriterPackage
from ai_task_manager.config.files import read_private, write_private
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.installation.sessions.operator import doctor, setup
from ai_task_manager.installation.sessions.operator_schema import parse
from ai_task_manager.installation.workflows.bootstrap import activation_reference, prepare
from ai_task_manager.presentation.cli.main import main
from ai_task_manager.storage.database import Database
from tests.session_observations.helpers import OwnedProvider


@pytest.fixture
def operator(tmp_path, monkeypatch):
    provider = OwnedProvider(tmp_path / "authored-provider")
    monkeypatch.setattr(HttpTransport, "ENDPOINT", provider.endpoint)
    monkeypatch.setenv("AITM_CREDENTIAL_OPERATOR_CONTROL", "authored-local-credential")
    private = tempfile.TemporaryDirectory(prefix="aitm-op-", dir="/tmp")
    root = Path(private.name).resolve()
    state = root / "state"
    python = Path(sys.executable).absolute()
    source = Path("/Users/faton/Documents/ChatGPT/technical-writer")
    package = WriterPackage.capture(source, str(python))
    request = {
        "version": 1,
        "state_directory": str(state),
        "linear": {
            "expected": asdict(provider.expected),
            "agent_connection": provider.linear["agent_connection"],
            "credential": {"environment": "AITM_CREDENTIAL_OPERATOR_CONTROL"},
        },
        "logical_session": "lead-session",
        "writer": {
            "root": str(package.root),
            "python": str(python),
            "python_sha256": hashlib.sha256(python.read_bytes()).hexdigest(),
            "pins": package.pins,
        },
        "capacity_policies": [{"host": "codex", "host_limit": 1, "project_limit": 1}],
        "actions": ["enqueue"],
        "deadline": time.time() + 120,
        "operation_key": "authored-operator-setup",
    }
    try:
        yield state / "service.json", request, provider
    finally:
        provider.close()
        private.cleanup()


def snapshot(root):
    return {
        str(p.relative_to(root)): (
            p.stat().st_ino,
            p.stat().st_mode,
            p.stat().st_mtime_ns,
            p.read_bytes(),
        )
        for p in root.rglob("*")
        if p.is_file()
    }


def call(capsys, config, action, request_file=None):
    argv = ["--config", str(config), "--json", "session", action]
    if request_file is not None:
        argv += ["--input", str(request_file)]
    code = main(argv)
    output = capsys.readouterr()
    raw = output.out or output.err
    return code, json.loads(raw), raw


def test_OP01_default_cli_setup_doctor_prepare_and_ordinary_owner_guard(operator, capsys):
    config, request, provider = operator
    request_file = config.parent.parent / "setup.json"
    request_file.write_text(json.dumps(request))
    code, established, raw = call(capsys, config, "setup", request_file)
    assert code == 0 and established["ok"]
    assert "authored-local-credential" not in raw and "capability" not in established["data"]
    values = read_private(config)
    assert values["approval_policy"] == asdict(
        WriterPackage(
            Path(request["writer"]["root"]), request["writer"]["python"], request["writer"]["pins"]
        ).verify()
    )
    assert not Path(values["workflow_control"]["record"]).exists()
    assert not Path(values["workflow_control"]["endpoint"]).exists()
    before = snapshot(config.parent)
    code, diagnosed, raw = call(capsys, config, "doctor")
    assert code == 0 and diagnosed["data"]["ready"]
    assert snapshot(config.parent) == before
    assert "authored-local-credential" not in raw
    assert prepare(config, {"operation": "task_list", "data": {}})["data"] == []
    assert main(["--config", str(config), "--json", "task", "list"]) == 3
    capsys.readouterr()
    assert provider.requests > 0
    with sqlite3.connect(values["core_database"]) as conn:
        assert conn.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_OP02_exact_replay_keeps_capability_grant_and_store(operator):
    config, request, _ = operator
    first = setup(config, request)
    before = snapshot(config.parent)

    def contents():
        dumps = {}
        for path in config.parent.glob("*.sqlite"):
            with sqlite3.connect(path) as conn:
                dumps[path.name] = list(conn.iterdump())
        return dumps

    logical_before = contents()
    assert setup(config, request) == first
    assert contents() == logical_before
    assert {k: v for k, v in snapshot(config.parent).items() if not k.endswith(".sqlite")} == {
        k: v for k, v in before.items() if not k.endswith(".sqlite")
    }


@pytest.mark.parametrize("change", ["scope", "source", "capacity", "deadline", "key", "actions"])
def test_OP03_changed_manifest_refuses_without_replacing_state(operator, change):
    config, request, _ = operator
    setup(config, request)
    before = snapshot(config.parent)
    changed = copy.deepcopy(request)
    if change == "scope":
        changed["logical_session"] = "different-session"
        changed["linear"]["agent_connection"]["logical_session"] = "different-session"
    elif change == "source":
        changed["writer"]["python_sha256"] = "0" * 64
    elif change == "capacity":
        changed["capacity_policies"][0]["host_limit"] = 2
    elif change == "deadline":
        changed["deadline"] += 60
    elif change == "key":
        changed["operation_key"] = "new-enrollment-cannot-renew"
    else:
        changed["actions"] = ["start"]
    with pytest.raises(Exception):
        setup(config, changed)
    assert snapshot(config.parent) == before


@pytest.mark.parametrize(
    "change",
    [
        "version",
        "unknown",
        "secret",
        "two_credentials",
        "namespace",
        "capacity",
        "bool_capacity",
        "infinite",
        "pin",
        "interpreter",
        "relative",
        "team",
        "connection",
    ],
)
def test_OP04_invalid_manifest_fails_before_state_creation(operator, change):
    config, request, _ = operator
    changed = copy.deepcopy(request)
    if change == "version":
        changed["version"] = True
    elif change == "unknown":
        changed["factory"] = "module.factory"
    elif change == "secret":
        changed["linear"]["credential"] = {"value": "AUTHORED-SECRET-DO-NOT-PRINT"}
    elif change == "two_credentials":
        changed["linear"]["credential"]["file"] = "/some/file"
    elif change == "namespace":
        changed["linear"]["credential"] = {"environment": "LINEAR_API_KEY"}
    elif change == "capacity":
        changed["capacity_policies"] = []
    elif change == "bool_capacity":
        changed["capacity_policies"][0]["host_limit"] = True
    elif change == "infinite":
        changed["deadline"] = float("inf")
    elif change == "pin":
        changed["writer"]["pins"]["SKILL.md"] = "0" * 64
    elif change == "interpreter":
        changed["writer"]["python"] = "/bin/sh"
    elif change == "relative":
        changed["state_directory"] = "relative"
    elif change == "team":
        changed["linear"]["expected"]["team_key"] = "OTHER"
    else:
        changed["linear"]["agent_connection"]["logical_session"] = "foreign"
    with pytest.raises(Exception):
        setup(config, changed)
    assert not config.parent.exists()


@pytest.mark.parametrize("case", ["foreign", "symlink", "public", "long_socket", "project"])
def test_OP05_unsafe_state_and_foreign_files_refuse(operator, case, monkeypatch):
    config, request, _ = operator
    if case == "long_socket":
        state = config.parent.parent / ("x" * 90)
        request["state_directory"] = str(state)
        config = state / "service.json"
    elif case == "project":
        monkeypatch.chdir(config.parent.parent)
    else:
        config.parent.mkdir(mode=0o700)
        if case == "foreign":
            (config.parent / "foreign").write_text("retain")
        elif case == "symlink":
            config.parent.rmdir()
            target = config.parent.parent / "target"
            target.mkdir(mode=0o700)
            config.parent.symlink_to(target)
        else:
            config.parent.chmod(0o755)
    with pytest.raises(Exception):
        setup(config, request)
    assert not config.exists()


def test_OP06_provider_refusal_retains_partial_intent_and_cannot_adopt(operator):
    config, request, provider = operator
    provider.actual = provider.expected.__class__(
        provider.expected.viewer_id,
        provider.expected.workspace_id,
        provider.expected.team_id,
        "FOREIGN",
        provider.expected.project_id,
    )
    with pytest.raises(Exception):
        setup(config, request)
    assert (config.parent / "operator-intent.json").exists()
    before = snapshot(config.parent)
    provider.actual = provider.expected
    with pytest.raises(EvidenceRequired, match="Incomplete operator setup retained"):
        setup(config, request)
    assert snapshot(config.parent) == before
    assert not doctor(config)["ok"]
    assert snapshot(config.parent) == before


@pytest.mark.parametrize(
    "case",
    [
        "copy",
        "replace",
        "revoked",
        "expired",
        "owner",
        "latch",
        "capacity",
        "capability",
        "identity",
        "connection",
        "missing",
        "legacy_schema",
    ],
)
def test_OP07_doctor_failures_are_readonly_and_replay_refuses(operator, case, monkeypatch):
    config, request, provider = operator
    setup(config, request)
    values = read_private(config)
    if case == "copy":
        target = config.parent / "replacement.sqlite"
        shutil.copyfile(values["core_database"], target)
        target.chmod(0o600)
        os.replace(target, values["core_database"])
    elif case == "replace":
        Path(values["core_database"]).unlink()
        Path(values["core_database"]).touch(mode=0o600)
    elif case in {"revoked", "latch"}:
        journal = AuthorityJournal(values["authority_database"], values["authority_key"])
        ref = (
            "session-revoked:"
            + __import__("ai_task_manager.config.files", fromlist=["digest"]).digest(
                values["grant_ref"]
            )
            if case == "revoked"
            else activation_reference(config, values["workflow_control"])
        )
        bind_record(journal, ref, {"authored_control": True})
    elif case == "expired":
        monkeypatch.setattr(time, "time", lambda: request["deadline"] + 1)
    elif case == "owner":
        write_private(values["workflow_control"]["record"], {"authored_control": True})
    elif case == "capacity":
        with Database(values["core_database"]).transaction() as conn:
            conn.execute("UPDATE capacity SET host_limit=2")
    elif case == "capability":
        data = read_private(values["session_file"])
        write_private(
            values["session_file"], {**data, "capability": "invalid-capability-value"}, replace=True
        )
    elif case == "identity":
        provider.actual = provider.expected.__class__(
            str(__import__("uuid").uuid4()),
            provider.expected.workspace_id,
            provider.expected.team_id,
            "TST",
            provider.expected.project_id,
        )
    elif case == "connection":
        provider.agent_id = str(__import__("uuid").uuid4())
    elif case == "missing":
        Path(values["authority_database"]).unlink()
    else:
        with sqlite3.connect(values["core_database"]) as conn:
            conn.execute("UPDATE metadata SET version=4")
    before = snapshot(config.parent)
    observed = doctor(config)
    assert not observed["ok"] and observed["exit_code"] != 0
    assert snapshot(config.parent) == before
    with pytest.raises(Exception):
        setup(config, request)
    assert snapshot(config.parent) == before


def test_OP08_absent_doctor_does_not_create_and_cli_redacts_secret(operator, capsys):
    config, request, _ = operator
    before = snapshot(config.parent.parent)
    code, observed, raw = call(capsys, config, "doctor")
    assert code == 4 and not observed["data"]["ready"]
    assert snapshot(config.parent.parent) == before
    request["linear"]["credential"] = {"value": "AUTHORED-SECRET-DO-NOT-PRINT"}
    request_file = config.parent.parent / "bad.json"
    request_file.write_text(json.dumps(request))
    code, observed, raw = call(capsys, config, "setup", request_file)
    assert code != 0 and "AUTHORED-SECRET-DO-NOT-PRINT" not in raw
    assert not config.parent.exists()


def test_OP09_file_credential_reference_keeps_private_mode_guard(operator):
    config, request, provider = operator
    request["linear"]["credential"] = {
        "file": provider.linear["credential_file"],
        "key": "LINEAR_API_KEY",
    }
    setup(config, request)
    assert doctor(config)["ok"]
    Path(provider.linear["credential_file"]).chmod(0o644)
    assert not doctor(config)["ok"]


def test_OP10_closed_parser_rejects_imports_and_nonobjects():
    for value in (None, [], {"factory": "unsafe.module"}):
        with pytest.raises(Exception):
            parse(value)


def test_OP11_writer_runtime_dependency_is_required_without_resolving_venv(operator):
    import venv

    import yaml

    config, request, _ = operator
    runtime = config.parent.parent / "writer-runtime"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(runtime)
    python = runtime / "bin/python"
    request["writer"]["python"] = str(python)
    request["writer"]["python_sha256"] = hashlib.sha256(python.read_bytes()).hexdigest()
    with pytest.raises(AccessDenied, match="validator runtime is unavailable"):
        setup(config, request)
    assert not config.parent.exists()
    dependencies = (
        runtime / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    )
    copied = dependencies / "yaml"
    shutil.copytree(Path(yaml.__file__).parent, copied)
    setup(config, request)
    assert doctor(config)["ok"]
    shutil.rmtree(copied)
    before = snapshot(config.parent)
    observed = doctor(config)
    assert not observed["ok"]
    assert {row["check"]: row["verdict"] for row in observed["data"]["checks"]}["writing"] == "fail"
    assert snapshot(config.parent) == before


@pytest.mark.parametrize("case", ["foreign_file", "wal", "schema", "attempt", "outbox"])
def test_OP12_doctor_refuses_unsafe_or_unsettled_state_without_writes(operator, case):
    config, request, _ = operator
    setup(config, request)
    values = read_private(config)
    if case == "foreign_file":
        target = config.parent / "foreign.json"
        target.write_text("{}")
        target.chmod(0o600)
    elif case == "schema":
        with sqlite3.connect(values["core_database"]) as conn:
            conn.execute("DROP TRIGGER aitm_publication_tasks_insert")
    elif case == "wal":
        with sqlite3.connect(values["core_database"]) as conn:
            assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    else:
        # Declare old execution/effect rows through registered SQLite writer controls.
        database = Database(values["core_database"])
        with database.transaction() as conn:
            sid = conn.execute("SELECT id FROM sessions").fetchone()[0]
            conn.execute("PRAGMA defer_foreign_keys=ON")
            conn.execute(
                "INSERT INTO tasks(id,session_id,version,data) VALUES('control-task',?,1,'{}')",
                (sid,),
            )
            if case == "attempt":
                conn.execute(
                    "INSERT INTO attempts VALUES('control-attempt','control-task',"
                    "?,'codex',0,'{}')",
                    (sid,),
                )
            else:
                conn.execute(
                    "INSERT INTO outbox(id,session_id,task_id,kind,payload,state) "
                    "VALUES('control-effect',?,'control-task','create','{}','uncertain')",
                    (sid,),
                )
    before = snapshot(config.parent)
    observed = doctor(config)
    assert not observed["ok"]
    assert snapshot(config.parent) == before
    with pytest.raises(Exception):
        setup(config, request)
    assert snapshot(config.parent) == before


def test_OP13_missing_credential_is_explicit_and_sanitized(operator, monkeypatch):
    config, request, _ = operator
    setup(config, request)
    monkeypatch.delenv("AITM_CREDENTIAL_OPERATOR_CONTROL")
    before = snapshot(config.parent)
    observed = doctor(config)
    assert not observed["ok"]
    assert "authored-local-credential" not in json.dumps(observed)
    assert any(
        row["check"] == "session_authority" and row["verdict"] == "fail"
        for row in observed["data"]["checks"]
    )
    assert snapshot(config.parent) == before


@pytest.mark.parametrize("crosses_deadline", [False, True])
def test_OP14_doctor_requires_authority_current_after_provider_reads(
    operator, monkeypatch, crosses_deadline
):
    config, request, _ = operator
    setup(config, request)
    current = [request["deadline"] - 1]
    monkeypatch.setattr(time, "time", lambda: current[0])
    successful_identity_responses = []
    send = HttpTransport.send

    def delayed_identity(self, query, variables, credentials):
        response = send(self, query, variables, credentials)
        if "query VerifyBinding" in query:
            assert response.status == 200
            successful_identity_responses.append(True)
            if crosses_deadline:
                # Model a successful response arriving after expiry without a timing race.
                current[0] = request["deadline"] + 1
        return response

    monkeypatch.setattr(HttpTransport, "send", delayed_identity)
    before = snapshot(config.parent)
    observed = doctor(config)
    assert successful_identity_responses
    assert snapshot(config.parent) == before
    assert observed["data"]["ready"] is not crosses_deadline
    assert observed["ok"] is not crosses_deadline
    if crosses_deadline:
        assert observed["exit_code"] != 0


@pytest.mark.parametrize("crosses_deadline", [False, True])
def test_OP15_setup_requires_authority_current_before_completion(
    operator, monkeypatch, crosses_deadline
):
    config, request, _ = operator
    current = [request["deadline"] - 1]
    monkeypatch.setattr(time, "time", lambda: current[0])
    successful_connection_responses = []
    send = HttpTransport.send

    def delayed_final_connection(self, query, variables, credentials):
        response = send(self, query, variables, credentials)
        if "query AgentUsers" in query:
            assert response.status == 200
            successful_connection_responses.append(True)
            # The first connection read is composition; the second completes establishment.
            if crosses_deadline and len(successful_connection_responses) == 2:
                current[0] = request["deadline"] + 1
        return response

    monkeypatch.setattr(HttpTransport, "send", delayed_final_connection)
    if crosses_deadline:
        with pytest.raises(AccessDenied):
            setup(config, request)
        assert len(successful_connection_responses) == 2
        assert (config.parent / "operator-intent.json").exists()
        assert not (config.parent / "operator-result.json").exists()
        assert read_private(config)["grant_ref"]
        assert (config.parent / "session.json").exists()
        before = snapshot(config.parent)
        assert not doctor(config)["ok"]
        with pytest.raises(AccessDenied):
            setup(config, request)
        assert snapshot(config.parent) == before
    else:
        assert setup(config, request)["ok"]
        assert len(successful_connection_responses) == 2
        assert (config.parent / "operator-result.json").exists()
        before = snapshot(config.parent)
        assert doctor(config)["ok"]
        assert snapshot(config.parent) == before
