"""Enroll a trusted operator's finite reporting phase without renewing execution."""

import math
import time
from dataclasses import asdict

from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.decisions.observations import PREFIX, bind_record, identity, intent
from ai_task_manager.adapters.workflow_control.identity import source_digest
from ai_task_manager.application.service.linear import LinearComponents
from ai_task_manager.config.files import FileSeal, digest, private_path, read_private, write_private
from ai_task_manager.config.service import load_service
from ai_task_manager.domains.work.errors import AccessDenied

from .recovery import history, installed_authority, installed_provider_history, setup_lock


def setup(config_file, predecessor_config, *, event_id, deadline, operation_key, components=None):
    """Trusted installer port. The original capability and enrollments stay unchanged."""
    config_file, predecessor_config = private_path(config_file), private_path(predecessor_config)
    if config_file == predecessor_config or not isinstance(operation_key, str) or not operation_key:
        raise AccessDenied("Reporting requires a distinct explicit configuration and operation key")
    if type(deadline) not in {int, float} or not math.isfinite(deadline) or deadline <= time.time():
        raise AccessDenied("Reporting requires a fresh finite deadline")
    with setup_lock(config_file):
        seal, original_seal = FileSeal(config_file), FileSeal(predecessor_config)
        config, original = read_private(config_file), read_private(predecessor_config)
        installed_authority(original)
        installed_provider_history(original)
        journal = AuthorityJournal.open_existing(
            original["authority_database"], original["authority_key"]
        )
        enrollment = history(journal, original["grant_ref"])
        if (
            enrollment is None
            or enrollment["config_path"] != str(predecessor_config)
            or enrollment["config_intent"] != intent(original)
        ):
            raise AccessDenied("Original enrollment configuration history changed")
        prior = history(journal, "session-setup-result:" + digest(original["grant_ref"]))
        if prior is None or prior["session_file"] != identity(
            original["session_file"], content=True
        ):
            raise AccessDenied("Original completed capability history is required")
        base = {
            k: v
            for k, v in original.items()
            if k not in {"workflow_control", "grant_ref", "session_observations"}
        }
        reference = (
            PREFIX + "reporting:" + digest({"config": str(config_file), "key": operation_key})
        )
        desired = {**base, "reporting_ref": reference}
        if config not in (base, desired) and config != {
            **desired,
            "grant_ref": reference,
            "session_observations": {
                "reference": reference,
                "journal": identity(journal.path),
                "key": identity(journal.key_path, content=True),
            },
        }:
            raise AccessDenied(
                "Reporting configuration must preserve the original scope and stores"
            )
        components = components or LinearComponents(original["linear"], existing_only=True)
        if components.configuration_hash != digest(original["linear"]):
            raise AccessDenied("Reporting provider composition changed")
        binding = components.client.verify_binding()
        service = load_service(predecessor_config, linear_components=components, existing_only=True)
        components.client.check_connection(service.scope, refresh=True)
        event = service.core.notifications.get_event(service.handle, event_id)
        task = service.core.get_task(service.handle, event.task_id)
        if task.sync_state != "confirmed" or event.payload.get("task_version") != task.version:
            raise AccessDenied("Reporting requires the exact current confirmed task event")
        sources = [
            identity(predecessor_config, content=True),
            identity(original["session_file"], content=True),
            identity(original["core_database"]),
            identity(original["linear"]["journal"]),
        ]
        if original.get("writer"):
            sources += [
                identity(original["writer"]["journal"]),
                identity(original["writer"]["key"], content=True),
            ]
        payload = {
            "kind": "event_reporting",
            "scope": asdict(service.scope),
            "provider": asdict(binding.expected),
            "config_path": str(config_file),
            "config_intent": intent(desired),
            "actions": [],
            "expires_at": deadline,
            "source_digest": source_digest(),
            "sources": sources,
            "predecessor_enrollment": original["grant_ref"],
            "event_id": event.id,
            "event_digest": digest(asdict(event)),
            "task_id": task.id,
            "task_version": task.version,
            "decision_revision": getattr(task, "decision_revision", 0),
        }
        if deadline <= time.time():
            raise AccessDenied("Reporting authority expired during current provider verification")
        seal.check()
        original_seal.check()
        bind_record(journal, reference, payload)
        bind_record(
            journal,
            "session-setup-result:" + digest(reference),
            {
                "scope": asdict(service.scope),
                "session_file": identity(original["session_file"], content=True),
            },
        )
        pointer = {
            "reference": reference,
            "journal": identity(journal.path),
            "key": identity(journal.key_path, content=True),
        }
        write_private(
            config_file,
            {**desired, "grant_ref": reference, "session_observations": pointer},
            replace=True,
        )
        return {
            "enrollment_ref": reference,
            "event_id": event.id,
            "task_id": task.id,
            "expires_at": deadline,
        }
