"""Fresh delegation for a task successor preserves the old capability file."""

import math
import secrets
import time
from dataclasses import asdict
from pathlib import Path

from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.decisions.observations import (
    PREFIX, SessionObservations, bind_record, identity, intent,
)
from ai_task_manager.application.service.linear import LinearComponents
from ai_task_manager.config.files import digest, private_path, read_private, write_private
from ai_task_manager.config.sessions import SessionFile
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import Scope, SessionHandle
from .recovery import setup_lock
from .setup import ACTIONS


def setup(config_file, predecessor_config, *, actions, deadline, operation_key, components=None):
    """Trusted operator setup stages delegation; Core separately admits the task.

    The new configuration and capability destination must already be private.
    This does not register, resume, rotate or un-revoke the predecessor session.
    The staged token has no Core authority before successor admission commits.
    """
    config_file, predecessor_config = private_path(config_file), private_path(predecessor_config)
    with setup_lock(config_file):
        config, previous = read_private(config_file), read_private(predecessor_config)
        if (type(actions) is not tuple or not actions or len(set(actions)) != len(actions)
                or not set(actions) <= ACTIONS | {"successor"} or "successor" not in actions
                or type(deadline) not in {int, float} or not math.isfinite(deadline)
                or deadline <= time.time() or not isinstance(operation_key, str) or not operation_key):
            raise AccessDenied("Fresh finite successor delegation is required")
        for field in ("core_database", "authority_database", "authority_key", "linear", "approval_policy"):
            if config.get(field) != previous.get(field):
                raise AccessDenied("Successor delegation cannot change retained scope or history")
        if config["session_file"] == previous["session_file"] or config_file == predecessor_config:
            raise AccessDenied("Successor requires separate configuration and capability files")
        old = read_private(previous["session_file"])
        scope = Scope(**old["scope"])
        components = components or LinearComponents(config["linear"], existing_only=True)
        if components.configuration_hash != digest(config["linear"]):
            raise AccessDenied("Current provider differs from successor configuration")
        binding = components.client.verify_binding()
        expected = binding.expected
        if (scope.provider, scope.workspace, scope.principal, scope.project) != (
                "linear", expected.workspace_id, expected.viewer_id, expected.project_id):
            raise AccessDenied("Current provider differs from predecessor scope")
        components.client.check_connection(scope, refresh=True)
        journal = AuthorityJournal.open_existing(config["authority_database"], config["authority_key"])
        ref = PREFIX + digest({"successor_config": str(config_file), "key": operation_key})
        destination = private_path(config["session_file"], existing=False)
        if destination.exists():
            staged = read_private(destination)
            if staged.get("scope") != asdict(scope):
                raise AccessDenied("Staged successor capability has another scope")
        else:
            SessionFile(destination).save(SessionHandle(secrets.token_urlsafe(32)), scope)
        bind_record(journal, ref, {
            "scope": asdict(scope), "provider": asdict(expected),
            "config_path": str(config_file), "config_intent": intent(config),
            "actions": list(actions), "expires_at": deadline,
        })
        bind_record(journal, "session-setup-result:" + digest(ref), {
            "scope": asdict(scope), "session_file": identity(destination, content=True),
        })
        pointer = {"reference": ref, "journal": identity(journal.path),
                   "key": identity(journal.key_path, content=True)}
        updated = {**config, "grant_ref": ref, "session_observations": pointer}
        write_private(config_file, updated, replace=True)
        SessionObservations(journal, components.client, config_file, pointer).verify_enrollment(ref)
        return {"enrollment_ref": ref, "scope": asdict(scope), "capability_saved": True}
