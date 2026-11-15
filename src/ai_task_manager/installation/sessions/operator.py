"""Trusted installed setup and read-only diagnostics over existing session authority."""

import hashlib
import importlib.metadata
import json
import os
import re
import sqlite3
import stat
import subprocess
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.decisions.observations import (
    SessionObservations,
    current_enrollment,
    identity,
    intent,
)
from ai_task_manager.adapters.linear.client import LinearClient
from ai_task_manager.adapters.linear.connections import AgentConnection
from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.linear.models import ExpectedBinding
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.adapters.workflow_control.identity import settings, source_digest
from ai_task_manager.adapters.writing.package import WriterPackage
from ai_task_manager.application.service.linear import CredentialEnvironment, CredentialFile
from ai_task_manager.application.service.models import result
from ai_task_manager.config.capacity import policies
from ai_task_manager.config.claim_store import file_identity
from ai_task_manager.config.files import FileSeal, digest, private_path, read_private, write_private
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired, IdempotencyConflict
from ai_task_manager.domains.work.models import SessionHandle
from ai_task_manager.storage.database import Database
from ai_task_manager.storage.sessions import resolve

from . import setup as trusted_setup
from .operator_schema import absolute, parse
from .recovery import history, installed_authority, installed_provider_history, setup_lock

INTENT = "operator-intent.json"
COMPLETE = "operator-result.json"
STATE_FILES = {
    INTENT,
    COMPLETE,
    "core.sqlite",
    "authority.sqlite",
    "authority.key",
    "session.json",
    "provider.sqlite",
    "provider.sqlite.dependencies.sqlite",
    "provider.sqlite.dependencies.key",
    "writing",
    "writing.sqlite",
    "writing.key",
    "owner.json",
    "owner.sock",
    "operator.session-setup.lock",
}


def directory(path):
    path = absolute(str(path))
    if path.resolve() != path or not path.is_dir():
        raise AccessDenied("Operator storage must use physical existing directories")
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise AccessDenied("Operator storage must be owned with mode 700")
    return path


def writer_package(values, *, probe=False):
    root, python = absolute(values["root"]), absolute(values["python"])
    if root.resolve() != root or python.parent.resolve() != python.parent:
        raise AccessDenied("Writer source and interpreter parent must be physical")
    info = python.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or not os.access(python, os.X_OK)
        or re.fullmatch(r"python(?:3(?:\.\d+)?)?", python.name) is None
        or hashlib.sha256(python.read_bytes()).hexdigest() != values["python_sha256"]
    ):
        raise AccessDenied("Approved Python interpreter pin does not match")
    package = WriterPackage(root, str(python), values["pins"])
    policy = package.verify()
    environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
        if key in os.environ
    }
    # Keep the invocation path: resolving a venv Python symlink loses its dependencies.
    completed = subprocess.run(
        [str(python), "-B", "-E", "-s", str(root / "scripts/validate_document.py"), "--help"],
        cwd=root,
        env=environment,
        capture_output=True,
        timeout=5,
        check=False,
    )
    if completed.returncode:
        raise AccessDenied("Approved writer validator runtime is unavailable")
    package.verify()
    if probe:
        completed = subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                "import sys; print(sys.implementation.name); print(sys.version_info >= (3, 12))",
            ],
            env=environment,
            capture_output=True,
            timeout=5,
            check=False,
        )
        if completed.returncode or completed.stdout != b"cpython\nTrue\n":
            raise AccessDenied("Approved interpreter must be supported CPython")
    return package, policy


def configuration(config_file, request, control, policy):
    root = Path(request["state_directory"])
    linear = request["linear"]
    credential = linear["credential"]
    reference = (
        {"credential_environment": credential["environment"]}
        if "environment" in credential
        else {"credential_file": credential["file"], "credential_key": credential["key"]}
    )
    return {
        "version": 1,
        "core_database": str(root / "core.sqlite"),
        "authority_database": str(root / "authority.sqlite"),
        "authority_key": str(root / "authority.key"),
        "session_file": str(root / "session.json"),
        "approval_policy": asdict(policy),
        "linear": {
            "expected": linear["expected"],
            "agent_connection": linear["agent_connection"],
            "journal": str(root / "provider.sqlite"),
            **reference,
        },
        "writer": {
            "root": request["writer"]["root"],
            "python": request["writer"]["python"],
            "pins": request["writer"]["pins"],
            "sessions_root": str(root / "writing"),
            "journal": str(root / "writing.sqlite"),
            "key": str(root / "writing.key"),
        },
        "capacity_policies": request["capacity_policies"],
        "workflow_control": settings(control),
    }


