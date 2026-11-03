"""Project-neutral declarations validated against a closed trusted capability set."""

from dataclasses import asdict, dataclass

from ai_task_manager.domains.work.errors import ValidationError

from .models import Catalog, Group, content_hash, fingerprint, identifier


@dataclass(frozen=True)
class RunnerCapability:
    key: str
    categories: tuple[str, ...]
    input_kinds: tuple[str, ...]
    implementation_digest: str


@dataclass(frozen=True)
class ResourceCapability:
    key: str
    semantic_keys: tuple[str, ...]
    implementation_digest: str


@dataclass(frozen=True)
class Capabilities:
    runners: tuple[RunnerCapability, ...]
    resources: tuple[ResourceCapability, ...]
    required_groups: tuple[str, ...] = ()

    def validate(self):
        if type(self.runners) is not tuple or not self.runners:
            raise ValidationError("Trusted runner capabilities are required")
        if type(self.resources) is not tuple or type(self.required_groups) is not tuple:
            raise ValidationError("Capabilities must be immutable")
        for rows, kind in ((self.runners, RunnerCapability), (self.resources, ResourceCapability)):
            if any(type(r) is not kind for r in rows):
                raise ValidationError("Capabilities must have unique typed identities")
            for row in rows:
                identifier(row.key)
                content_hash(row.implementation_digest)
                fields = (
                    (row.categories, row.input_kinds)
                    if kind is RunnerCapability
                    else (row.semantic_keys,)
                )
                for values in fields:
                    if type(values) is not tuple or not values or len(set(values)) != len(values):
                        raise ValidationError("Capability requirements must be explicit and unique")
                    for value in values:
                        identifier(value)
                if kind is RunnerCapability and not set(row.input_kinds) <= {
                    "source",
                    "test",
                    "config",
                    "tool",
                    "environment",
                    "resource",
                }:
                    raise ValidationError("Unsupported input category")
            if len({r.key for r in rows}) != len(rows):
                raise ValidationError("Capabilities must have unique identities")
        for key in self.required_groups:
            identifier(key)
        if len(set(self.required_groups)) != len(self.required_groups):
            raise ValidationError("Required checks must be unique")
        for key in self.required_groups:
            identifier(key)

    @property
    def digest(self):
        self.validate()
        return fingerprint(asdict(self))


def validate_catalog(data, digest, capabilities):
    """Only trusted composition supplies capabilities; catalog JSON cannot register code."""
    content_hash(digest)
    if type(capabilities) is not Capabilities:
        raise ValidationError("Configured capabilities are required")
    capabilities.validate()
    if (
        type(data) is not dict
        or set(data) != {"format", "groups"}
        or type(data["format"]) is not int
        or data["format"] != 2
        or type(data["groups"]) is not list
        or not 0 < len(data["groups"]) <= 256
    ):
        raise ValidationError("Catalog requires format 2 and explicit groups")
    runners = {r.key: r for r in capabilities.runners}
    resources = {r.key: r for r in capabilities.resources}
    groups, seen = [], set()
    fields = {"labels", "category", "runner", "resources", "required", "prerequisites"}
    for row in data["groups"]:
        if type(row) is not dict or set(row) != fields:
            raise ValidationError("Catalog row fields must match the neutral schema")
        labels = row["labels"]
        if (
            type(labels) is not list
            or not labels
            or any(type(x) is not str or not x.strip() or len(x) > 256 for x in labels)
        ):
            raise ValidationError("Catalog labels must be nonempty bounded names")
        names = tuple(x.strip().casefold() for x in labels)
        if len(set(names)) != len(names) or seen.intersection(names):
            raise ValidationError("Catalog aliases must be unique")
        seen.update(names)
        runner = runners.get(row["runner"]) if type(row["runner"]) is str else None
        if runner is None or row["category"] not in runner.categories:
            raise ValidationError("Runner or category is not configured")
        for field in ("resources", "prerequisites"):
            if type(row[field]) is not list or any(type(v) is not str for v in row[field]):
                raise ValidationError("Requirements must be explicit arrays")
        if any(key not in resources for key in row["resources"]):
            raise ValidationError("Resource capability is not configured")
        semantic = tuple(
            f"{key}/{name}" for key in row["resources"] for name in resources[key].semantic_keys
        )
        group = Group(
            names[0],
            names,
            row["category"],
            row["runner"],
            tuple(row["resources"]),
            row["required"],
            tuple(row["prerequisites"]),
            runner.input_kinds,
            semantic,
        )
        group.validate()
        groups.append(group)
    by_key = {g.key: g for g in groups}
    for key in capabilities.required_groups:
        if key not in by_key or not by_key[key].required:
            raise ValidationError("Configured required check cannot be omitted or weakened")
    visiting, done = set(), set()

    def visit(key):
        if key not in by_key or key in visiting:
            raise ValidationError("Unknown or cyclic prerequisites")
        if key in done:
            return
        visiting.add(key)
        for parent in by_key[key].prerequisites:
            visit(parent)
        visiting.remove(key)
        done.add(key)

    for key in by_key:
        visit(key)
    return Catalog(digest, capabilities.digest, tuple(groups))
