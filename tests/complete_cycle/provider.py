"""Declared empty dependency graph through the existing loopback HTTP provider."""

import copy
import json
from pathlib import Path

from tests.linear.live.adapter_fixture import AdapterProvider


class CycleProvider(AdapterProvider):
    def graphql(self, query, variables):
        if "DependencySchema" in query:
            return json.loads(
                Path(__file__)
                .parents[1]
                .joinpath("scheduling/fixtures/dependency_schema.json")
                .read_text()
            )
        if "query Dependency" in query:
            value = copy.deepcopy(self.issues[variables["id"]])
            value.update(
                createdAt=value.get("createdAt", value["updatedAt"]),
                archivedAt=None,
                trashed=False,
                priority=3,
            )
            if "DependencyProse" not in query:
                value.pop("title", None)
                value.pop("description", None)
            for name in ("relations", "inverseRelations"):
                if name + "(first:" in query:
                    value[name] = {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
            return {"issue": value}
        result = super().graphql(query, variables)
        if "mutation CreateIssue" in query:
            value = self.issues[result["issueCreate"]["issue"]["id"]]
            value["createdAt"] = value["updatedAt"]
        return result