def selected_paths(config_file, request):
    root, config_file = absolute(request["state_directory"]), absolute(str(config_file))
    if (
        config_file.parent != root
        or config_file.name in STATE_FILES
        or config_file.name.endswith(".session-setup.lock")
    ):
        raise AccessDenied(
            "Configuration needs a distinct filename in the selected state directory"
        )
    package_root = Path(__file__).resolve().parents[2]
    if (
        root.is_relative_to(Path.cwd().resolve())
        or root.is_relative_to(package_root)
        or any(part in {".cache", "Caches", "site-packages", ".venv"} for part in root.parts)
    ):
        raise AccessDenied("Authoritative state must be outside the project, package and caches")
    if root.resolve() != root or not root.parent.is_dir():
        raise AccessDenied("State directory requires an existing physical parent")
    return root, config_file


def validate_files(root, config_file):
    """Reject unsafe local files before any storage constructor can run."""
    directory(root)
    allowed = STATE_FILES | {Path(config_file).name, Path(config_file).name + ".session-setup.lock"}
    for child in root.iterdir():
        if child.name not in allowed:
            raise AccessDenied("Foreign files in operator state require explicit reconciliation")
        if child.is_dir() and not child.is_symlink() and child.name == "writing":
            directory(child)
        else:
            identity(child)


def setup(config_file, request):
    request = parse(request)
    root, config_file = selected_paths(config_file, request)
    if request["deadline"] <= time.time():
        raise AccessDenied(
            "Session setup deadline has expired; this command cannot renew authority"
        )
    _, policy = writer_package(request["writer"], probe=True)
    if not root.exists():
        root.mkdir(mode=0o700)
    directory(root)
    with setup_lock(root / "operator"):
        record_path, completed_path = root / INTENT, root / COMPLETE
        if record_path.exists():
            record = read_private(record_path)
            if (
                record.get("request") != request
                or record.get("config_file") != str(config_file)
                or record.get("source") != source_digest()
            ):
                raise IdempotencyConflict(
                    "Installation intent differs; setup cannot replace or renew it"
                )
            if not completed_path.exists():
                raise EvidenceRequired(
                    "Incomplete operator setup retained; reconcile private intent and owned files"
                )
            config = read_private(config_file)
            expected = configuration(config_file, request, record["workflow_control"], policy)
            if intent(config) != digest(expected):
                raise AccessDenied("Installed configuration differs from the original setup intent")
            completed = read_private(completed_path)
            if completed.get("config") != identity(config_file, content=True) or completed.get(
                "request_digest"
            ) != digest(request):
                raise AccessDenied("Completed operator setup binding changed")
            validate_files(root, config_file)
            # Check existing readiness without constructing mutable services.
            readiness = doctor(config_file)
            if not readiness["ok"]:
                raise AccessDenied(
                    "Installed session is not ready; inspect session doctor diagnostics"
                )
        else:
            if set(root.iterdir()) != {root / "operator.session-setup.lock"}:
                raise AccessDenied("Initial setup cannot adopt existing foreign or partial files")
            control = settings(
                {
                    "record": str(root / "owner.json"),
                    "endpoint": str(root / "owner.sock"),
                    "instance": str(uuid4()),
                }
            )
            record = {
                "version": 1,
                "request": request,
                "config_file": str(config_file),
                "source": source_digest(),
                "workflow_control": control,
            }
            write_private(record_path, record)
            write_private(config_file, configuration(config_file, request, control, policy))
        established = trusted_setup(
            config_file,
            logical_session=request["logical_session"],
            actions=tuple(request["actions"]),
            deadline=request["deadline"],
            operation_key=request["operation_key"],
        )
        config = read_private(config_file)
        installed_authority(config)
        current_authority(
            config_file,
            config,
            ReadOnlyAuthority(config["authority_database"], config["authority_key"]),
            established["enrollment_ref"],
            established["scope"],
            request["deadline"],
        )
        if not completed_path.exists():
            write_private(
                completed_path,
                {
                    "version": 1,
                    "request_digest": digest(request),
                    "config": identity(config_file, content=True),
                },
            )
        return result(established)


@contextmanager
def readonly(path):
    path = private_path(path)
    identity(path)
    # WAL reads may create shared-memory files. Refuse that mode before opening SQLite.
    with path.open("rb") as stream:
        header = stream.read(20)
    if header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
        raise AccessDenied("Read-only diagnostics require an existing rollback-journal store")
    original = identity(path)
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        yield conn
        if identity(path) != original:
            raise AccessDenied("Observed store was replaced during diagnostics")
    finally:
        conn.close()


