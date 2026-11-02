"""Values returned by explicitly configured trusted ports, never proof issuers."""

import json
import re
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, fields
from enum import StrEnum
from typing import Protocol

from ai_task_manager.config.files import digest, strict_json
from ai_task_manager.domains.work.errors import AccessDenied, ValidationError


def token(value):
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value):
        raise ValidationError("An opaque bounded identifier is required")
    return value


def sha(value, length=64):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{" + str(length) + "}", value):
        raise ValidationError("Invalid content digest")
    return value


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class Binding:
    task_id: str
    attempt_id: str
    principal_id: str
    session_id: str
    team_id: str
    project_id: str
    issue_id: str
    prose_digest: str
    source_digest: str
    worktree_id: str
    common_git_id: str
    resource_id: str
    command_id: str
    operation_key: str

    def validate(self):
        for field in fields(self):
            value = getattr(self, field.name)
            sha(value) if field.name.endswith("_digest") else token(value)
        return self

    @property
    def fingerprint(self):
        self.validate()
        return digest(asdict(self))


class Answer(StrEnum):
    CHECKS_PASSED = "CHECKS PASSED"
    WOULD_NOT_MERGE = "WOULD NOT MERGE"
    MERGED = "MERGED"
    NOT_MERGED = "NOT MERGED"
    NOTHING_TO_MERGE = "NOTHING TO MERGE"


@dataclass(frozen=True)
class Observation:
    operation_ref: str
    binding_digest: str
    evidence_ref: str
    kind: str
    payload_json: str

    def payload(self, operation, kind):
        if (
            self.operation_ref != operation["ref"]
            or self.binding_digest != operation["fingerprint"]
            or self.kind != kind
        ):
            raise AccessDenied("Observation does not match this operation")
        token(self.evidence_ref)
        value = strict_json(self.payload_json)
        if type(value) is not dict:
            raise ValidationError("Observation must contain an object")
        return value


@dataclass(frozen=True)
class Callback:
    operation_ref: str
    binding_digest: str
    source_digest: str
    issue_id: str
    team_id: str
    principal_id: str
    sequence: int
    kind: str
    range_ref: str
    head: str
    content_digest: str
    approval_ref: str = "absent"
    result_ref: str = "absent"
    part_id: str = "whole"

    def validate(self, operation):
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValidationError("Callback sequence must be a positive integer")
        if self.kind not in {
            "landed",
            "after_merge",
            "branch_note",
            "local_db",
            "pipeline",
            "release_items",
            "rolled_back",
            "followup",
        }:
            raise ValidationError("Unknown callback kind")
        for field in fields(self):
            if field.name == "sequence":
                continue
            value = getattr(self, field.name)
            if field.name.endswith("_digest"):
                sha(value)
            elif field.name == "head":
                sha(value, 40)
            else:
                token(value)
        binding = operation["binding"]
        expected = {
            "operation_ref": operation["ref"],
            "binding_digest": operation["fingerprint"],
            "source_digest": binding["source_digest"],
            "issue_id": binding["issue_id"],
            "team_id": binding["team_id"],
            "principal_id": binding["principal_id"],
        }
        if any(getattr(self, key) != value for key, value in expected.items()):
            raise AccessDenied("Callback binding mismatch")
        publication = operation["facts"].get("publication")
        if (
            not publication
            or self.head != publication["new"]
            or self.range_ref != publication["range_ref"]
        ):
            raise AccessDenied("Callback requires matching observed publication")
        return self


@dataclass(frozen=True)
class Receipt:
    operation_ref: str
    binding_digest: str
    sequence: int
    stage: str
    evidence_ref: str


class Authority(Protocol):
    def registration(self, binding: Binding) -> None: ...
    def before_start(self, binding: Binding) -> None: ...
    def check_current(self, binding: Binding) -> None: ...
    def authority_boundary(self, binding: Binding) -> AbstractContextManager: ...


class Observations(Protocol):
    def read(self, evidence_ref: str, binding: Binding) -> Observation: ...
    def process(self, binding: Binding) -> Observation: ...
    def receipt(self, evidence_ref: str, binding: Binding) -> Receipt: ...


class Downstream(Protocol):
    """Return a verifiable receipt ref; every thrown exception is uncertain.

    A no-effect denial is represented only by a trusted negative receipt.
    """

    def accept(self, callback: Callback, operation_key: str) -> str: ...


class StopPort(Protocol):
    def verify(self, binding: Binding, decision_ref: str) -> None: ...
    def request(self, binding: Binding, decision_ref: str) -> None: ...
