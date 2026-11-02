"""Verified provider capability and fixed read-only GraphQL operations."""

import hashlib
import json
from dataclasses import dataclass

from ai_task_manager.adapters.linear.errors import LinearError

_TYPE = "kind name ofType { kind name ofType { kind name ofType { kind name } } }"
SCHEMA = """query DependencySchema {
 issue: __type(name: "Issue") { fields { name args { name type { TYPE } } type { TYPE } } }
 relation: __type(name: "IssueRelation") { fields { name type { TYPE } } }
 connection: __type(name: "IssueRelationConnection") { fields { name type { TYPE } } }
 relationTypes: __type(name: "IssueRelationType") { enumValues { name } }
}""".replace("TYPE", _TYPE)
META = """id identifier createdAt updatedAt archivedAt trashed priority assignee { id }
 team { id key } project { id } state { id name }"""
METADATA = "query DependencyMetadata($id: String!) { issue(id: $id) { " + META + " } }"
PROSE = "query DependencyProse($id: String!) { issue(id: $id) { " + META + " title description } }"
RELATIONS = {
    direction: "query Dependency"
    + direction.title()
    + "($id: String!, $after: String) { issue(id: $id) { "
    + META
    + " "
    + direction
    + """(first: 50, after: $after, includeArchived: true) {
 nodes { id type updatedAt archivedAt issue { id } relatedIssue { id } }
 pageInfo { hasNextPage endCursor } } } }"""
    for direction in ("relations", "inverseRelations")
}


def named(value):
    while value and value.get("ofType"):
        value = value["ofType"]
    return value.get("name") if isinstance(value, dict) else None


@dataclass(frozen=True)
class DependencySchema:
    sha256: str

    @classmethod
    def verify(cls, data):
        try:
            issue = {f["name"]: f for f in data["issue"]["fields"]}
            for field in ("relations", "inverseRelations"):
                if named(issue[field]["type"]) != "IssueRelationConnection":
                    raise ValueError()
                args = {x["name"]: named(x["type"]) for x in issue[field]["args"]}
                if any(
                    args.get(k) != v
                    for k, v in {
                        "first": "Int",
                        "after": "String",
                        "includeArchived": "Boolean",
                    }.items()
                ):
                    raise ValueError()
            if named(issue["priority"]["type"]) != "Float":
                raise ValueError()
            fields = {f["name"]: named(f["type"]) for f in data["relation"]["fields"]}
            if any(
                fields.get(k) != v
                for k, v in {
                    "id": "ID",
                    "type": "String",
                    "updatedAt": "DateTime",
                    "archivedAt": "DateTime",
                    "issue": "Issue",
                    "relatedIssue": "Issue",
                }.items()
            ):
                raise ValueError()
            if not {"nodes", "pageInfo"} <= {f["name"] for f in data["connection"]["fields"]}:
                raise ValueError()
            if {v["name"] for v in data["relationTypes"]["enumValues"]} != {
                "blocks",
                "duplicate",
                "related",
                "similar",
            }:
                raise ValueError()
        except (KeyError, TypeError, ValueError) as exc:
            raise LinearError("unsupported_dependency_schema") from exc
        return cls(hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest())
