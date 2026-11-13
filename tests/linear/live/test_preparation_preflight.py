"""Fixed preparation profile over authored HTTP and frozen schema evidence; no live calls."""

import copy
import json
import os
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ai_task_manager.adapters.linear.models import Credentials
from ai_task_manager.adapters.linear.transport import HttpTransport
from tests.linear.helpers import EXPECTED

from .adapter_fixture import AdapterProvider
from .fixture_admin import FixtureAdministration
from .fixture_graphql import (
    INPUTS,
    OPERATIONS,
    PREPARATION_INPUTS,
    PREPARATION_REQUIRED,
    PREPARATION_SHAPE,
    REQUIRED,
    SHAPE,
    FixtureGraphQL,
)
from .ledger import DriverRefused, Limits, RunLedger, Scope


def frozen_shape():
    return json.loads(
        (Path(__file__).parents[1] / "frozen/fixture-admin-schema-v1.json").read_text()
    )


def authored_preparation_shape():
    shape = frozen_shape()
    query = next(item for item in shape.values() if item["name"] == "Query")
    # The older authored full fixture omitted project(id), which this profile uses.
    next(field for field in query["fields"] if field["name"] == "project")["args"] = [
        {"name": "id"}
    ]
    return shape


class FrozenSchemaProvider(AdapterProvider):
    def __init__(self, shape):
        self.shape = copy.deepcopy(shape)
        super().__init__()

    def graphql(self, query, variables):
        if query == SHAPE:
            return copy.deepcopy(self.shape)
        if query == PREPARATION_SHAPE:
            by_name = {item["name"]: item for item in self.shape.values()}
            return {
                **{
                    f"t{i}": copy.deepcopy(by_name[name])
                    for i, name in enumerate(PREPARATION_REQUIRED)
                },
                **{
                    f"i{i}": copy.deepcopy(by_name[name])
                    for i, name in enumerate(PREPARATION_INPUTS)
                },
            }
        return super().graphql(query, variables)


@contextmanager
def owned(tmp_path, *, shape=None, webhooks=0, comments=0):
    now = [time.time()]
    ledger = RunLedger(
        tmp_path / "ledger.sqlite",
        str(uuid4()),
        Scope(
            EXPECTED.workspace_id,
            EXPECTED.viewer_id,
            EXPECTED.team_id,
            EXPECTED.project_id,
            str(uuid4()),
        ),
        Limits(
            now[0] + 900,
            requests=100,
            projects=1,
            issues=1,
            webhooks=webhooks,
            comments=comments,
            started_at=now[0],
            cleanup_requests=20,
            cleanup_seconds=60,
        ),
        clock=lambda: now[0],
    )
    provider = FrozenSchemaProvider(shape if shape is not None else authored_preparation_shape())

    def client(**kwargs):
        return FixtureGraphQL(
            ledger,
            HttpTransport(timeout=2, _test_endpoint=provider.endpoint),
            lambda: Credentials("AUTHORED_SCHEMA_CONTROL"),
            clock=lambda: now[0],
            **kwargs,
        )

    try:
        yield SimpleNamespace(ledger=ledger, provider=provider, client=client, now=now)
    finally:
        provider.close()
        assert not provider.thread.is_alive()


def test_PF01_actual_frozen_schema_preserves_full_failure_and_preparation_pass(tmp_path):
    source = os.environ.get("DP01_ACTUAL_SHAPE_FILE")
    if not source:
        pytest.skip("Explicit frozen actual schema diagnosis required; no live lookup")
    actual = json.loads(Path(source).read_text())
    assert actual["findings"] == [
        {"type": "Webhook", "missing": ["teamIds"], "actual_type": "Webhook"}
    ]
    with owned(tmp_path, shape=actual["raw_shape"]) as p:
        with pytest.raises(DriverRefused, match="fixture_shape_unsupported"):
            p.client().preflight()
        preparation = p.client(profile="preparation")
        assert preparation.preflight().schema_sha256
        assert preparation.profile == "preparation"
        assert [row[0]["query"] for row in p.provider.requests] == [
            SHAPE,
            PREPARATION_SHAPE,
            # The profile uses the original fixed identity query.
            OPERATIONS["identity"],
        ]
        assert not p.provider.projects and not p.provider.issues and not p.provider.webhooks


