"""Explicit installed owner launch; no dynamic registration discovery."""

from ai_task_manager.application.service.models import result

from .composition import InstalledWorkflow
from .registry import NativeAuthorities, NativeRegistration, NativeRegistry

__all__ = [
    "InstalledWorkflow",
    "NativeAuthorities",
    "NativeRegistration",
    "NativeRegistry",
    "launch",
]


def launch(
    config_file,
    installation_file,
    *,
    registry=None,
    service_options=None,
    stop=None,
    ready=None,
    reviews=None,
    recovery=None,
):
    supplied_registry = registry
    if registry is None:
        from ai_task_manager.installation.native import resolve

        registry = resolve(config_file, installation_file, service_options=service_options)
    selected = str(recovery.spec.seal.path) if recovery else None
    installed = InstalledWorkflow(
        config_file,
        installation_file,
        registry,
        service_options=service_options,
        reviews=reviews,
        recovery=recovery if selected == str(installation_file) else None,
        retained=recovery if selected != str(installation_file) else None,
    )
    members = []
    for member in installed.spec.members(retained=recovery is not None):
        member_registry = supplied_registry
        if member_registry is None:
            from ai_task_manager.installation.native import resolve
            member_registry = resolve(config_file, member.seal.path, service_options=service_options)
        members.append(InstalledWorkflow(
            config_file, member.seal.path, member_registry, service_options=service_options,
            recovery=recovery if selected == str(member.seal.path) else None,
            retained=recovery if selected != str(member.seal.path) else None,
        ))
    if members:
        installed.service.workflows.bind_members(
            tuple(member.service.workflows for member in members), installed.spec.check,
        )
        installed.service.workflows.check_owner_deadline(installed.spec.data["deadline"])
        installed.owner.member_installations = tuple(members)
    if recovery is not None and installed.owner.recovery is None:
        recovery.authorize(installed.service)
        installed.owner.recovery = recovery
        installed.owner.settings = recovery.successor
    installed.serve(stop, ready)
    shutdown = installed.owner.shutdown_result
    uncertain = any(
        row.get("state") in {"unknown", "uncertain"} or row.get("quiescent") is False
        for row in shutdown
    )
    envelope = result(
        {"state": "owner_closed", "shutdown": shutdown}, "uncertain" if uncertain else "confirmed"
    )
    if uncertain:
        envelope["exit_code"] = 6
    return envelope
