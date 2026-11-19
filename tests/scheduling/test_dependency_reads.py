"""Exact authored wire replay and query counts; these are not live speed measurements."""

import copy
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.linear.client import LinearClient
from ai_task_manager.adapters.linear.dependencies.reader import DependencyReader
from ai_task_manager.adapters.linear.dependencies.schema import DETAILS, METADATA, PROSE, RELATIONS
from ai_task_manager.adapters.linear.models import Credentials, ExpectedBinding, WireResponse
from ai_task_manager.domains.work.models import Scope
from tests.scheduling.provider_fixture import ProviderFixture

BASELINE = Path(__file__).parent / "fixtures/dependency_reader_v1.json"


@pytest.mark.parametrize("name,requests", [("single", 9), ("paginated", 23)])
def test_exact_baseline_observation_with_fewer_requests(tmp_path, name, requests):
    capture = next(row for row in json.loads(BASELINE.read_text()) if row["case"] == name)
    p = ProviderFixture(tmp_path / "owned")
    p.expected = ExpectedBinding(**capture["expected"])
    p.scope = Scope(**capture["scope"])
    p.nodes, p.edges, p.states = (copy.deepcopy(capture[k]) for k in ["nodes", "edges", "states"])
    p.page_size = capture["page_size"]
    client = LinearClient(p, lambda: Credentials("authored-token"), p.expected, max_pages=5)
    local = {issue: SimpleNamespace(**value) for issue, value in capture["local"].items()}
    p.calls.clear()
    observation = DependencyReader(client, p.scope).observe(local[capture["root"]], local)
    assert json.loads(json.dumps(asdict(observation))) == capture["observation"]
    assert len(p.calls) == requests < capture["query_count"]
    assert sum("VerifyBinding" in q for q, _ in p.calls) == 2
    assert sum("DependencySchema" in q for q, _ in p.calls) == 1
    (tmp_path / "query-count.json").write_text(
        json.dumps(
            {
                "case": name,
                "before": capture["query_count"],
                "after": len(p.calls),
                "observation_identical": True,
                "live_speedup": "unmeasured",
            }
        )
    )


@pytest.mark.parametrize("field", ["relations", "inverseRelations", "title", "description"])
def test_missing_batched_field_is_unavailable(tmp_path, field):
    p = ProviderFixture(tmp_path / "owned")
    task = p.task()
    original = p.send

    def altered(query, variables, credentials):
        response = original(query, variables, credentials)
        if query == DETAILS:
            data = json.loads(response.body)
            data["data"]["issue"].pop(field)
            return WireResponse(200, {}, json.dumps(data).encode())
        return response

    p.send = altered
    assert p.refresh(task)["state"] == "unknown"


def test_combined_graphql_error_cannot_use_partial_data(tmp_path):
    p = ProviderFixture(tmp_path / "owned")
    task = p.task()
    original = p.send

    def altered(query, variables, credentials):
        response = original(query, variables, credentials)
        if query == DETAILS:
            data = json.loads(response.body)
            data["errors"] = [{"message": "authored failure"}]
            return WireResponse(200, {}, json.dumps(data).encode())
        return response

    p.send = altered
    assert p.refresh(task)["state"] == "unknown"


def test_forward_inverse_followup_cursors_remain_independent(tmp_path):
    p = ProviderFixture(tmp_path / "owned")
    a, b, root, x, y, z = [p.task() for _ in range(6)]
    p.edge(a, root)
    p.edge(b, root)
    for target in [x, y, z]:
        p.edge(root, target)
    p.page_size = 1
    assert p.refresh(root)["state"] == "blocked"
    calls = [(q, v) for q, v in p.calls if v.get("id") == root.issue_id]
    assert {v["after"] for q, v in calls if q == RELATIONS["relations"]} == {"1", "2"}
    assert {v["after"] for q, v in calls if q == RELATIONS["inverseRelations"]} == {"1"}
    assert sum(q == DETAILS for q, v in calls) == 2


def test_fixture_v2_retains_original_query_shapes(tmp_path):
    p = ProviderFixture(tmp_path / "owned")
    task = p.task()
    node = task.issue_id
    metadata = p.client.request(METADATA, {"id": node})["issue"]
    assert "title" not in metadata and "description" not in metadata
    prose = p.client.request(PROSE, {"id": node})["issue"]
    assert prose["title"] == task.title and not {"relations", "inverseRelations"} & prose.keys()
    for direction, query in RELATIONS.items():
        raw = p.client.request(query, {"id": node, "after": None})["issue"]
        other = "relations" if direction == "inverseRelations" else "inverseRelations"
        assert direction in raw and other not in raw and "title" not in raw


@pytest.mark.parametrize("field", ["assignee", "prose", "state", "priority", "revision"])
def test_post_metadata_change_is_not_hidden_by_batch(tmp_path, field):
    from uuid import uuid4

    p = ProviderFixture(tmp_path / "owned")
    task = p.task()
    injected = []

    def mutate(query, variables):
        if query != DETAILS:
            return
        assert any(q == METADATA for q, _ in p.calls)
        node = p.nodes[task.issue_id]
        if field == "assignee":
            node["assignee"]["id"] = str(uuid4())
        elif field == "prose":
            node["description"] = "unreviewed change"
        elif field == "state":
            node["state"] = {"id": p.states["Done"], "name": "Done"}
        elif field == "priority":
            node["priority"] = 1
        else:
            node["updatedAt"] = "2026-11-02T00:00:01Z"
        injected.append(field)

    p.callback = mutate
    assert p.refresh(task)["state"] == "unknown"
    assert injected == [field]
