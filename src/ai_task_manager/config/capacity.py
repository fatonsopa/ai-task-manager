"""Parse explicit finite worker limits for the existing Core capacity policy."""

from ai_task_manager.domains.work.errors import EvidenceRequired, ValidationError
from ai_task_manager.domains.work.models import CapacityPolicy


def policies(config):
    if "capacity_policies" not in config:
        return ()
    rows = config["capacity_policies"]
    if type(rows) is not list or not 1 <= len(rows) <= 2:
        raise ValidationError("Declare finite capacity for each selected native host")
    result = []
    for row in rows:
        if (
            type(row) is not dict
            or set(row) != {"host", "host_limit", "project_limit"}
            or type(row["host"]) is not str
            or row["host"] not in {"claude", "codex"}
            or any(type(row[k]) is not int or row[k] < 1 for k in ("host_limit", "project_limit"))
            or any(p.host == row["host"] for p in result)
        ):
            raise ValidationError("Exact distinct positive native capacity limits required")
        result.append(CapacityPolicy(**row))
    if len({p.project_limit for p in result}) != 1:
        raise ValidationError("The global project capacity must be consistent across hosts")
    return tuple(result)


def require_installed(database, expected):
    """Runtime loads validate trusted setup facts and never install new policy."""
    with database.transaction() as conn:
        for policy in expected:
            row = conn.execute(
                "SELECT host_limit,project_limit FROM capacity WHERE host=?", (policy.host,)
            ).fetchone()
            if row is None or tuple(row) != (policy.host_limit, policy.project_limit):
                raise EvidenceRequired("Declared capacity requires matching trusted setup")
