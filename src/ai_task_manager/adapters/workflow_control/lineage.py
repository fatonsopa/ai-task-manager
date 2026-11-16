"""Resolve only signed owner replacement links in the original authority history."""

import sqlite3
from pathlib import Path

from ai_task_manager.config.files import digest, read_private
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.sessions.recovery import _verified_history


def replacement_reference(config_file, control):
    return "workflow-owner-replacement:" + digest(
        {"config": str(Path(config_file).absolute()), "instance": control["instance"]}
    )


def verified_controls(config_file):
    from .identity import file_binding, settings, source_digest

    config = read_private(config_file)
    control = settings(config.get("workflow_control"))
    pointers = {"authority_key", "authority_database"}
    if not pointers.intersection(config):
        # Older direct owner adapters have no replacement authority at all.
        # Their existing descriptor is still checked by OwnerBinding.
        return [control]
    if not pointers <= config.keys():
        raise AccessDenied("Complete original replacement authority is required")
    key = Path(config["authority_key"]).read_bytes()
    controls = [control]
    with sqlite3.connect(
        Path(config["authority_database"]).as_uri() + "?mode=ro", uri=True
    ) as conn:
        for _ in range(16):
            reference = replacement_reference(config_file, control)
            saved = conn.execute(
                "SELECT data,mac FROM records WHERE ref=?", (reference,)
            ).fetchone()
            link = _verified_history(saved, key, reference)
            if link is None:
                return controls
            if (
                link["config"] != file_binding(config_file)
                or link["source"] != source_digest()
                or link["previous"] != control
                or link["descriptor"] != file_binding(control["record"])
                or link["successor_descriptor"] != file_binding(link["successor"]["record"])
            ):
                raise AccessDenied("Original owner replacement lineage changed")
            authority = conn.execute(
                "SELECT data,mac FROM records WHERE ref=?", (link["authority"],)
            ).fetchone()
            admitted = _verified_history(authority, key, link["authority"])
            if (
                admitted is None
                or admitted["previous"] != control
                or admitted["successor"] != link["successor"]
                or admitted["source"] != link["source"]
                or admitted["config"] != link["config"]
            ):
                raise AccessDenied("Original replacement authority changed")
            successor = settings(link["successor"])
            if successor["instance"] == control["instance"]:
                raise AccessDenied("Owner replacement lineage cannot repeat an instance")
            control = successor
            controls.append(control)
    raise AccessDenied("Owner replacement lineage exceeds its finite bound")


def resolve_control(config_file):
    return verified_controls(config_file)[-1]
