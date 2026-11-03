"""Authored credential controls, owned HTTP and credential-free relay children."""

import json
import os
import sys
import time
import traceback
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from ai_task_manager.adapters.hosts.bridge.policy import clean_environment
from ai_task_manager.adapters.hosts.transports.process import HostProcess
from ai_task_manager.adapters.linear.errors import LinearError, ScopeMismatch
from ai_task_manager.adapters.linear.transport import HttpTransport
from ai_task_manager.application.service import linear
from ai_task_manager.config.files import MAX_JSON_BYTES, read_private, strict_json, write_private
from ai_task_manager.config.service import load_service
from ai_task_manager.domains.work.errors import AccessDenied, ValidationError
from ai_task_manager.installation.sessions import setup
from ai_task_manager.supervisor.models import Command
from tests.session_observations.helpers import OwnedProvider

NAME = "Operator_Selected_Linear_Credential_135"
SECRET = "authored-credential-135-never-persist"


@pytest.mark.parametrize(
    "name",
    ["PATH", "HOME", "USER", "LANG", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_ENTRYPOINT", "CODEX_HOME"],
)
def test_EC11_native_environment_names_cannot_select_credentials(monkeypatch, name):
    monkeypatch.setenv(name, "authored-collision-credential")
    with pytest.raises(ValidationError, match="reserved"):
        linear.CredentialEnvironment(name)


@pytest.fixture
def provider(tmp_path):
    value = OwnedProvider(tmp_path / "provider")
    try:
        yield value
    finally:
        value.close()


def select_environment(provider, monkeypatch):
    monkeypatch.setenv(NAME, SECRET)
    provider.linear = {
        "expected": provider.linear["expected"],
        "credential_environment": NAME,
        "journal": provider.linear["journal"],
    }
    config = read_private(provider.config)
    config["linear"] = provider.linear
    write_private(provider.config, config, replace=True)


def test_EC01_private_file_unchanged_and_environment_does_not_bypass(provider, monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", SECRET)
    assert provider.components().binding.expected == provider.expected
    path = Path(provider.linear["credential_file"])
    path.chmod(0o644)
    before = path.stat()
    with pytest.raises(AccessDenied, match="mode 600"):
        provider.components()
    assert path.stat().st_ino == before.st_ino and path.stat().st_mode == before.st_mode
    path.chmod(0o600)
    path.write_text("LINEAR_API_KEY=one\nLINEAR_API_KEY=two\n")
    with pytest.raises(AccessDenied, match="duplicated"):
        provider.components()
    path.unlink()
    path.symlink_to(provider.config)
    with pytest.raises(AccessDenied, match="symbolic"):
        provider.components()


def test_EC01_malformed_file_has_redacted_traceback(tmp_path):
    path = tmp_path / "credential.env"
    path.write_bytes(b"LINEAR_API_KEY=" + SECRET.encode() + b"\xff")
    path.chmod(0o600)
    with pytest.raises(AccessDenied):
        try:
            linear.CredentialFile(path, "LINEAR_API_KEY")()
        except Exception:
            assert SECRET not in traceback.format_exc()
            raise


@pytest.mark.parametrize(
    "contents",
    [
        "",
        "OTHER_KEY=one\n",
        "LINEAR_API_KEY=\n",
        "LINEAR_API_KEY=one two\n",
        "LINEAR_API_KEY='unterminated\n",
    ],
)
def test_EC01_missing_empty_or_ambiguous_file_value_refuses(tmp_path, monkeypatch, contents):
    monkeypatch.setenv("LINEAR_API_KEY", SECRET)
    path = tmp_path / "credential.env"
    path.write_text(contents)
    path.chmod(0o600)
    with pytest.raises(AccessDenied):
        linear.CredentialFile(path, "LINEAR_API_KEY")()
    path.unlink()
    with pytest.raises(AccessDenied):
        linear.CredentialFile(path, "LINEAR_API_KEY")()


def test_EC02_operator_selected_environment_uses_existing_client(provider, monkeypatch):
    select_environment(provider, monkeypatch)
    components = provider.components()
    assert components.client.credentials().value == SECRET
    assert SECRET not in repr(components.client.credentials())
    assert components.binding.expected == provider.expected
    assert provider.requests == 3


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        " ",
        "key with space",
        "key\nvalue",
        "key\r",
        "key\x00",
        "key\t",
        "key\x7f",
        "key\u2603",
        "a" * (MAX_JSON_BYTES + 1),
    ],
)
def test_EC03_missing_or_malformed_environment_redacts(provider, monkeypatch, value):
    select_environment(provider, monkeypatch)
    monkeypatch.setenv("LINEAR_API_KEY", SECRET)
    if value is None:
        monkeypatch.delenv(NAME)
    elif "\x00" in value:
        # An injected mapping exercises a malformed value that the OS itself rejects.
        monkeypatch.setattr(linear.os, "environ", {NAME: value})
    else:
        monkeypatch.setenv(NAME, value)
    with pytest.raises(AccessDenied):
        try:
            provider.components()
        except Exception:
            message = traceback.format_exc()
            assert SECRET not in message
            if value and len(value) > 1:
                assert value not in message
            raise
    assert provider.requests == 0


