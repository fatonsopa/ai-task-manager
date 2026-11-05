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
    config_file,
    installation_file,
    *,
    authority_source=None,
    commands=None,
    service_options=None,
    role="worker",
):
    installation = read_private(installation_file)
    if role not in {"worker", "reviewer"} or installation.get("role", "worker") != role:
        raise AccessDenied("Worker workflow cannot use a lead or reviewer installation")
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

        authority_source = NormalAuthoritySource(manifest, role=role)
    source = authority_source or MissingNativeAuthority()
    permission = source.resolve(manifest.data["authorization_ref"], manifest)
    if type(permission) is not NativePermission:
        raise AccessDenied("Native authority is not a trusted permission capability")
    permission.check(manifest, installation["task_id"], installation["deadline"])
    d = manifest.data
    custom_commands = commands is not None
    if commands is None and d.get("commands_manifest"):
        from ai_task_manager.installation.commands.manifest import CommandManifest

        pointer = d["commands_manifest"]
        commands = CommandManifest(pointer["path"], pointer["sha256"]).prepare
    code_registrations = ()
    if "code_ref" in installation or d.get("code_manifest") is not None:
        if role != "worker" or not {"code_ref", "code_digest"} <= set(installation):
            raise AccessDenied("Code provisioning requires an exact worker installation")
        if custom_commands:
            raise AccessDenied("Code provisioning requires its declared command manifest")
        if d.get("code_manifest") is None or d.get("commands_manifest") is None:
            raise AccessDenied("Declared code and command provisioning sources are required")
        from .code_provisioning import CodeProvisioning

        provisioning = CodeProvisioning(
            d["code_manifest"], d["commands_manifest"], installation, parent_check=manifest.check
        )
        code_registrations = (provisioning.registration,)
        commands = provisioning.prepare_commands
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
        worker_git = None
        if context.task.code_bearing:
            from ai_task_manager.adapters.git.commits import ConfinedWorkerGit, WorkerGitContainer
            from ai_task_manager.installation.commands.registry import InstalledCommands

            if type(runner) is not InstalledCommands:
                raise AccessDenied("Code work requires installed confined verification commands")
            images = {plan.image_id for plan in runner.runner.plans.values()}
            if len(images) != 1:
                raise AccessDenied("One exact installed worker Git image is required")
            runner.check_sources()
            container = WorkerGitContainer(
                context.runtime_journal,
                context.runtime_journal.path.parent / "worker-git-containers",
                docker=runner.runner.docker,
                docker_sha256=runner.runner.docker_hash,
                endpoint=runner.runner.environment["DOCKER_HOST"],
                image=next(iter(images)),
                resource_root=context.resources.root,
                deadline=context.deadline,
                run_id=manifest.data["authorization_ref"],
            )
            worker_git = ConfinedWorkerGit(context.runtime_journal, container)
        return NativeAuthorities(
            policy,
            accounting,
            accounting,
            "assignment",
            permission.decision_preparation,
            confined_commands=runner,
            command_refs=tuple(runner.references) if runner is not None else (),
            worker_git=worker_git,
        )

    registration = NativeRegistration(
        d["native_ref"], d["host"], manifest.profile, d["account_hash"], pins, prepare
    )
    if registration.fingerprint != installation["native_digest"]:
        raise AccessDenied("Cold native registration differs from installed reference digest")
    reviewer_factory = None
    if installation.get("reviewer_installation") is not None:
        if role != "worker":
            raise AccessDenied("Independent reviewer cannot delegate another review")
        from .reviewer import NormalReviewerFactory

        reviewer_factory = NormalReviewerFactory(manifest, service_options=service_options)
    return NativeRegistry(
        (registration,),
        service_options=service_options,
        reviewer_factory=reviewer_factory,
        code_registrations=code_registrations,
    )


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
