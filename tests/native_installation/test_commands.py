"""Real Core/resource guards with an authored recording transport, never Docker proof."""

import hashlib
import json
import os
import socket
import sys
import tempfile
import time
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path

import pytest

from ai_task_manager.adapters.hosts.bridge.accounting import HostInvocationAccounting
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.config.files import write_private
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.commands import CommandDefinition, build
from ai_task_manager.installation.commands.manifest import CommandManifest
from ai_task_manager.installation.native import resolve
from ai_task_manager.installation.workflows.composition import NativeContext
from tests.native_installation.helpers import PermissionSource, bundle
from tests.runtime.helpers import RuntimeRig


@pytest.fixture
def owned_runtime(tmp_path):
    owned = tmp_path / "runtime"
    owned.mkdir(mode=0o700)
    runtime = RuntimeRig(owned)
    try:
        yield runtime
    finally:
        runtime.close()
        record = os.environ.get("NATIVE_INSTALL_RESOURCE_REPORT")
        if record:
            workers = [
                {"pid": w.process.pid, "returncode": w.process.poll()}
                for w in runtime.runtime.workers.values()
            ]
            assert all(w["returncode"] is not None for w in workers)
            with Path(record).open("a") as output:
                output.write(json.dumps({"workers": workers}) + "\n")


@pytest.fixture
def local_endpoint():
    directory = Path(tempfile.mkdtemp(prefix="aitm-native-", dir="/private/tmp"))
    local_socket = socket.socket(socket.AF_UNIX)
    try:
        local_socket.bind(str(directory / "socket"))
        yield directory / "socket"
    finally:
        local_socket.close()
        (directory / "socket").unlink(missing_ok=True)
        directory.rmdir()


@pytest.fixture
def installed_command(tmp_path, monkeypatch, request, owned_runtime, local_endpoint):
    runtime = owned_runtime
    dispatch = runtime.start()
    if getattr(request, "param", None) != "unadmitted":
        runtime.admit(dispatch)
    attempt = runtime.rig.core.get_attempt(runtime.rig.handle, dispatch.attempt_id)
    task = runtime.rig.core.get_task(runtime.rig.handle, attempt.task_id)
    journal = HostJournal(tmp_path / "host.sqlite", tmp_path / "host.key")
    context = NativeContext(
        runtime.rig.core,
        runtime.rig.handle,
        task,
        runtime.journal,
        journal,
        runtime.resources,
        runtime.runtime,
        time.time() + 30,
    )
    script = dispatch.resources.worktree / "verify.py"
    script.write_text("print('authored command input')\n")
    definition = CommandDefinition(
        "verify-report",
        "verify.py",
        hashlib.sha256(script.read_bytes()).hexdigest(),
        ("verify.py",),
    )
    # No Docker daemon or CLI is invoked. This socket is only a constructor capability.
    sources = []
    commands = build(
        context,
        docker_executable=Path(sys.executable).resolve(),
        docker_endpoint="unix://" + str(local_endpoint),
        image_id="sha256:" + "a" * 64,
        definitions=(definition,),
        check_sources=lambda: sources.append("checked"),
    )
    calls = []

    def recording(*args):
        calls.append(args)
        return {"authored_transport_call": len(calls)}

    monkeypatch.setattr(commands.runner, "execute_registered", recording)
    return runtime, dispatch, context, commands, definition, calls, sources


def invoke(fixture, reference="verify-report", resources=None):
    _, dispatch, _, commands, _, _, _ = fixture
    return commands.execute_registered(
        reference, resources or dispatch.resources, lambda: None, nullcontext, "original-key"
    )


def test_NI04_real_resource_binding_and_fixed_recipe(installed_command):
    _, dispatch, _, commands, definition, calls, sources = installed_command
    result = invoke(installed_command)
    assert result == {"authored_transport_call": 1}
    assert sources and calls[0][0] == definition.reference
    assert calls[0][1] == dispatch.resources and calls[0][-1] == "original-key"
    plan = commands.runner.plans[definition.reference]
    assert plan.argv == ("python", "-I", "verify.py") and plan.seconds == 8
    assert set(commands.runner.environment) == {"PATH", "HOME", "DOCKER_CONFIG", "DOCKER_HOST"}


@pytest.mark.parametrize("installed_command", ["unadmitted"], indirect=True)
def test_NI04_omitted_canonical_resource_admission_refuses(installed_command):
    with pytest.raises(AccessDenied, match="canonical attempt"):
        invoke(installed_command)
    assert installed_command[5] == []


