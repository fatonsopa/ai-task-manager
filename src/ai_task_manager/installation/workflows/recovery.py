"""Explicit operator recovery uses original signed activation and current authority."""

import os
import sqlite3
import subprocess
from pathlib import Path

from ai_task_manager.adapters.workflow_control.client import WorkflowClient
from ai_task_manager.adapters.workflow_control.identity import (
    file_binding,
    settings,
    source_digest,
)
from ai_task_manager.adapters.workflow_control.recovery import physical
from ai_task_manager.application.service.models import Unavailable
from ai_task_manager.config.files import read_private
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired
from ai_task_manager.domains.work.models import Scope
from ai_task_manager.installation.sessions.recovery import _verified_history
from ai_task_manager.storage.workflows import WorkflowStore

from .bootstrap import activation_reference
from .spec import Installation

ACTIONS = frozenset({"inspect", "resume", "stop", "successor", "retire"})


def request(value):
    if (
        type(value) is not dict
        or set(value) != {"action", "workflow_ref", "operation_key"}
        or type(value["action"]) is not str
        or value["action"] not in ACTIONS
        or any(type(value[k]) is not str or not 1 <= len(value[k]) <= 256
               or not value[k].strip() for k in ("workflow_ref", "operation_key"))
    ):
        raise AccessDenied("Explicit recovery action and original workflow identity required")
    return value


def owner_state(descriptor, activation):
    if (descriptor.get("pid"), descriptor.get("start")) != (
        activation["owner"]["pid"], activation["owner"]["start"]
    ):
        raise AccessDenied("Original owner process identity changed")
    observed = subprocess.run(
        ["ps", "-p", str(descriptor["pid"]), "-o", "uid=,lstart="],
        capture_output=True, text=True, timeout=2,
    )
    if observed.returncode == 1 and not observed.stdout.strip() and not observed.stderr.strip():
        return "dead"
    if observed.returncode == 0 and not observed.stderr.strip():
        fields = observed.stdout.strip().split(maxsplit=1)
        if len(fields) == 2:
            return "live" if fields == [str(os.getuid()), descriptor["start"]] else "reused"
    return "unknown"


def recover(config_file, installation_file, value):
    """Trusted operator API; public JSON supplies selection, never authority."""
    value = request(value)
    spec = Installation(installation_file, config_file)
    config = read_private(config_file)
    control = settings(config.get("workflow_control"))
    # Constructors must never recreate a missing original store or key.
    for name in ("core_database", "authority_database", "authority_key", "session_file"):
        physical(config[name])
    if config.get("linear"):
        physical(config["linear"]["journal"])
    reference = activation_reference(config_file, control)
    # Read-only URI mode also refuses a disappearance between stat and connect.
    conn = sqlite3.connect(
        Path(config["authority_database"]).as_uri() + "?mode=ro", uri=True
    )
    try:
        versions = conn.execute("SELECT version FROM authority_version").fetchall()
        if versions != [(2,)]:
            raise EvidenceRequired("Current original authority source required")
        saved = conn.execute("SELECT data,mac FROM records WHERE ref=?", (reference,)).fetchone()
    finally:
        conn.close()
    key = Path(config["authority_key"]).read_bytes()
    if len(key) != 32:
        raise AccessDenied("Authority key is invalid")
    activated = _verified_history(saved, key, reference)
    if activated is None or "recovery" not in activated:
        raise EvidenceRequired("Original installation recovery witness is unavailable")
    if any(activated.get(k) != v for k, v in {
        "config": file_binding(config_file), "scope": spec.data["scope"],
        "instance": control["instance"], "source": source_digest(),
        "deadline": spec.data["deadline"],
    }.items()):
        raise AccessDenied("Original activation differs from installed recovery")
    witness = activated["recovery"]

    def check():
        spec.check()
        if witness["installation"] != file_binding(installation_file):
            raise AccessDenied("Original recovery installation changed")
        binding = witness["workflow"]
        try:
            original = physical(binding["path"])
        except (OSError, AccessDenied) as error:
            raise EvidenceRequired("Original recovery workflow journal is unavailable") from error
        if original != {k: v for k, v in binding.items() if k != "store_id"}:
            raise AccessDenied("Original recovery workflow journal changed")
        if Path(binding["path"]) != Path(spec.data["state_root"]) / "workflow.sqlite":
            raise AccessDenied("Original workflow journal differs from installation")

    check()
    descriptor = read_private(control["record"])
    if any(descriptor.get(k) != activated[k] for k in ("config", "scope", "instance", "source")):
        raise AccessDenied("Original owner descriptor differs from activation")
    state = owner_state(descriptor, witness)
    if state != "live":
        # Cold observation/settlement needs its own current authority port (T02).
        # Do not construct ordinary services that might initialize missing history.
        raise EvidenceRequired("Current owner is not verified live; retained recovery is required")
    client = WorkflowClient(config_file, spec.data["scope"])
    scope = Scope(**spec.data["scope"])
    binding = witness["workflow"]
    store = WorkflowStore.open_existing(binding["path"], store_id=binding["store_id"])
    row = store.read(value["workflow_ref"], scope)
    original_key = store.original_key(value["workflow_ref"], scope)
    if (row["task_id"], row["plan_id"], original_key) != (
        spec.data["task_id"], spec.data["plan_ref"], value["operation_key"]
    ):
        raise AccessDenied("Recovery requires the original task, plan and operation key")
    # Current scope/enrollment is checked by the original live owner, including
    # its actual installed observation source. JSON never supplies this proof.
    envelope = client.forward("workflow_inspect", {"workflow_ref": value["workflow_ref"]})
    if not envelope["ok"]:
        return envelope
    check()
    if value["action"] == "inspect":
        envelope["data"]["owner_state"] = state
        return envelope
    if value["action"] != "resume":
        raise Unavailable("The selected recovery action is not installed")
    # The ordinary guarded owner rechecks plan, decisions and effect identities.
    envelope = client.forward("workflow_resume", {"workflow_ref": value["workflow_ref"]})
    check()
    return envelope
