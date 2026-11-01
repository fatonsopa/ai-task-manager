import json

from ai_task_manager.domains.work.errors import CapacityExceeded, ValidationError
from ai_task_manager.domains.work.models import CapacityPolicy, Scope


def configure(conn, policy: CapacityPolicy):
    if (
        not policy.host
        or type(policy.host_limit) is not int
        or type(policy.project_limit) is not int
        or min(policy.host_limit, policy.project_limit) < 1
    ):
        raise ValidationError("Capacity must have finite positive integer limits")
    row = conn.execute("SELECT * FROM capacity WHERE host=?", (policy.host,)).fetchone()
    if row and (row["host_limit"], row["project_limit"]) != (
        policy.host_limit,
        policy.project_limit,
    ):
        raise ValidationError("Persisted capacity cannot be replaced by a caller configuration")
    if not row:
        other = conn.execute("SELECT project_limit FROM capacity LIMIT 1").fetchone()
        if other and other[0] != policy.project_limit:
            raise ValidationError("The global project capacity must be consistent across hosts")
        conn.execute(
            "INSERT INTO capacity VALUES(?,?,?)",
            (policy.host, policy.host_limit, policy.project_limit),
        )


def check(conn, host: str, scope: Scope):
    policy = conn.execute("SELECT * FROM capacity WHERE host=?", (host,)).fetchone()
    if policy is None:
        raise CapacityExceeded("Host capacity has not been configured")
    rows = conn.execute(
        "SELECT s.scope FROM attempts a JOIN sessions s ON s.id=a.session_id "
        "WHERE a.host=? AND a.active=1",
        (host,),
    ).fetchall()
    project_rows = conn.execute(
        "SELECT s.scope FROM attempts a JOIN sessions s ON s.id=a.session_id WHERE a.active=1"
    ).fetchall()
    project_count = sum(
        1
        for row in project_rows
        if all(
            json.loads(row[0])[key] == getattr(scope, key)
            for key in ("provider", "workspace", "project")
        )
    )
    if len(rows) >= policy["host_limit"] or project_count >= policy["project_limit"]:
        raise CapacityExceeded("Configured worker capacity is exhausted")