class ReadOnlyAuthority(AuthorityJournal):
    """Reuse signature and enrollment verification without the initializing constructor."""

    def __init__(self, path, key_path):
        self.path, self.key_path, self.clock = Path(path), Path(key_path), time.time
        self._key = private_path(key_path).read_bytes()
        if len(self._key) != 32:
            raise AccessDenied("Installed authority key is invalid")
        with self.transaction() as conn:
            if [tuple(r) for r in conn.execute("SELECT version FROM authority_version")] != [(2,)]:
                raise AccessDenied("Installed authority schema requires reconciliation")

    @contextmanager
    def transaction(self):
        with readonly(self.path) as conn:
            yield conn


def current_authority(config_file, config, journal, reference, scope, deadline):
    """Observe signed local authority after provider I/O, without granting or renewing it."""
    enrollment = current_enrollment(journal, reference, scope)
    if (
        config.get("grant_ref") != reference
        or enrollment["config_path"] != str(Path(config_file).absolute())
        or enrollment["config_intent"] != intent(config)
        or enrollment["expires_at"] != deadline
        or deadline <= time.time()
    ):
        raise AccessDenied("Current unchanged session delegation required before completion")
    return True


def provider_client(config):
    linear = config["linear"]
    credential = (
        CredentialEnvironment(linear["credential_environment"])
        if "credential_environment" in linear
        else CredentialFile(linear["credential_file"], linear["credential_key"])
    )
    client = LinearClient(HttpTransport(), credential, ExpectedBinding(**linear["expected"]))
    selected = AgentConnection(**linear["agent_connection"])
    client.agent_connection = lambda: selected
    return client


