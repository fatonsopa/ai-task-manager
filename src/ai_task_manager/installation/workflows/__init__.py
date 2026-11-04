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
):
    if registry is None:
        from ai_task_manager.installation.native import resolve

        registry = resolve(config_file, installation_file, service_options=service_options)
    installed = InstalledWorkflow(
        config_file, installation_file, registry, service_options=service_options, reviews=reviews
    )
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
