"""Immutable bindings shared by the managed controller and its observations."""

from dataclasses import dataclass
from typing import Literal

from ai_task_manager.domains.work.models import Scope


@dataclass(frozen=True)
class HostBinding:
    scope: Scope
    core_session: str
    attempt_id: str
    worker_id: str
    host: Literal["claude", "codex"]
    principal_ref: str
    configuration_digest: str
    deadline: float


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_identity: str
    group_id: int


@dataclass(frozen=True)
class AcceptedInput:
    event_id: str
    challenge: str
    input_id: str
    text: str


@dataclass(frozen=True)
class PublicOutput:
    event_id: str
    native_session: str
    turn_id: str
    item_id: str
    text: str
    trace_ref: str


class ManagedProcesses:
    """Trusted installer/controller ownership, not a model-selectable process map."""

    def __init__(self):
        self._owned = {}

    def attach(self, attempt_id, process):
        from ai_task_manager.domains.work.errors import AccessDenied

        old = self._owned.get(attempt_id)
        if old is not None and old is not process:
            raise AccessDenied("Existing native process ownership requires reconciliation")
        self._owned[attempt_id] = process

    def get(self, attempt_id):
        return self._owned.get(attempt_id)