def doctor(config_file):
    """Observe local state and provider authority; never construct mutable services."""
    checks, state = [], {}

    def check(name, observe, needs=()):
        if any(dependency not in state for dependency in needs):
            checks.append(
                {"check": name, "verdict": "unavailable", "reason": "prerequisite_failed"}
            )
            return
        try:
            state[name] = observe()
            checks.append({"check": name, "verdict": "pass"})
        except Exception as exc:
            unavailable = isinstance(exc, (OSError, sqlite3.Error)) or (
                isinstance(exc, LinearError)
                and exc.category
                in {"transport_failure", "transport_deadline", "rate_limited", "http_failure"}
            )
            checks.append(
                {
                    "check": name,
                    "verdict": "unavailable" if unavailable else "fail",
                    "reason": "observation_unavailable" if unavailable else "binding_refused",
                }
            )

    def installation():
        config = read_private(config_file)
        root = directory(Path(config_file).parent)
        record = read_private(root / INTENT)
        request = parse(record["request"])
        selected_paths(config_file, request)
        completed = read_private(root / COMPLETE)
        if (
            record["config_file"] != str(absolute(str(config_file)))
            or record["source"] != source_digest()
            or completed["request_digest"] != digest(request)
            or completed["config"] != identity(config_file, content=True)
            or importlib.metadata.version("ai-task-manager") != "0.1.0"
        ):
            raise AccessDenied("Installed package or setup binding changed")
        state.update(config=config, request=request, record=record, seal=FileSeal(config_file))
        return True

    def writing():
        _, policy = writer_package(state["request"]["writer"])
        expected = configuration(
            config_file, state["request"], state["record"]["workflow_control"], policy
        )
        if intent(state["config"]) != digest(expected):
            raise AccessDenied("Configuration or writing policy differs from setup intent")
        return True

    def paths():
        config = state["config"]
        validate_files(directory(state["request"]["state_directory"]), config_file)
        for name in ("core_database", "authority_database", "authority_key", "session_file"):
            identity(config[name])
        identity(config["linear"]["journal"])
        if "credential_file" in config["linear"]:
            identity(config["linear"]["credential_file"])
        return True

    def authority():
        config = state["config"]
        installed_authority(config)
        journal = ReadOnlyAuthority(config["authority_database"], config["authority_key"])
        installed_provider_history(config)
        state["journal"] = journal
        enrollment = journal._get(config["grant_ref"], "session-enrollment:")
        if enrollment["actions"] != state["request"]["actions"]:
            raise AccessDenied("Installed delegation changed")
        client = provider_client(config)
        grant = SessionObservations(
            journal, client, config_file, config["session_observations"]
        ).verify_enrollment(config["grant_ref"])
        client.check_connection(grant.scope, refresh=True)
        state.update(grant=grant, client=client)
        return True

    def claim():
        config, journal = state["config"], state["journal"]
        enrollment = journal.claim_store()
        with readonly(config["core_database"]) as conn:
            from ai_task_manager.storage.publication_schema import validate as validate_publication
            from ai_task_manager.storage.scheduling_schema import validate as validate_scheduling

            validate_scheduling(conn)
            validate_publication(conn)
            if (
                [tuple(r) for r in conn.execute("SELECT version FROM metadata")] != [(5,)]
                or Database.identity(conn) != enrollment["store_id"]
                or file_identity(config["core_database"]) != enrollment["file_identity"]
                or conn.execute("PRAGMA journal_mode").fetchone()[0] != "delete"
            ):
                raise AccessDenied("Original current claim store is required")
            data = read_private(config["session_file"])
            if set(data) != {"version", "capability", "scope"} or data["version"] != 1:
                raise AccessDenied("Installed session format changed")
            sid, scope = resolve(conn, SessionHandle(data["capability"]))
            if asdict(scope) != data["scope"] or scope != state["grant"].scope:
                raise AccessDenied("Installed session capability scope changed")
            state["session_id"] = sid
        with readonly(config["linear"]["journal"]) as conn:
            if [tuple(r) for r in conn.execute("SELECT version FROM metadata")] != [(4,)]:
                raise AccessDenied("Provider schema requires reconciliation")
            encoded = json.dumps(asdict(scope), sort_keys=True, separators=(",", ":"))
            principal = conn.execute(
                "SELECT 1 FROM principals WHERE scope=?", (encoded,)
            ).fetchone()
            connection = conn.execute(
                "SELECT connection_digest FROM agent_bindings WHERE scope=?", (encoded,)
            ).fetchone()
            if (
                not principal
                or not connection
                or connection[0] != state["client"].connection.digest
            ):
                raise AccessDenied("Installed provider principal or connection changed")
        return True

    def capacity():
        expected = policies(state["config"])
        if not expected:
            raise AccessDenied("Finite installed capacity is required")
        with readonly(state["config"]["core_database"]) as conn:
            actual = [
                tuple(r)
                for r in conn.execute(
                    "SELECT host,host_limit,project_limit FROM capacity ORDER BY host"
                )
            ]
        if actual != sorted((p.host, p.host_limit, p.project_limit) for p in expected):
            raise AccessDenied("Installed capacity differs from explicit finite policy")
        return True

    def preparation():
        from ai_task_manager.installation.workflows.bootstrap import activation_reference

        config, sid = state["config"], state["session_id"]
        control = settings(config["workflow_control"])
        for key in ("record", "endpoint"):
            path = Path(control[key])
            directory(path.parent)
            if path.exists() or path.is_symlink():
                raise AccessDenied("Existing owner state prevents preparation")
        if history(state["journal"], activation_reference(config_file, control)) is not None:
            raise AccessDenied("Owner activation permanently closed preparation")
        with readonly(config["core_database"]) as conn:
            if conn.execute("SELECT 1 FROM attempts WHERE session_id=? LIMIT 1", (sid,)).fetchone():
                raise AccessDenied("Execution history prevents preparation")
            if conn.execute(
                "SELECT 1 FROM outbox WHERE session_id=? AND state IN "
                "('pending','leased','uncertain') LIMIT 1",
                (sid,),
            ).fetchone():
                raise AccessDenied("Unsettled effects prevent preparation")
        return True

    check("installation", installation)
    check("writing", writing, ("installation",))
    check("private_paths", paths, ("installation", "writing"))
    check("session_authority", authority, ("private_paths",))
    check("claim_store", claim, ("session_authority",))
    check("capacity", capacity, ("claim_store",))
    check("preparation", preparation, ("claim_store",))
    if "seal" in state:
        check("configuration_unchanged", state["seal"].check)
    check(
        "current_authority",
        lambda: current_authority(
            config_file,
            state["config"],
            state["journal"],
            state["grant"].source_ref,
            asdict(state["grant"].scope),
            state["grant"].expires_at,
        ),
        ("session_authority",),
    )
    ready = all(row["verdict"] == "pass" for row in checks)
    if ready:
        return result({"ready": True, "checks": checks})
    return {
        "ok": False,
        "outcome": "blocked",
        "data": {"ready": False, "checks": checks},
        "error": {"category": "blocked", "message": "Session readiness is not confirmed"},
        "exit_code": 4,
    }
