"""Translate pinned project inputs into the canonical checks catalog."""

import hashlib
import re
from dataclasses import dataclass

from ai_task_manager.config.files import strict_json
from ai_task_manager.domains.checks.catalog import validate_catalog
from ai_task_manager.domains.checks.models import Catalog, content_hash, fingerprint
from ai_task_manager.domains.work.errors import AccessDenied, ValidationError


@dataclass(frozen=True)
class ProjectInputs:
    catalog: Catalog
    source_pin: str
    source_digest: str
    package_manager: str
    static_members: tuple[str, ...]


def load_project_inputs(*, catalog, static_inputs, package, lockfile, source_pin, expected):
    """Expected digests are supplied by the configured trusted source binding.

    This reads no host credentials/configuration and installs no dependencies.
    Private source bytes remain with the caller, outside published artifacts.
    """
    if type(source_pin) is not str or not re.fullmatch(r"[0-9a-f]{40}", source_pin):
        raise ValidationError("An immutable project source pin is required")
    raw = {
        "catalog": catalog,
        "static_inputs": static_inputs,
        "package": package,
        "lockfile": lockfile,
    }
    if set(expected) != set(raw):
        raise ValidationError("All selected project input digests are required")
    actual = {}
    for name, data in raw.items():
        if type(data) is not bytes or len(data) > 1500000:
            raise ValidationError("Project input is unavailable or exceeds its byte limit")
        content_hash(expected[name])
        actual[name] = hashlib.sha256(data).hexdigest()
        if actual[name] != expected[name]:
            raise AccessDenied("Pinned project input changed")
    parsed_catalog = validate_catalog(strict_json(catalog), actual["catalog"])
    config, tools = strict_json(static_inputs), strict_json(package)
    if (
        type(config) is not dict
        or type(config.get("members")) is not dict
        or not config["members"]
        or type(config.get("all_members_when")) is not list
    ):
        raise ValidationError("Static check input declaration is malformed")
    for name, row in config["members"].items():
        if (
            type(name) is not str
            or type(row) is not dict
            or type(row.get("inputs")) is not list
            or not row["inputs"]
            or any(type(x) is not str or not x for x in row["inputs"])
        ):
            raise ValidationError("Static member requires explicit input patterns")
    manager = tools.get("packageManager") if type(tools) is dict else None
    if type(manager) is not str or not manager.strip():
        raise ValidationError("Project package-manager identity is required")
    return ProjectInputs(
        parsed_catalog,
        source_pin,
        fingerprint({"pin": source_pin, "inputs": actual}),
        manager,
        tuple(config["members"]),
    )
