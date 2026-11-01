import json
from dataclasses import replace

import pytest
from ai_task_manager.adapters.linear.client import LinearClient
from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.linear.models import Credentials, WireResponse
from ai_task_manager.adapters.linear.transport import HttpTransport

from tests.linear.helpers import EXPECTED, LinearRig, ProviderFixture, uid


class ScriptedWire:
    def __init__(self, responses):
        self.responses, self.calls = iter(responses), []

    def send(self, query, variables, credentials):
        self.calls.append((query, variables, credentials.authorization()))
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value


def response(status=200, data=None, **headers):
    return WireResponse(
        status, headers, json.dumps(data if data is not None else {"data": {}}).encode()
    )


@pytest.mark.parametrize(
    "wire,category",
    [
        (response(401), "authentication"),
        (response(403), "authentication"),
        (response(429, **{"retry-after": "17"}), "rate_limited"),
        (response(500), "http_failure"),
        (WireResponse(200, {}, b"invalid"), "malformed_response"),
        (
            response(
                200,
                {
                    "data": {"partial": True},
                    "errors": [{"message": "private issue secret", "extensions": {"code": "FAIL"}}],
                },
            ),
            "graphql_failure",
        ),
        (response(200, {"errors": [{"extensions": {"code": "RATELIMITED"}}]}), "rate_limited"),
        (response(200, {"data": None}), "missing_data"),
        (response(200, []), "malformed_response"),
        (LinearError("transport_failure", uncertain=True), "transport_failure"),
    ],
)
def test_l02_safe_response_taxonomy_and_mutation_uncertainty(wire, category):
    client = LinearClient(ScriptedWire([wire]), lambda: Credentials("fixture-secret"), EXPECTED)
    with pytest.raises(LinearError) as caught:
        client.request("mutation Fixture", {"input": {"title": "private issue"}}, mutation=True)
    assert caught.value.category == category
    assert "private" not in str(caught.value) and "secret" not in str(caught.value)
    if category not in {"authentication"}:
        assert caught.value.uncertain


@pytest.mark.parametrize(
    "kind,expected", [("api_key", "fixture-secret"), ("oauth", "Bearer fixture-secret")]
)
def test_l01_auth_form_is_explicit(kind, expected):
    wire = ScriptedWire([response()])
    client = LinearClient(wire, lambda: Credentials("fixture-secret", kind), EXPECTED)
    client.request("query Fixture", {"title": 'quote" } mutation Inject {'})
    assert wire.calls[0][2] == expected and wire.calls[0][1]["title"].startswith('quote"')
    assert "fixture-secret" not in repr(Credentials("fixture-secret", kind))


def page(nodes, more, cursor):
    return response(
        data={
            "data": {
                "items": {"nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}
            }
        }
    )


def test_l03_complete_cursor_pagination():
    wire = ScriptedWire(
        [page([{"id": uid(1)}], True, "cursor-1"), page([{"id": uid(2)}], False, None)]
    )
    client = LinearClient(wire, lambda: Credentials("fixture-secret"), EXPECTED)
    assert len(client.paginate("query Page", {}, ("items",))) == 2
    assert [call[1]["after"] for call in wire.calls] == [None, "cursor-1"]


@pytest.mark.parametrize(
    "responses",
    [
        [page([], True, None)],
        [page([], True, "x"), page([], True, "x")],
        [page([{"id": uid(1)}], True, "x"), page([{"id": uid(1)}], False, None)],
        [page([], True, "x"), response(503)],
    ],
)
def test_l03_incomplete_or_ambiguous_pagination_is_not_empty_success(responses):
    client = LinearClient(ScriptedWire(responses), lambda: Credentials("fixture-secret"), EXPECTED)
    with pytest.raises(LinearError):
        client.paginate("query Page", {}, ("items",))


def test_l03_bounded_page_ceiling():
    client = LinearClient(
        ScriptedWire([page([], True, "x")]),
        lambda: Credentials("fixture-secret"),
        EXPECTED,
        max_pages=1,
    )
    with pytest.raises(LinearError, match="pagination_limit"):
        client.paginate("query Page", {}, ("items",))


@pytest.mark.parametrize(
    "field", ["viewer_id", "workspace_id", "team_id", "team_key", "project_id"]
)
def test_l04_wrong_binding_blocks_before_mutations(field):
    provider = ProviderFixture()
    try:
        expected = replace(EXPECTED, **{field: "OTHER" if field == "team_key" else uid(900)})
        client = LinearClient(
            HttpTransport(_test_endpoint=provider.endpoint),
            lambda: Credentials("fixture-secret"),
            expected,
        )
        with pytest.raises(LinearError):
            client.verify_binding()
        assert not provider.issues
    finally:
        provider.close()


def test_l01_model_controlled_endpoint_redirect_and_response_bounds_are_refused(tmp_path):
    for endpoint in ("https://untrusted.example/graphql", "http://user:secret@127.0.0.1/graphql"):
        with pytest.raises(LinearError):
            HttpTransport(_test_endpoint=endpoint)
    with pytest.raises(LinearError):
        HttpTransport(timeout=float("inf"))
    provider = ProviderFixture()
    try:
        provider.fail_next = (302, b"redirect")
        client = LinearClient(
            HttpTransport(_test_endpoint=provider.endpoint),
            lambda: Credentials("fixture-secret"),
            EXPECTED,
        )
        with pytest.raises(LinearError, match="http_failure"):
            client.verify_binding()
        provider.fail_next = (200, b"x" * 200)
        transport = HttpTransport(_test_endpoint=provider.endpoint, max_response_bytes=100)
        with pytest.raises(LinearError, match="response_too_large"):
            transport.send("query Fixture", {}, Credentials("fixture-secret"))
    finally:
        provider.close()


def test_l04_issue_number_and_team_shorthand_are_not_canonical_identity(tmp_path):
    provider = ProviderFixture()
    try:
        rig = LinearRig(tmp_path, provider)
        before = len(provider.requests)
        for value in ("74", "TST-74", "CG-74", "not-an-id"):
            with pytest.raises(LinearError, match="invalid_canonical_identity"):
                rig.client.read_issue(value)
        assert len(provider.requests) == before
    finally:
        provider.close()


def test_l04_changed_credentials_cannot_reuse_a_previous_verified_account_binding(tmp_path):
    provider = ProviderFixture()
    try:
        rig = LinearRig(tmp_path, provider)
        task = rig.task(confirmed=False)
        before = len(provider.requests)
        rig.client.credentials = lambda: Credentials("another-account-secret")
        with pytest.raises(LinearError, match="credential_binding_changed"):
            rig.client.create_issue(task)
        assert len(provider.requests) == before and not provider.issues
    finally:
        provider.close()
