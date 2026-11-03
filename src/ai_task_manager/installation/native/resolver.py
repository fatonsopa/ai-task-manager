"""Fixed profile recipes and existing accounting; no import-string registration."""

from pathlib import Path

from ai_task_manager.adapters.hosts.bridge.accounting import HostInvocationAccounting
from ai_task_manager.adapters.hosts.bridge.admission import MANAGED_TOOLS, NativeSourcePins
from ai_task_manager.adapters.hosts.bridge.codex_home import CodexHomeBoundary
from ai_task_manager.adapters.hosts.bridge.codex_settings import CodexSettings
from ai_task_manager.adapters.hosts.bridge.policy import NativePolicy, policy_digest
from ai_task_manager.config.files import digest, read_private
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.workflows.registry import (
    NativeAuthorities,
    NativeRegistration,
    NativeRegistry,
)

from .authority import MissingNativeAuthority, NativePermission
from .manifest import NativeManifest


def resolve(
    config_file, installation_file, *, authority_source=None, commands=None, service_options=None
):
    installation = read_private(installation_file)
    path = installation.get("native_manifest")
    if path is None:
        return NativeRegistry()
    manifest = NativeManifest(path, config_file, installation_file)
    d = manifest.data
    pins = NativeSourcePins({str(manifest.seal.path): manifest.seal.sha256})
    expected = digest(
        {
            "reference": d["native_ref"],
            "host": d["host"],
            "model": d["model"],
            "effort": d["effort"],
            "account": d["account_hash"],
            "source": pins.fingerprint,
        }
    )
    if (
        installation.get("native_ref") != d["native_ref"]
        or installation.get("native_digest") != expected
    ):
        raise AccessDenied("Cold native reference/source digest differs from installation")
    if authority_source is None and d.get("authority_store") is not None:
        from .normal import NormalAuthoritySource

        authority_source = NormalAuthoritySource(manifest)
    source = authority_source or MissingNativeAuthority()
    permission = source.resolve(manifest.data["authorization_ref"], manifest)
    if type(permission) is not NativePermission:
        raise AccessDenied("Native authority is not a trusted permission capability")
    permission.check(manifest, installation["task_id"], installation["deadline"])
    d = manifest.data
    if commands is None and d.get("commands_manifest"):
        from ai_task_manager.installation.commands.manifest import CommandManifest

        pointer = d["commands_manifest"]
        commands = CommandManifest(pointer["path"], pointer["sha256"]).prepare
    policy = policy_builder(manifest)

    def prepare(context):
        manifest.check()
        permission.check(manifest, context.task.id, context.deadline)

        accounting = HostInvocationAccounting(
            permission.controller,
            context.core,
            context.handle,
            context.runtime_journal,
            context.host_journal,
            permission.plan,
            admissions=permission.admissions,
        )
        runner = commands(context) if commands is not None else None
        return NativeAuthorities(
            policy,
            accounting,
            accounting,
            "assignment",
            permission.decision_preparation,
            confined_commands=runner,
            command_refs=tuple(runner.references) if runner is not None else (),
        )

    registration = NativeRegistration(
        d["native_ref"], d["host"], manifest.profile, d["account_hash"], pins, prepare
    )
    if registration.fingerprint != installation["native_digest"]:
        raise AccessDenied("Cold native registration differs from installed reference digest")
    return NativeRegistry((registration,), service_options=service_options)


def policy_builder(manifest):
    """One fixed source-bound launch recipe shared by normal and canary hosts."""
    d = manifest.data
    settings = (
        CodexSettings(CodexHomeBoundary(Path(d["profile"])), d["model"], d["effort"])
        if d["host"] == "codex"
        else None
    )

    def policy(host, pair):
        manifest.check()
        if host != d["host"]:
            raise AccessDenied("Native host selection differs from installation")
        seals = (manifest.seal, manifest.config, manifest.installation)
        selected = (
            {"codex_settings": settings.fingerprint}
            if settings
            else {str(s.path): s.sha256 for s in seals}
        )
        return NativePolicy(
            host,
            manifest.executable,
            d["executable_sha256"],
            manifest.environment,
            pair.worktree,
            policy_digest(
                host,
                d["executable_sha256"],
                manifest.environment,
                pair.worktree,
                selected,
                model=d["model"],
                effort=d["effort"],
            ),
            seals,
            tuple(sorted(MANAGED_TOOLS)) if host == "claude" else (),
            d["model"],
            d["effort"],
            settings,
        )

    return policy