@pytest.mark.parametrize(
    "missing",
    [
        "project_field",
        "issue_field",
        "project_argument",
        "issue_argument",
        "project_mutation_argument",
        "issue_mutation_argument",
        "project_input",
        "id_type",
    ],
)
def test_PF02_needed_schema_contract_refuses_before_mutation(tmp_path, missing):
    shape = authored_preparation_shape()
    by_name = {item["name"]: item for item in shape.values()}
    if missing in {"project_field", "issue_field"}:
        name, field = (
            ("Project", "creator") if missing == "project_field" else ("Issue", "assignee")
        )
        by_name[name]["fields"] = [f for f in by_name[name]["fields"] if f["name"] != field]
    elif missing in {"project_argument", "issue_argument"}:
        field = "project" if missing == "project_argument" else "issue"
        next(f for f in by_name["Query"]["fields"] if f["name"] == field)["args"] = []
    elif missing in {"project_mutation_argument", "issue_mutation_argument"}:
        field = "projectArchive" if missing == "project_mutation_argument" else "issueArchive"
        next(f for f in by_name["Mutation"]["fields"] if f["name"] == field)["args"] = []
    elif missing == "project_input":
        by_name["ProjectCreateInput"]["inputFields"] = [
            f for f in by_name["ProjectCreateInput"]["inputFields"] if f["name"] != "teamIds"
        ]
    else:
        next(f for f in by_name["ProjectCreateInput"]["inputFields"] if f["name"] == "id")["type"][
            "name"
        ] = "ID"
    with owned(tmp_path, shape=shape) as p:
        with pytest.raises(DriverRefused, match="fixture_shape_unsupported"):
            p.client(profile="preparation").preflight()
        assert len(p.provider.requests) == 1
        assert not p.provider.projects and p.ledger.summary()["operations"] == []


@pytest.mark.parametrize("webhooks,comments", [(1, 0), (0, 1), (1, 1)])
def test_PF03_preparation_rejects_nonzero_optional_budgets_before_composition(
    tmp_path, webhooks, comments
):
    with owned(tmp_path, webhooks=webhooks, comments=comments) as p:
        before = p.ledger.summary()
        with pytest.raises(DriverRefused, match="preparation_requires_zero_optional_resources"):
            p.client(profile="preparation")
        assert p.ledger.summary() == before and p.provider.requests == []


@pytest.mark.parametrize("profile", ["unknown", "", None, {}, True])
def test_PF04_unknown_profile_refuses_without_a_request(tmp_path, profile):
    with owned(tmp_path) as p:
        with pytest.raises(DriverRefused, match="unknown_fixture_profile"):
            p.client(profile=profile)
        assert p.provider.requests == []


@pytest.mark.parametrize(
    "operation",
    [
        "webhook_create",
        "webhook_metadata",
        "webhook_marker",
        "webhook_update",
        "webhook_delete",
        "webhook_ids",
        "comment_create",
        "comment_metadata",
    ],
)
def test_PF05_optional_operations_and_full_product_claims_refuse(tmp_path, operation):
    with owned(tmp_path) as p:
        client = p.client(profile="preparation")
        client.preflight()
        count = len(p.provider.requests)
        with pytest.raises(DriverRefused):
            client.request(operation, {})
        with pytest.raises(DriverRefused, match="full_fixture_profile_required"):
            client.product_capabilities()
        assert len(p.provider.requests) == count and p.ledger.summary()["operations"] == []


def test_PF06_refresh_keeps_profile_and_project_operation_contract(tmp_path):
    with owned(tmp_path) as p:
        client = p.client(profile="preparation")
        client.preflight()
        p.now[0] += 301
        client.refresh_if_needed()
        assert [r[0]["query"] for r in p.provider.requests].count(PREPARATION_SHAPE) == 2
        assert SHAPE not in [r[0]["query"] for r in p.provider.requests]
        admin = FixtureAdministration(client)
        assert admin.create_project("authored-project")["id"] == p.ledger.scope.project
        count = len(p.provider.requests)
        with pytest.raises(AttributeError):
            client.profile = "full"
        client._profile = "full"
        for call in (client.preflight, client.refresh_if_needed, client.product_capabilities):
            with pytest.raises(DriverRefused, match="fixture_profile_changed"):
                call()
        assert len(p.provider.requests) == count


def test_PF07_full_default_positive_and_capability_pair_remain_supported(tmp_path):
    with owned(tmp_path, shape=frozen_shape(), webhooks=1, comments=1) as p:
        client = p.client()
        client.preflight()
        assert client.profile == "full"
        assert len(client.product_capabilities()) == 2
        assert p.provider.requests[0][0]["query"] == SHAPE
        assert REQUIRED["Webhook"] and INPUTS["CommentCreateInput"]


def test_PF08_budget_binding_is_rechecked_after_preflight(tmp_path):
    with owned(tmp_path) as p:
        client = p.client(profile="preparation")
        client.preflight()
        count = len(p.provider.requests)
        p.ledger.limits = replace(p.ledger.limits, comments=1)
        with pytest.raises(DriverRefused, match="preparation_requires_zero_optional_resources"):
            client.request("identity", {"team": p.ledger.scope.team})
        assert len(p.provider.requests) == count
