"""Immutable check inputs. Values crossing this boundary contain no credentials."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum

from ai_task_manager.domains.work.errors import SchemaError, ValidationError


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identifier(value):
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > 1024
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ValidationError("A bounded identifier is required")
    return value


def content_hash(value):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValidationError("A SHA256 content digest is required")
    return value


def repo_path(value):
    if (
        type(value) is not str
        or not value
        or len(value) > 1024
        or "\\" in value
        or value.startswith("/")
        or any(x in {"", ".", ".."} for x in value.split("/"))
    ):
        raise ValidationError("A repository-relative path is required")
    return value


class Availability(StrEnum):
    KNOWN = "known"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"


class Outcome(StrEnum):
    PASSED = "passed"
    ASSERTION = "assertion_failed"
    SOURCE = "source_error"
    DEV_RED = "dev_red"
    SETUP = "setup_failed"
    CAPACITY = "capacity"
    TIMEOUT = "timeout"
    SIGNAL = "signal"
    MISSING = "missing_summary"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Scope:
    principal: str
    project: str
    session: str
    task: str

    def validate(self):
        for value in asdict(self).values():
            identifier(value)


@dataclass(frozen=True)
class Fact:
    kind: str
    name: str
    value: str | None

    def validate(self):
        if self.kind not in {"source", "test", "config", "tool", "environment", "database"}:
            raise ValidationError("Unknown input fact category")
        identifier(self.name)
        if self.kind in {"source", "test", "config"}:
            repo_path(self.name)
        if self.value is not None and type(self.value) is not str:
            raise ValidationError("Input values must be digests or explicit availability values")
        if self.value not in {None, "absent", "none"}:
            content_hash(self.value)
        if self.value == "none" and self.kind != "database":
            raise ValidationError("Only an explicit database-free requirement may use none")


@dataclass(frozen=True)
class Unit:
    name: str
    group: str
    inputs: tuple[Fact, ...]
    mandatory: bool = False

    def validate(self):
        identifier(self.name)
        identifier(self.group)
        if type(self.inputs) is not tuple or any(type(f) is not Fact for f in self.inputs):
            raise ValidationError("Input facts must be immutable typed values")
        if type(self.mandatory) is not bool:
            raise ValidationError("Mandatory selection must be explicit")
        keys = []
        for fact in self.inputs:
            fact.validate()
            keys.append((fact.kind, fact.name))
        if len(keys) != len(set(keys)):
            raise ValidationError("Input facts must be unique")
        if {f.kind for f in self.inputs} != {
            "source",
            "test",
            "config",
            "tool",
            "environment",
            "database",
        }:
            raise ValidationError("Every input category must be declared, including none")

    @property
    def reusable(self):
        return all(f.value is not None for f in self.inputs)

    @property
    def input_digest(self):
        return fingerprint(sorted((f.kind, f.name, f.value) for f in self.inputs))


@dataclass(frozen=True)
class Discovery:
    status: Availability
    units: tuple[Unit, ...]
    known_paths: tuple[str, ...]
    producer_digest: str


@dataclass(frozen=True)
class Change:
    status: Availability
    paths: tuple[str, ...]
    base: str
    head: str


@dataclass(frozen=True)
class Group:
    key: str
    labels: tuple[str, ...]
    lane: str | None
    database: str
    switchable: bool
    cpus: int | None


@dataclass(frozen=True)
class Catalog:
    digest: str
    groups: tuple[Group, ...]

    def resolve(self, label):
        key = label.strip().casefold()
        for group in self.groups:
            if key in group.labels:
                return group
        raise ValidationError("The catalog does not declare this group")


@dataclass(frozen=True)
class Plan:
    scope: Scope
    attempt: str
    base: str
    head: str
    source_digest: str
    catalog_digest: str
    producer_digest: str
    units: tuple[Unit, ...]
    groups: tuple[Group, ...]
    prerequisites: tuple[tuple[str, tuple[str, ...]], ...]
    cacheable: bool
    reasons: tuple[tuple[str, str], ...]
    change_paths: tuple[str, ...]
    inventory_digest: str
    inventory_size: int
    selection_availability: str

    @property
    def digest(self):
        return fingerprint(self.record())

    def record(self):
        from .catalog import DATABASES, LANES

        self.scope.validate()
        identifier(self.attempt)
        for value in (self.base, self.head):
            if type(value) is not str or not re.fullmatch(r"[0-9a-f]{40}", value):
                raise SchemaError("Check plan requires immutable Git identities")
        for value in (
            self.source_digest,
            self.catalog_digest,
            self.producer_digest,
            self.inventory_digest,
        ):
            content_hash(value)
        if (
            type(self.inventory_size) is not int
            or self.inventory_size < 0
            or self.inventory_size != len(self.units) + len(self.reasons)
            or type(self.cacheable) is not bool
            or self.selection_availability != ("known_selected" if self.units else "known_empty")
            or (self.change_paths and self.inventory_size == 0)
        ):
            raise SchemaError("Invalid check inventory or selection facts")
        groups = {g.key for g in self.groups}
        if (
            len(groups) != len(self.groups)
            or (self.inventory_size and not groups)
            or any(u.group not in groups or not u.reusable for u in self.units)
        ):
            raise SchemaError("Check inventory groups or input facts are incomplete")
        for group in self.groups:
            identifier(group.key)
            if (
                type(group.labels) is not tuple
                or not group.labels
                or group.key != group.labels[0]
                or any(
                    type(label) is not str or label != label.strip().casefold()
                    for label in group.labels
                )
                or group.database not in DATABASES
                or (group.lane is not None and group.lane not in LANES)
                or type(group.switchable) is not bool
                or (group.cpus is not None and (type(group.cpus) is not int or group.cpus < 1))
            ):
                raise SchemaError("Stored group metadata is malformed")
        selected_names = [u.name for u in self.units]
        reason_names = []
        for row in self.reasons:
            if type(row) is not tuple or len(row) != 2:
                raise SchemaError("Nonselection reasons must name their unit")
            identifier(row[0])
            identifier(row[1])
            reason_names.append(row[0])
        if len(set(selected_names + reason_names)) != self.inventory_size:
            raise SchemaError("Every inventory unit requires one selection decision")
        order = {g.key: n for n, g in enumerate(self.groups)}
        for child, parents in self.prerequisites:
            if child not in order or any(
                p not in order or order[p] >= order[child] for p in parents
            ):
                raise SchemaError("Stored prerequisite order is invalid")
        for path in self.change_paths:
            repo_path(path)
        for item in self.units:
            item.validate()
        return dict(asdict(self), format=1)

    @classmethod
    def restore(cls, data):
        if (
            type(data) is not dict
            or set(data) != set(cls.__dataclass_fields__) | {"format"}
            or type(data.get("format")) is not int
            or data["format"] != 1
        ):
            raise SchemaError("Check plan predates the complete input-binding format")
        if any(
            type(data[k]) is not list
            for k in ("units", "groups", "prerequisites", "reasons", "change_paths")
        ):
            raise SchemaError("Check plan collections must be explicit arrays")
        return cls(
            Scope(**data["scope"]),
            data["attempt"],
            data["base"],
            data["head"],
            data["source_digest"],
            data["catalog_digest"],
            data["producer_digest"],
            tuple(
                Unit(u["name"], u["group"], tuple(Fact(**f) for f in u["inputs"]), u["mandatory"])
                for u in data["units"]
            ),
            tuple(
                Group(
                    g["key"],
                    tuple(g["labels"]),
                    g["lane"],
                    g["database"],
                    g["switchable"],
                    g["cpus"],
                )
                for g in data["groups"]
            ),
            tuple((k, tuple(v)) for k, v in data["prerequisites"]),
            data["cacheable"],
            tuple(tuple(r) for r in data["reasons"]),
            tuple(data["change_paths"]),
            data["inventory_digest"],
            data["inventory_size"],
            data["selection_availability"],
        )
