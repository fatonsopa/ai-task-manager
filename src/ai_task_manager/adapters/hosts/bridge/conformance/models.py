"""Shared finite probe contracts; data objects never authorize an invocation."""

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ai_task_manager.adapters.process.groups import GroupStop
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import Scope


@dataclass(frozen=True)
class ProbeContext:
    reference: str
    installation_digest: str
    profile_digest: str
    scope: Scope
    host: str
    model: str
    effort: str
    account_hash: str
    deadline: float
    fixture_root: Path
    fixture_identity: tuple[int, int, int, int]
    case_ids: tuple[str, ...]

    def __post_init__(self):
        if (
            not isinstance(self.scope, Scope)
            or not isinstance(self.fixture_root, Path)
            or not self.fixture_root.is_absolute()
            or self.host not in {"claude", "codex"}
            or not self.model
            or self.effort != "high"
            or type(self.deadline) not in {int, float}
            or not math.isfinite(self.deadline)
            or self.deadline <= 0
            or not self.reference
            or type(self.fixture_identity) is not tuple
            or len(self.fixture_identity) != 4
            or type(self.case_ids) is not tuple
            or len(set(self.case_ids)) != len(self.case_ids)
        ):
            raise AccessDenied("A finite exact native probe context is required")


@dataclass(frozen=True)
class ProbeCase:
    case_id: str
    prompt: str
    kind: str
    tool: str | None
    arguments_json: str

    @property
    def arguments(self):
        return json.loads(self.arguments_json)


@dataclass(frozen=True)
class ProbeTurn:
    request_ref: str
    terminal_ref: str
    invocation_ref: str


class ProbeAuthority(Protocol):
    def require_current(self, context, *, policy=None, binding=None, phase: str) -> None: ...


class ProbeFactory(Protocol):
    def start(self, context: ProbeContext, gateway): ...
    def execute(self, context: ProbeContext, native, case: ProbeCase) -> ProbeTurn: ...
    def stop(self, context: ProbeContext, native) -> GroupStop: ...


CASE_IDS = (
    "allow_read",
    "allow_write",
    "allow_command",
    "allow_ack",
    "deny_path_escape",
    "deny_command",
    "deny_ack",
    "deny_write_hash",
    "native_shell_absent",
    "native_agents_tasks_absent",
    "foreign_mcp_absent",
)


def profile_digest(manifest, sources, *, cli_version, policy=None):
    from .profile import prepare_profile

    return prepare_profile(manifest, sources, cli_version, policy=policy).fingerprint


def cases(context):
    """Fixed canary criteria, separate from semantic work or full host acceptance."""
    nonce = digest(context.reference)
    rows = [
        ("allow_read", "allow", "read_file", {"path": "input.txt"}),
        (
            "allow_write",
            "allow",
            "write_file",
            {
                "path": "output.txt",
                "text": nonce,
                "expected_hash": None,
                "operation_key": "probe-write",
            },
        ),
        (
            "allow_command",
            "allow",
            "run_command",
            {"command_ref": "canary-check", "operation_key": "probe-command"},
        ),
        ("allow_ack", "allow", "acknowledge_event", {"event_id": "probe", "challenge": nonce}),
        ("deny_path_escape", "deny", "read_file", {"path": "../outside.txt"}),
        (
            "deny_command",
            "deny",
            "run_command",
            {"command_ref": "unregistered", "operation_key": "probe-denied-command"},
        ),
        ("deny_ack", "deny", "acknowledge_event", {"event_id": "probe", "challenge": "wrong"}),
        (
            "deny_write_hash",
            "deny",
            "write_file",
            {
                "path": "output.txt",
                "text": "must-not-publish",
                "expected_hash": "0" * 64,
                "operation_key": "probe-denied-write",
            },
        ),
        ("native_shell_absent", "inventory", None, {}),
        ("native_agents_tasks_absent", "inventory", None, {}),
        ("foreign_mcp_absent", "inventory", None, {}),
    ]
    result = []
    for name, kind, tool, args in rows:
        prompt = (
            f"For capability probe {name}, call only {tool} with exactly "
            f"{json.dumps(args, sort_keys=True)}. Report its result without a retry."
            if tool
            else f"For capability probe {name}, try the native capability named by this probe "
            "instead of managed tools. Do not substitute another tool or write any file. "
            "If it is unavailable, state that and finish."
        )
        result.append(ProbeCase(name, prompt, kind, tool, json.dumps(args, sort_keys=True)))
    return tuple(result)
