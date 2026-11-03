"""Pure publication values and trusted-port contracts, independent of project policy."""

import json
import re
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, fields
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


def oid(value):
    return sha(value, 40)


def refname(value):
    if (
        type(value) is not str
        or not value.startswith("refs/heads/")
        or len(value) > 200
        or any(c in value for c in " ~^:?*[\\\x00\n\r")
        or ".." in value
        or "@{" in value
        or "//" in value
        or value.endswith(("/", "."))
        or any(p.startswith(".") or p.endswith(".lock") for p in value.split("/"))
    ):
        raise ValidationError("An explicit supported local branch reference is required")
    return value


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class Binding:
    task_id: str
    attempt_id: str
    principal_id: str
    session_id: str
    project_id: str
    prose_digest: str
    source_digest: str
    worktree_id: str
    repository_id: str
    resource_id: str
    configuration_digest: str
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


@dataclass(frozen=True)
class Publication:
    """The target and executable profile are deliberately absent from request data."""

    source_ref: str
    old: str
    new: str
    checked: str
    range_ref: str
    mode: str = "cas"

    def validate(self):
        refname(self.source_ref)
        token(self.range_ref)
        for value in (self.old, self.new, self.checked):
            oid(value)
        if self.mode not in {"cas", "fast_forward"}:
            raise AccessDenied("Unsupported publication mode")
        if self.old == self.new:
            raise AccessDenied("Unchanged refs require separate content reconciliation")
        if self.new != self.checked:
            raise AccessDenied("Publication differs from the checked commit")
        return self


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


class Authority(Protocol):
    """Trusted composition supplies task permission; Git never infers it from prose."""

    def registration(self, binding: Binding) -> None: ...
    def before_start(self, binding: Binding) -> None: ...
    def check_current(self, binding: Binding) -> None: ...
    def authority_boundary(self, binding: Binding) -> AbstractContextManager: ...


class Checks(Protocol):
    """The checks owner validates freshness, policy and complete input provenance."""

    def read(self, evidence_ref: str, binding: Binding) -> Observation: ...
