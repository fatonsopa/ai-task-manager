"""Authored HTTP provider; no credential, account or live Linear observation."""

import copy
import json
from pathlib import Path

from tests.linear.helpers import EXPECTED, ProviderFixture

from .fixture_graphql import OPERATIONS, SHAPE


class AdapterProvider(ProviderFixture):
    def __init__(self):
        self.projects, self.webhooks = {}, {}
        self.transform = None
        super().__init__()

    def identity(self):
        return {
            "viewer": {"id": EXPECTED.viewer_id, "admin": True},
            "organization": {"id": EXPECTED.workspace_id},
        }

    def graphql(self, query, variables):
        result = self._fixture_query(query, variables)
        return self.transform(query, variables, result) if self.transform else result

    def _fixture_query(self, query, variables):
        if query == SHAPE:
            return json.loads(
                (Path(__file__).parents[1] / "frozen/fixture-admin-schema-v1.json").read_text()
            )
        name = next((name for name, document in OPERATIONS.items() if document == query), None)
        if name == "identity":
            return {**self.identity(), "team": {"id": EXPECTED.team_id, "key": "TST"}}
        if name == "project_create":
            values = variables["input"]
            item = {
                **values,
                "creator": {"id": EXPECTED.viewer_id},
                "lead": {"id": values["leadId"]},
                "archivedAt": None,
                "trashed": False,
                "updatedAt": self.tick(),
            }
            self.projects[values["id"]] = item
            return {"projectCreate": {"success": True, "project": {"id": values["id"]}}}
        if name == "webhook_create":
            values = variables["input"]
            self.webhooks[values["id"]] = {
                **values,
                "creator": {"id": EXPECTED.viewer_id},
                "team": {"id": values["teamId"]},
                "teamIds": None,
                "updatedAt": self.tick(),
                "archivedAt": None,
            }
            return {
                "webhookCreate": {
                    "success": True,
                    "webhook": {"id": values["id"], "enabled": values["enabled"]},
                }
            }
        if name == "webhook_update":
            item = self.webhooks[variables["id"]]
            item.update(variables["input"])
            item["updatedAt"] = self.tick()
            return {
                "webhookUpdate": {
                    "success": True,
                    "webhook": {"id": item["id"], "enabled": item["enabled"]},
                }
            }
        if name == "webhook_delete":
            self.webhooks.pop(variables["id"], None)
            return {"webhookDelete": {"success": True, "entityId": variables["id"]}}
        if name in {"project_archive", "issue_archive"}:
            kind = name.split("_")[0]
            item = (self.projects if kind == "project" else self.issues)[variables["id"]]
            item["archivedAt"] = self.tick()
            item["updatedAt"] = self.tick()
            return {
                kind + "Archive": {
                    "success": True,
                    "entity": {"id": item["id"], "archivedAt": item["archivedAt"]},
                }
            }
        if name == "webhook_ids":
            return {
                **self.identity(),
                "webhooks": {
                    "nodes": [{"id": value} for value in self.webhooks],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }
        if name and name.endswith(("_metadata", "_marker")):
            kind, purpose = name.split("_")
            table = {"project": self.projects, "webhook": self.webhooks, "issue": self.issues}[kind]
            item = copy.deepcopy(table.get(variables["id"]))
            if item is not None:
                if kind == "project":
                    item["teams"] = {
                        "nodes": [{"id": value} for value in item["teamIds"]],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                if purpose == "marker":
                    names = {
                        "project": {"description", "content"},
                        "webhook": {"label"},
                        "issue": {"title", "description"},
                    }[kind] | {"id", "updatedAt"}
                    item = {key: value for key, value in item.items() if key in names}
                else:
                    for key in ("secret", "label", "description", "title", "content"):
                        item.pop(key, None)
            return {**self.identity(), kind: item}
        result = super().graphql(query, variables)
        if "mutation CreateIssue" in query:
            self.issues[result["issueCreate"]["issue"]["id"]].update(
                creator={"id": EXPECTED.viewer_id}, archivedAt=None
            )
        return result