@pytest.mark.parametrize(
    "name", [None, [], 4, "", "1TOKEN", " TOKEN", "TOKEN-X", "TOKEN=bad", "TOKEN\n", "\u2603"]
)
def test_EC04_malformed_names_refuse_before_provider(provider, name):
    provider.linear = {
        "expected": provider.linear["expected"],
        "journal": provider.linear["journal"],
        "credential_environment": name,
    }
    with pytest.raises(ValidationError):
        provider.components()
    assert provider.requests == 0


@pytest.mark.parametrize(
    "change",
    [
        {"credential_file": "/must-not-read"},
        {"credential_key": "LINEAR_API_KEY"},
        {"credential": SECRET},
        {"source": "environment"},
        {"credential_environment": None},
    ],
)
def test_EC05_closed_sources_refuse(provider, monkeypatch, change):
    select_environment(provider, monkeypatch)
    provider.linear.update(change)
    with pytest.raises(ValidationError):
        provider.components()
    assert provider.requests == 0


def test_EC05_duplicate_keys_and_incomplete_sources_refuse(provider):
    with pytest.raises(ValidationError, match="Duplicate"):
        strict_json('{"credential_environment":"FIRST","credential_environment":"SECOND"}')
    for key in ("credential_file", "credential_key"):
        config = dict(provider.linear)
        del config[key]
        with pytest.raises(ValidationError):
            linear.LinearComponents(config)
    assert provider.requests == 0


@pytest.mark.parametrize(
    "field", ["viewer_id", "workspace_id", "team_id", "team_key", "project_id"]
)
def test_EC06_wrong_identity_refuses(provider, monkeypatch, field):
    select_environment(provider, monkeypatch)
    provider.actual = replace(
        provider.actual, **{field: "FOREIGN" if field == "team_key" else str(uuid4())}
    )
    with pytest.raises(ScopeMismatch):
        provider.components()
    assert provider.requests == 1


def test_EC07_rotation_and_missing_selected_name_refuse(provider, monkeypatch):
    select_environment(provider, monkeypatch)
    client = provider.components().client
    monkeypatch.setenv(NAME, "changed-authored-credential")
    with pytest.raises(LinearError, match="credential_binding_changed"):
        client.request("query NoEffect { viewer { id } }", {})
    assert provider.requests == 3
    monkeypatch.delenv(NAME)
    monkeypatch.setenv("LINEAR_API_KEY", SECRET)
    with pytest.raises(AccessDenied):
        client.credentials()


def test_EC08_EC09_session_setup_persistence_and_model_boundary(provider, monkeypatch, capsys):
    select_environment(provider, monkeypatch)
    monkeypatch.setattr(HttpTransport, "ENDPOINT", provider.endpoint)
    config = read_private(provider.config)
    external = os.environ.get("ENVIRONMENT_CREDENTIAL_FIXTURE")
    if external:
        config["core_database"] = external
        write_private(provider.config, config, replace=True)
    result = setup(
        provider.config,
        logical_session="authored-environment-session",
        actions=("enqueue",),
        deadline=provider.deadline,
        operation_key="environment-setup",
    )
    assert result["capability_saved"]
    service = load_service(provider.config)
    assert service.call("session_show", {})["ok"]
    service.decisions.observations.verify_enrollment(service.grant_ref)
    before = provider.requests
    assert not service.call("session_show", {"credential_environment": "MODEL_SELECTED_NAME"})["ok"]
    assert provider.requests == before
    updated = read_private(provider.config)
    updated["linear"]["credential_environment"] = "MODEL_SELECTED_NAME"
    write_private(provider.config, updated, replace=True)
    assert not service.call("session_show", {})["ok"]
    assert provider.requests == before
    for path in provider.root.rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes(), str(path)
    if external:
        assert SECRET.encode() not in Path(external).read_bytes()
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err + json.dumps(result)


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_EC10_actual_relay_child_omits_parent_credentials(tmp_path, monkeypatch, host):
    monkeypatch.setenv(NAME, SECRET)
    monkeypatch.setenv("UNRELATED_PROVIDER_TOKEN_135", "authored-unrelated-parent-secret")
    home = tmp_path / "home"
    home.mkdir()
    profile = home / (".claude_roni" if host == "claude" else "codex-profile")
    profile.mkdir(mode=0o700)
    environment = clean_environment(
        home, **{"claude_profile" if host == "claude" else "codex_home": profile}
    )
    assert NAME not in environment and SECRET not in environment.values()
    program = (
        "import json,os,sys\n"
        "for line in sys.stdin:\n"
        " print(json.dumps({"
        "'selected': 'Operator_Selected_Linear_Credential_135' in os.environ, "
        "'unrelated': 'UNRELATED_PROVIDER_TOKEN_135' in os.environ}),flush=True)\n"
    )
    command = Command((sys.executable, "-I", "-u", "-c", program), tmp_path, environment, 10)
    process = HostProcess(command, tmp_path / "output", deadline=time.time() + 15)
    try:
        process.start()
        process.write("{}\n")
        assert process.read(3) == {"selected": False, "unrelated": False}
    finally:
        if process.group is not None:
            assert process.stop().confirmed
    for path in (tmp_path / "output").iterdir():
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes()
