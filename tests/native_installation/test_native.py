"""Cold intent may select a profile; it cannot manufacture startup or billing."""

import json
import os
from pathlib import Path

import pytest

from ai_task_manager.application.service.models import Unavailable
from ai_task_manager.config.files import write_private
from ai_task_manager.domains.work.errors import AccessDenied, ValidationError
from ai_task_manager.installation.native import NativeManifest, resolve
from ai_task_manager.installation.native.resolver import policy_builder
from tests.native_installation.helpers import PermissionSource, bundle


@pytest.mark.parametrize("version", [None, "", 301])
def test_explicit_invalid_cli_version_is_refused(tmp_path, version):
    b = bundle(tmp_path, "claude", "claude-sonnet-5-5")
    write_private(b.manifest, {**b.data, "cli_version": version}, replace=True)
    with pytest.raises(AccessDenied, match="native CLI version"):
        NativeManifest(b.manifest, b.config, b.installation)


@pytest.mark.parametrize("version", [None, "2.1.325"])
def test_cli_version_is_carried_from_sealed_manifest_to_policy(tmp_path, version):
    from types import SimpleNamespace

    b = bundle(tmp_path, "claude", "claude-sonnet-5-5")
    if version is not None:
        write_private(b.manifest, {**b.data, "cli_version": version}, replace=True)
    manifest = NativeManifest(b.manifest, b.config, b.installation)
    policy = policy_builder(manifest)("claude", SimpleNamespace(worktree=tmp_path))
    assert policy.cli_version == (version or "2.1.301")


@pytest.mark.parametrize(
    "host,model",
    [
        ("claude", "claude-opus-5-5"),
        ("claude", "claude-sonnet-5-5"),
        ("codex", "gpt-6.1-sol"),
        ("codex", "gpt-6-astra"),
    ],
)
def test_NI01_four_exact_high_profile_intents(tmp_path, host, model):
    b = bundle(tmp_path, host, model)
    before = {p: p.read_bytes() for p in Path(b.data["profile"]).iterdir()}
    source = PermissionSource()
    registry = resolve(b.config, b.installation, authority_source=source)
    native = registry.require("normal-native")
    assert native.host == host and native.profile.model == model and native.profile.effort == "high"
    assert source.calls == 1 and all(p.read_bytes() == v for p, v in before.items())
    # This resolves permission metadata only; no permit/model/startup is issued.


def test_NI02_missing_normal_supplier_is_explicit(tmp_path):
    b = bundle(tmp_path)
    with pytest.raises(Unavailable, match="not implemented"):
        resolve(b.config, b.installation)


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "fallback"),
        ("effort", "medium"),
        ("host", "fixture"),
        ("account_hash", "not-an-account"),
        ("passed", True),
        ("factory", "module:call"),
    ],
)
def test_NI02_NI03_unknown_or_self_asserted_native_configuration_refuses(tmp_path, field, value):
    b = bundle(tmp_path)
    data = {**b.data, field: value}
    write_private(b.manifest, data, replace=True)
    with pytest.raises((AccessDenied, ValidationError)):
        NativeManifest(b.manifest, b.config, b.installation)


def test_NI03_explicit_default_claude_profile_preserves_default_login_discovery(tmp_path):
    b = bundle(tmp_path, "claude", "claude-opus-5-5")
    selected = Path(b.data["home"]) / ".claude"
    selected.mkdir(mode=0o700)
    data = {**b.data, "profile": str(selected)}
    write_private(b.manifest, data, replace=True)
    manifest = NativeManifest(b.manifest, b.config, b.installation)
    assert manifest.data["profile"] == str(selected)
    assert "CLAUDE_CONFIG_DIR" not in manifest.environment


def test_NI03_copied_manifest_and_same_bytes_replacement_refuse(tmp_path):
    b = bundle(tmp_path)
    copy = tmp_path / "copy.json"
    copy.write_bytes(b.manifest.read_bytes())
    copy.chmod(0o600)
    with pytest.raises(AccessDenied, match="Copied"):
        NativeManifest(copy, b.config, b.installation)
    source = NativeManifest(b.manifest, b.config, b.installation)
    os.replace(copy, b.manifest)
    with pytest.raises(AccessDenied):
        source.check()


def test_NI02_wrong_digest_does_not_consume_authority(tmp_path):
    b = bundle(tmp_path)
    spec = json.loads(b.installation.read_text())
    spec["native_digest"] = "b" * 64
    b.installation.write_text(json.dumps(spec))
    source = PermissionSource()
    with pytest.raises(AccessDenied, match="digest differs"):
        resolve(b.config, b.installation, authority_source=source)
    assert source.calls == 0


def test_NI02_permission_data_flag_is_not_authority(tmp_path):
    b = bundle(tmp_path)

    class Wrong:
        def resolve(self, *args):
            return {"admitted": True}

    with pytest.raises(AccessDenied, match="trusted permission"):
        resolve(b.config, b.installation, authority_source=Wrong())


def test_NI03_live_profile_mode_change_refuses(tmp_path):
    b = bundle(tmp_path)
    source = NativeManifest(b.manifest, b.config, b.installation)
    Path(b.data["profile"]).chmod(0o777)
    with pytest.raises(AccessDenied, match="profile directory"):
        source.check()


@pytest.mark.parametrize("selected", ["config", "installation", "manifest", "executable"])
def test_NI03_sealed_input_same_bytes_replacement_refuses(tmp_path, selected):
    b = bundle(tmp_path)
    executable = tmp_path / "never-executed"
    executable.write_text("#!/usr/bin/env false\n")
    executable.chmod(0o700)
    import hashlib

    b.data.update(
        executable=str(executable),
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
    )
    write_private(b.manifest, b.data, replace=True)
    source = NativeManifest(b.manifest, b.config, b.installation)
    target = executable if selected == "executable" else getattr(b, selected)
    copy = tmp_path / "replacement"
    copy.write_bytes(target.read_bytes())
    copy.chmod(target.stat().st_mode & 0o777)
    os.replace(copy, target)
    with pytest.raises(AccessDenied):
        source.check()
