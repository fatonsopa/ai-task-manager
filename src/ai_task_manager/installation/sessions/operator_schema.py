"""Closed installation request; references select credentials, never secret values."""

import math
import re
from pathlib import Path

from ai_task_manager.adapters.linear.connections import AgentConnection
from ai_task_manager.adapters.linear.models import canonical_id
from ai_task_manager.config.capacity import policies
from ai_task_manager.config.environment import CREDENTIAL_ENVIRONMENT_NAME_PATTERN
from ai_task_manager.domains.work.errors import ValidationError


def exact(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ValidationError("Unknown or incomplete operator manifest fields")


def absolute(value):
    if type(value) is not str or not value or not Path(value).is_absolute():
        raise ValidationError("Operator paths must be explicit and absolute")
    if str(Path(value)) != value or ".." in Path(value).parts:
        raise ValidationError("Operator paths must be normalized")
    return Path(value)


def sha256(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValidationError("Explicit SHA-256 pins are required")


def parse(request):
    exact(
        request,
        {
            "version",
            "state_directory",
            "linear",
            "logical_session",
            "writer",
            "capacity_policies",
            "actions",
            "deadline",
            "operation_key",
        },
    )
    if type(request["version"]) is not int or request["version"] != 1:
        raise ValidationError("Unsupported operator manifest version")
    absolute(request["state_directory"])
    for key in ("logical_session", "operation_key"):
        if (
            type(request[key]) is not str
            or not request[key].strip()
            or len(request[key]) > 256
            or any(ord(c) < 32 for c in request[key])
        ):
            raise ValidationError("Bounded explicit session and operation key required")
    if request["actions"] != ["enqueue"]:
        raise ValidationError("Initial installation delegates only enqueue")
    if (
        type(request["deadline"]) not in {int, float}
        or not math.isfinite(request["deadline"])
        or request["deadline"] <= 0
    ):
        raise ValidationError("An absolute finite delegation deadline is required")
    selected_capacity = policies(request)
    if not selected_capacity:
        raise ValidationError("Declare finite capacity before installation")
    if any(
        max(policy.host_limit, policy.project_limit) > 2**31 - 1 for policy in selected_capacity
    ):
        raise ValidationError("Capacity exceeds the supported finite integer bound")
    linear = request["linear"]
    exact(linear, {"expected", "agent_connection", "credential"})
    exact(linear["expected"], {"viewer_id", "workspace_id", "team_id", "team_key", "project_id"})
    for key, value in linear["expected"].items():
        if key != "team_key":
            canonical_id(value)
    if linear["expected"]["team_key"] != "TST":
        raise ValidationError("This installation port requires the explicit TST team")
    selected = linear["agent_connection"]
    exact(
        selected, {"connection_id", "workspace_id", "principal_id", "logical_session", "agent_id"}
    )
    AgentConnection(**selected)
    if (
        selected["workspace_id"] != linear["expected"]["workspace_id"]
        or selected["principal_id"] != linear["expected"]["viewer_id"]
        or selected["logical_session"] != request["logical_session"]
    ):
        raise ValidationError("Selected connection must match the expected session and identity")
    credential = linear["credential"]
    if type(credential) is not dict:
        raise ValidationError("One supported credential reference is required")
    if set(credential) == {"environment"}:
        if (
            type(credential["environment"]) is not str
            or re.fullmatch(CREDENTIAL_ENVIRONMENT_NAME_PATTERN, credential["environment"]) is None
        ):
            raise ValidationError("Credential environment must use AITM_CREDENTIAL_ namespace")
    elif set(credential) == {"file", "key"}:
        absolute(credential["file"])
        if credential["key"] not in {
            "LINEAR_API_KEY",
            "LINEAR_TEST_USER_A_TOKEN",
            "LINEAR_TEST_USER_B_TOKEN",
        }:
            raise ValidationError("Unknown credential file field")
    else:
        raise ValidationError("One supported credential reference is required")
    writer = request["writer"]
    exact(writer, {"root", "python", "python_sha256", "pins"})
    absolute(writer["root"])
    absolute(writer["python"])
    sha256(writer["python_sha256"])
    pins = writer["pins"]
    if type(pins) is not dict or not 1 <= len(pins) <= 4096:
        raise ValidationError("A bounded writer package inventory is required")
    for name, value in pins.items():
        if (
            type(name) is not str
            or not name
            or len(name) > 512
            or Path(name).is_absolute()
            or ".." in Path(name).parts
        ):
            raise ValidationError("Writer pins must name relative package files")
        sha256(value)
    return request
