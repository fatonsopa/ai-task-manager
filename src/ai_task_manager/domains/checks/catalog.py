"""The single validation rule for the project test-group catalog."""

from ai_task_manager.domains.work.errors import ValidationError

from .models import Catalog, Group, content_hash

DATABASES = frozenset({"throwaway", "shared", "test-rung", "staging", "none"})
LANES = frozenset({"plain", "integ", "serial", "deno", "web", "catalog", "prev"})


def validate_catalog(data, digest):
    content_hash(digest)
    if type(data) is not dict or type(data.get("groups")) is not list or not data["groups"]:
        raise ValidationError("Catalog requires an explicit nonempty groups array")
    groups, seen = [], set()
    for row in data["groups"]:
        if type(row) is not dict:
            raise ValidationError("Catalog rows must be objects")
        labels = row.get("labels")
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
        database, switchable = row.get("database"), row.get("switchable")
        if database not in DATABASES or type(switchable) is not bool:
            raise ValidationError("Catalog database and switchable fields are required")
        if switchable and database not in {"shared", "throwaway"}:
            raise ValidationError("Only shared or throwaway groups may be switchable")
        if type(row.get("why")) is not str or not row["why"].strip():
            raise ValidationError("Catalog requires the database reason")
        lane = row.get("lane")
        if "lane" in row and lane not in LANES:
            raise ValidationError("Unknown catalog lane")
        cpus = row.get("cpus")
        if "cpus" in row and (
            type(cpus) is not int
            or cpus < 1
            or type(row.get("cpus_why")) is not str
            or not row["cpus_why"].strip()
        ):
            raise ValidationError("CPU demand needs a positive integer and measurement reason")
        groups.append(Group(names[0], names, lane, database, switchable, cpus))
    return Catalog(digest, tuple(groups))