@pytest.mark.parametrize(
    "fault", ["script", "reference", "environment", "deadline", "foreign-resource"]
)
def test_NI04_pre_effect_mismatch_refuses(installed_command, fault):
    _, dispatch, _, commands, _, calls, _ = installed_command
    reference, resources = "verify-report", None
    if fault == "script":
        (dispatch.resources.worktree / "verify.py").write_text("print('changed')")
    elif fault == "reference":
        reference = "arbitrary-shell"
    elif fault == "environment":
        commands.runner.environment["SECRET"] = "not-a-secret"
    elif fault == "deadline":
        commands.runner.deadline = 0
    else:
        resources = replace(dispatch.resources, database_id="foreign-database")
    with pytest.raises(AccessDenied):
        invoke(installed_command, reference, resources)
    assert calls == []


def test_NI05_lost_transport_reply_is_not_automatically_replayed(installed_command, monkeypatch):
    _, _, _, commands, _, calls, _ = installed_command

    def lost(*args):
        calls.append(args)
        raise OSError("authored lost transport reply")

    monkeypatch.setattr(commands.runner, "execute_registered", lost)
    with pytest.raises(OSError, match="lost transport"):
        invoke(installed_command)
    assert len(calls) == 1


def test_NI04_command_manifest_prepares_fixed_runner_and_rechecks_identity(
    installed_command, tmp_path
):
    _, _, context, commands, definition, _, _ = installed_command
    endpoint = Path(commands.runner.environment["DOCKER_HOST"][7:])
    info = endpoint.stat()
    executable = Path(sys.executable).resolve()
    path = tmp_path / "commands.json"
    data = {
        "version": 1,
        "manifest_path": str(path),
        "task_id": context.task.id,
        "prose_digest": context.task.prose_digest,
        "docker_executable": str(executable),
        "docker_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "docker_endpoint": "unix://" + str(endpoint),
        "endpoint_identity": {"path": str(endpoint), "device": info.st_dev, "inode": info.st_ino},
        "image_id": "sha256:" + "a" * 64,
        "commands": [
            {
                "reference": definition.reference,
                "recipe": "python-script",
                "script": definition.script,
                "script_sha256": definition.script_sha256,
                "files": list(definition.files),
                "seconds": definition.seconds,
            }
        ],
    }
    write_private(path, data)
    manifest = CommandManifest(path, hashlib.sha256(path.read_bytes()).hexdigest())
    prepared = manifest.prepare(context)
    assert prepared.references == ("verify-report",) and prepared.runner.jobs == {}
    foreign = replace(context, task=replace(context.task, id="foreign-task"))
    with pytest.raises(AccessDenied, match="another approved task"):
        manifest.prepare(foreign)
    swapped = tmp_path / "same-bytes.json"
    swapped.write_bytes(path.read_bytes())
    swapped.chmod(0o600)
    os.replace(swapped, path)
    with pytest.raises(AccessDenied):
        manifest.check()


def test_NI01_prepare_builds_existing_accounting_without_admitting_native(
    installed_command, tmp_path
):
    _, _, context, commands, _, _, _ = installed_command
    b = bundle(tmp_path / "native", task_id=context.task.id)
    # Trusted fixture preparation binds the same immutable parent deadline before resolve.
    spec = json.loads(b.installation.read_text())
    spec["deadline"] = context.deadline
    write_private(b.installation, spec, replace=True)
    from ai_task_manager.installation.native.manifest import installation_intent
    from tests.native_installation.helpers import refresh_digest

    b.data["installation"] = installation_intent(b.installation)
    write_private(b.manifest, b.data, replace=True)
    refresh_digest(b.installation, b.manifest, b.data)
    registry = resolve(
        b.config, b.installation, authority_source=PermissionSource(), commands=lambda c: commands
    )
    authority = registry.require("normal-native").prepare(context)
    authority.require()
    assert type(authority.launch_permits) is HostInvocationAccounting
    assert authority.model_accounting is authority.launch_permits
    assert authority.confined_commands is commands and authority.command_refs == ("verify-report",)
    with pytest.raises(AssertionError, match="No actual native"):
        authority.model_accounting.admissions.require_native_conformance()


@pytest.mark.parametrize(
    "field,value", [("recipe", "shell"), ("argv", ["sh", "-c", "bad"]), ("passed", True)]
)
def test_NI03_command_manifest_has_no_shell_or_verdict_fields(tmp_path, field, value):
    path = tmp_path / "commands.json"
    row = {
        "reference": "verify-report",
        "recipe": "python-script",
        "script": "verify.py",
        "script_sha256": "a" * 64,
        "files": ["verify.py"],
        "seconds": 8,
        field: value,
    }
    data = {
        "version": 1,
        "manifest_path": str(path),
        "task_id": "task",
        "prose_digest": "a" * 64,
        "docker_executable": "/never-executed",
        "docker_sha256": "a" * 64,
        "docker_endpoint": "unix:///never-opened",
        "endpoint_identity": {},
        "image_id": "sha256:" + "a" * 64,
        "commands": [row],
    }
    write_private(path, data)
    from ai_task_manager.domains.work.errors import ValidationError

    with pytest.raises(ValidationError):
        CommandManifest(path, hashlib.sha256(path.read_bytes()).hexdigest())
