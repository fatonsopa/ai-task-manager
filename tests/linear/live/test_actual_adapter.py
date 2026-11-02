"""Source-bound local HTTP tests, never live provider or fresh model evidence."""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest

from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.linear.models import Credentials
from ai_task_manager.adapters.linear.transport import HttpTransport
from tests.linear.helpers import EXPECTED

from .adapter_fixture import AdapterProvider
from .fixture_admin import FixtureAdministration, VerifiedEndpoint, adapter_limits_v1
from .fixture_graphql import OPERATIONS, REQUIRED, SHAPE, FixtureGraphQL
from .fixture_http import FixtureRequestTransport
from .ledger import DriverRefused, RunLedger, Scope


class AuthoredEndpoint:
    """Synthetic observer port; this is not an HTTPS/tunnel readiness receipt."""

    def __init__(self, ledger, candidate):
        s = ledger.scope
        self.observed = VerifiedEndpoint(
            "https://fixture.invalid/linear/owned",
            "authored-receiver",
            "authored-tunnel",
            ledger.clock() + 1000,
            ledger.run_id,
            s.workspace,
            s.owner,
            s.team,
            s.project,
            candidate,
        )

    def verify(self, reference):
        if reference != "authored-endpoint":
            raise DriverRefused("actual_endpoint_admission_required")
        return self.observed


@pytest.fixture
def adapter(tmp_path):
    os.chmod(tmp_path, 0o700)
    now = [time.time()]
    scope = Scope(
        EXPECTED.workspace_id,
        EXPECTED.viewer_id,
        EXPECTED.team_id,
        EXPECTED.project_id,
        str(uuid4()),
    )
    ledger = RunLedger(
        tmp_path / "driver.sqlite",
        str(uuid4()),
        scope,
        adapter_limits_v1(now[0], now[0] + 1200),
        clock=lambda: now[0],
    )
    provider = AdapterProvider()
    graphql = FixtureGraphQL(
        ledger,
        HttpTransport(timeout=2, _test_endpoint=provider.endpoint),
        lambda: Credentials("AUTHORED_NOT_LIVE"),
        clock=lambda: now[0],
    )
    candidate = str(uuid4())
    endpoint = AuthoredEndpoint(ledger, candidate)
    admin = FixtureAdministration(graphql, endpoint_authority=endpoint, quiescence=lambda: None)
    try:
        yield admin, graphql, ledger, provider, endpoint, candidate, now
    finally:
        provider.close()


def ready(adapter, *, webhook=False):
    admin, graphql, ledger, provider, endpoint, candidate, now = adapter
    graphql.preflight()
    admin.create_project("project")
    if webhook:
        graphql.actions.reserve_receiver(candidate)
        admin.create_webhook(
            "hook", candidate, "authored-endpoint", lambda: "AUTHORED_SIGNING_SECRET_32_BYTES_LONG"
        )
    return adapter


def operations(provider):
    return [request[0]["query"] for request in provider.requests]


def test_a01_a05_a06_actual_http_project_fresh_reads_and_accounting(adapter):
    admin, graphql, ledger, provider, *_ = ready(adapter)
    assert ledger.resource(ledger.scope.project)["kind"] == "project"
    assert operations(provider) == [
        SHAPE,
        OPERATIONS["identity"],
        OPERATIONS["project_create"],
        OPERATIONS["project_metadata"],
        OPERATIONS["project_marker"],
    ]
    assert len(ledger.summary()["requests"]) == len(provider.requests) == 5
    assert graphql.product_capabilities()[0].schema_digest


@pytest.mark.parametrize("target", ["field", "argument", "input", "idtype"])
def test_a02_missing_schema_shape_refuses_before_mutation(adapter, target):
    admin, graphql, ledger, provider, *_ = adapter

    def change(query, variables, result):
        if query == SHAPE:
            if target == "field":
                result["t0"]["fields"] = []
            elif target == "argument":
                item = result[f"t{list(REQUIRED).index('Mutation')}"]
                next(f for f in item["fields"] if f["name"] == "projectArchive")["args"] = []
            elif target == "input":
                result["i0"]["inputFields"] = []
            else:
                next(f for f in result["i0"]["inputFields"] if f["name"] == "id")["type"][
                    "name"
                ] = "ID"
        return result

    provider.transform = change
    with pytest.raises(DriverRefused, match="shape"):
        graphql.preflight()
    assert not provider.projects and not provider.webhooks


@pytest.mark.parametrize("target", ["viewer", "organization", "team", "admin"])
def test_a03_identity_mismatch_refuses_before_effect(adapter, target):
    admin, graphql, ledger, provider, *_ = adapter

    def change(query, variables, result):
        if query == OPERATIONS["identity"]:
            if target == "admin":
                result["viewer"]["admin"] = False
            else:
                result[target]["id"] = str(uuid4())
        return result

    provider.transform = change
    with pytest.raises(DriverRefused):
        graphql.preflight()
    assert not provider.projects


@pytest.mark.parametrize(
    "status,body",
    [
        (401, b"PRIVATE"),
        (403, b"PRIVATE"),
        (429, b"PRIVATE"),
        (302, b"PRIVATE"),
        (200, b"PRIVATE"),
        (200, b'{"errors":[{"message":"PRIVATE"}],"data":{}}'),
    ],
)
def test_a04_actual_http_errors_have_safe_categories(adapter, status, body):
    _, graphql, ledger, provider, *_ = adapter
    provider.fail_next = (status, body)
    with pytest.raises(LinearError) as caught:
        graphql.preflight()
    assert "PRIVATE" not in str(caught.value) and not provider.projects
    assert len(ledger.summary()["requests"]) == 1


def test_a03_changed_credentials_refuse_without_another_http_call(adapter):
    _, graphql, _, provider, *_ = ready(adapter)
    before = len(provider.requests)
    graphql._credentials = lambda: Credentials("DIFFERENT_AUTHORED_KEY")
    with pytest.raises(DriverRefused, match="credentials"):
        graphql.request("project_metadata", {"id": EXPECTED.project_id, "after": None})
    assert len(provider.requests) == before


@pytest.mark.parametrize(
    "target", ["creator", "team", "workspace", "marker", "revision", "candidate"]
)
def test_a07_a08_a10_a37_foreign_or_changed_readback_retains_uncertainty(adapter, target):
    admin, graphql, ledger, provider, *_ = adapter
    graphql.preflight()

    def change(query, variables, result):
        if query == OPERATIONS["project_metadata"]:
            if target == "creator":
                result["project"]["creator"]["id"] = str(uuid4())
            if target == "team":
                result["project"]["teams"]["nodes"][0]["id"] = str(uuid4())
            if target == "workspace":
                result["organization"]["id"] = str(uuid4())
        if query == OPERATIONS["project_marker"]:
            if target == "marker":
                result["project"]["content"] = "FOREIGN_PRIVATE_MARKER"
            if target == "revision":
                result["project"]["updatedAt"] = provider.tick()
        if query == OPERATIONS["project_create"] and target == "candidate":
            result["projectCreate"]["project"]["id"] = str(uuid4())
        return result

    provider.transform = change
    with pytest.raises(DriverRefused, match="unconfirmed"):
        admin.create_project("project")
    assert ledger.get("project")["state"] == "uncertain"
    if target in {"creator", "team", "workspace", "candidate"}:
        assert OPERATIONS["project_marker"] not in operations(provider)
    assert b"FOREIGN_PRIVATE_MARKER" not in ledger.path.read_bytes()


def test_a11_committed_create_replay_and_alias_do_not_duplicate_http(adapter):
    admin, _, ledger, provider, *_ = ready(adapter)
    before = len(provider.requests)
    admin.create_project("project")
    with pytest.raises(DriverRefused, match="resource_budget_exhausted"):
        admin.create_project("alias")
    assert len(provider.requests) == before and len(provider.projects) == 1


def test_a12_actual_effect_then_lost_local_reply_recovers_without_resend(adapter):
    admin, graphql, ledger, provider, *_ = adapter
    graphql.preflight()
    underlying = graphql.client.transport

    class LoseReply:
        def send(self, query, variables, credentials):
            response = underlying.send(query, variables, credentials)
            if query == OPERATIONS["project_create"]:
                raise LinearError("authored_lost_local_reply", uncertain=True)
            return response

    graphql.client.transport = LoseReply()
    with pytest.raises(DriverRefused, match="unconfirmed"):
        admin.create_project("project")
    assert len(provider.projects) == 1 and ledger.get("project")["state"] == "uncertain"
    admin.recover_create("project")
    assert ledger.get("project")["state"] == "confirmed"
    assert operations(provider).count(OPERATIONS["project_create"]) == 1


def test_a13_actual_threads_share_one_candidate_and_effect(adapter):
    admin, graphql, ledger, provider, *_ = adapter
    graphql.preflight()
    barrier = threading.Barrier(2)

    def run(key):
        barrier.wait(2)
        try:
            return admin.create_project(key)
        except DriverRefused:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, ["one", "two"]))
    assert (
        len(provider.projects) == 1
        and operations(provider).count(OPERATIONS["project_create"]) == 1
    )


def test_a14_a16_disabled_hook_and_secret_not_in_journal(adapter):
    admin, _, ledger, provider, _, candidate, _ = ready(adapter, webhook=True)
    hook = provider.webhooks[candidate]
    assert (
        hook["enabled"] is False
        and hook["allPublicTeams"] is False
        and hook["resourceTypes"] == ["Issue"]
    )
    assert hook["teamId"] == ledger.scope.team
    assert b"AUTHORED_SIGNING_SECRET_32_BYTES_LONG" not in ledger.path.read_bytes()
    assert b"AUTHORED_NOT_LIVE" not in ledger.path.read_bytes()


@pytest.mark.parametrize("target", ["expired", "url", "run", "scope", "candidate", "missing"])
def test_a15_endpoint_admission_is_bound_and_fails_before_effect(adapter, target):
    admin, graphql, ledger, provider, endpoint, candidate, now = ready(adapter)
    graphql.actions.reserve_receiver(candidate)
    if target == "expired":
        endpoint.observed = replace(endpoint.observed, expires_at=now[0])
    if target == "url":
        endpoint.observed = replace(endpoint.observed, url="http://fixture.invalid/linear/owned")
    if target == "run":
        endpoint.observed = replace(endpoint.observed, run_id=str(uuid4()))
    if target == "scope":
        endpoint.observed = replace(endpoint.observed, owner=str(uuid4()))
    if target == "candidate":
        endpoint.observed = replace(endpoint.observed, webhook_id=str(uuid4()))
    before = len(provider.requests)
    with pytest.raises(DriverRefused):
        admin.create_webhook(
            "hook",
            candidate,
            "missing" if target == "missing" else "authored-endpoint",
            lambda: "x" * 32,
        )
    assert len(provider.requests) == before and not provider.webhooks


def test_a17_a18_enable_has_durable_intent_and_independent_readback(adapter):
    admin, graphql, ledger, provider, _, candidate, _ = ready(adapter, webhook=True)
    before = len(provider.requests)
    row = admin.enable_webhook("enable", candidate, "authored-endpoint")
    assert row["state"] == "confirmed" and provider.webhooks[candidate]["enabled"] is True
    actual = len(provider.requests)
    admin.enable_webhook("enable", candidate, "authored-endpoint")
    assert actual > before and len(provider.requests) == actual
    with pytest.raises(DriverRefused):
        graphql.request(
            "webhook_update",
            {"id": candidate, "input": {"enabled": True}},
            effect=("ordinary", "enable"),
        )
    assert len(provider.requests) == actual


def test_a20_a24_cleanup_has_fresh_readback_and_authenticated_absence(adapter):
    admin, graphql, ledger, provider, _, candidate, _ = ready(adapter, webhook=True)
    admin.cleanup("disable", candidate, "disable")
    assert ledger.resource(candidate)["state"] == "disabled"
    admin.cleanup("delete", candidate, "delete")
    assert candidate not in provider.webhooks and ledger.resource(candidate)["state"] == "deleted"
    assert operations(provider).count(OPERATIONS["webhook_ids"]) == 1
    admin.cleanup("archive", ledger.scope.project, "archive")
    assert ledger.resource(ledger.scope.project)["state"] == "archived"
    assert "projectDelete" not in " ".join(operations(provider))


@pytest.mark.parametrize("kind", ["partial", "wrong_identity", "graphql_error"])
def test_a25_absence_failure_never_confirms_deletion(adapter, kind):
    admin, graphql, ledger, provider, _, candidate, _ = ready(adapter, webhook=True)
    admin.cleanup("disable", candidate, "disable")

    def change(query, variables, result):
        if query == OPERATIONS["webhook_ids"]:
            if kind == "partial":
                result["webhooks"]["pageInfo"] = {"hasNextPage": True, "endCursor": None}
            if kind == "wrong_identity":
                result["viewer"]["id"] = str(uuid4())
            if kind == "graphql_error":
                return {"webhooks": None}
        return result

    provider.transform = change
    with pytest.raises(DriverRefused, match="unconfirmed"):
        admin.cleanup("delete", candidate, "delete")
    assert (
        ledger.cleanup_action("delete")["state"] == "uncertain"
        and ledger.resource(candidate)["state"] == "disabled"
    )


def test_a27_pending_ordinary_effect_blocks_cleanup(adapter):
    admin, graphql, ledger, provider, _, candidate, _ = ready(adapter, webhook=True)
    graphql.actions.begin(
        "enable", candidate, "enable_webhook", {"id": candidate, "input": {"enabled": True}}
    )
    before = len(provider.requests)
    with pytest.raises(DriverRefused, match="unresolved_ordinary"):
        admin.cleanup("disable", candidate, "disable")
    assert len(provider.requests) == before


def test_a28_reopen_preserves_actions_and_counts(adapter):
    admin, graphql, ledger, provider, _, candidate, _ = ready(adapter, webhook=True)
    admin.enable_webhook("enable", candidate, "authored-endpoint")
    before = ledger.summary()
    reopened = RunLedger(
        ledger.path, ledger.run_id, ledger.scope, ledger.limits, clock=ledger.clock
    )
    assert reopened.summary() == before
    assert (
        FixtureGraphQL(
            reopened, graphql.client.transport, lambda: Credentials("AUTHORED_NOT_LIVE")
        ).actions.get("enable")["state"]
        == "confirmed"
    )


def test_a38_arbitrary_or_changed_effect_reference_cannot_delegate(adapter):
    _, graphql, ledger, provider, *_ = ready(adapter)
    before = len(provider.requests)
    with pytest.raises(DriverRefused):
        graphql.request(
            "project_create", {"input": {"id": str(uuid4())}}, effect=("create", "missing")
        )
    with pytest.raises(DriverRefused):
        graphql.request("project_create", {"input": {"id": str(uuid4())}})
    assert len(provider.requests) == before


@pytest.mark.parametrize("mode", ["complete", "missing_cursor", "duplicate"])
def test_a09_project_team_pagination_is_complete_and_bounded(adapter, mode):
    admin, graphql, ledger, provider, *_ = adapter
    graphql.preflight()

    def change(query, variables, result):
        if query == OPERATIONS["project_metadata"]:
            connection = result["project"]["teams"]
            if variables["after"] is None:
                if mode != "duplicate":
                    connection["nodes"] = []
                connection["pageInfo"] = {
                    "hasNextPage": True,
                    "endCursor": None if mode == "missing_cursor" else "second",
                }
        return result

    provider.transform = change
    if mode == "complete":
        admin.create_project("project")
        assert ledger.get("project")["state"] == "confirmed"
    else:
        with pytest.raises(DriverRefused, match="unconfirmed"):
            admin.create_project("project")
        assert ledger.get("project")["state"] == "uncertain"
    assert len(provider.requests) <= 6


def test_a19_concurrent_enable_sends_one_mutation(adapter):
    admin, graphql, _, provider, _, candidate, _ = ready(adapter, webhook=True)
    barrier = threading.Barrier(2)

    def enable(_):
        barrier.wait(2)
        try:
            return admin.enable_webhook("enable", candidate, "authored-endpoint")
        except DriverRefused:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(enable, range(2)))
    assert operations(provider).count(OPERATIONS["webhook_update"]) == 1
    assert graphql.actions.get("enable")["state"] == "confirmed"


def test_a21_lost_cleanup_reply_has_fresh_recovery_without_resend(adapter):
    admin, graphql, ledger, provider, _, candidate, _ = ready(adapter, webhook=True)
    admin.cleanup("disable", candidate, "disable")
    underlying = graphql.client.transport

    class LoseReply:
        def send(self, query, variables, credentials):
            response = underlying.send(query, variables, credentials)
            if query == OPERATIONS["webhook_delete"]:
                raise LinearError("authored_lost_local_reply", uncertain=True)
            return response

    graphql.client.transport = LoseReply()
    with pytest.raises(DriverRefused, match="unconfirmed"):
        admin.cleanup("delete", candidate, "delete")
    assert (
        ledger.cleanup_action("delete")["state"] == "uncertain"
        and candidate not in provider.webhooks
    )
    admin.recover_cleanup("delete")
    assert ledger.resource(candidate)["state"] == "deleted"
    assert operations(provider).count(OPERATIONS["webhook_delete"]) == 1


def test_a22_a23_explicit_limits_keep_reserved_cleanup_after_ordinary_exhaustion(adapter):
    admin, _, ledger, provider, _, candidate, _ = ready(adapter, webhook=True)
    assert (
        ledger.limits.requests,
        ledger.limits.cleanup_requests,
        ledger.limits.cleanup_seconds,
    ) == (150, 48, 180)
    while sum(row["cleanup_key"] is None for row in ledger.summary()["requests"]) < 102:
        ledger.request("product_transport")
    with pytest.raises(DriverRefused, match="request_budget"):
        ledger.request("read")
    admin.cleanup("disable", candidate, "disable")
    admin.cleanup("delete", candidate, "delete")
    admin.cleanup("archive", ledger.scope.project, "archive")
    rows = ledger.summary()["requests"]
    assert sum(row["cleanup_key"] is None for row in rows) == 102
    assert 0 < sum(row["cleanup_key"] is not None for row in rows) <= 48 and len(rows) <= 150
    assert ledger.resource(ledger.scope.project)["state"] == "archived"


def test_a19_ordinary_deadline_refuses_new_enable_before_http(adapter):
    admin, _, ledger, provider, _, candidate, now = ready(adapter, webhook=True)
    now[0] = ledger.limits.deadline - ledger.limits.cleanup_seconds
    before = len(provider.requests)
    with pytest.raises(DriverRefused, match="ordinary_deadline"):
        admin.enable_webhook("enable", candidate, "authored-endpoint")
    assert len(provider.requests) == before


def test_a27_archive_requires_actual_runtime_port_and_closed_children(adapter):
    admin, _, ledger, provider, _, candidate, _ = ready(adapter, webhook=True)
    before = len(provider.requests)
    admin.quiescence = None
    with pytest.raises(DriverRefused, match="quiescence"):
        admin.cleanup("archive", ledger.scope.project, "archive")
    assert len(provider.requests) == before
    admin.quiescence = lambda: None  # Authored non-running fixture, no live readiness claim.
    with pytest.raises(DriverRefused, match="children"):
        admin.cleanup("archive", ledger.scope.project, "archive")
    assert len(provider.requests) == before


def test_a12_possible_create_can_use_only_its_reserved_recovery_reads(adapter):
    admin, graphql, ledger, provider, *_ = adapter
    graphql.preflight()
    underlying = graphql.client.transport

    class LoseReply:
        def send(self, query, variables, credentials):
            response = underlying.send(query, variables, credentials)
            if query == OPERATIONS["project_create"]:
                raise LinearError("authored_reply_loss", uncertain=True)
            return response

    graphql.client.transport = LoseReply()
    with pytest.raises(DriverRefused):
        admin.create_project("project")
    while sum(row["cleanup_key"] is None for row in ledger.summary()["requests"]) < 102:
        ledger.request("product_transport")
    before = len(provider.requests)
    with pytest.raises(DriverRefused, match="context"):
        graphql.request("project_marker", {"id": str(uuid4())}, recovery_key="project")
    assert len(provider.requests) == before
    admin.recover_create("project")
    assert ledger.get("project")["state"] == "confirmed"
    assert operations(provider).count(OPERATIONS["project_create"]) == 1
    assert sum(row["cleanup_key"] is not None for row in ledger.summary()["requests"]) == 2


def test_a15_receiver_reservation_precedes_hook_without_an_api_effect(adapter):
    admin, graphql, _, provider, _, candidate, _ = ready(adapter)
    before = len(provider.requests)
    with pytest.raises(DriverRefused, match="reservation"):
        admin.create_webhook("hook", candidate, "authored-endpoint", lambda: "x" * 32)
    graphql.actions.reserve_receiver(candidate)
    assert (
        graphql.actions.receiver_candidate(candidate) == candidate
        and len(provider.requests) == before
    )
    with pytest.raises(DriverRefused, match="already_reserved"):
        graphql.actions.reserve_receiver(str(uuid4()))
    assert not provider.webhooks


def test_a18_a41_uncertain_enable_recovers_after_ordinary_and_endpoint_expiry(adapter):
    admin, graphql, ledger, provider, endpoint, candidate, now = ready(adapter, webhook=True)
    underlying = graphql.client.transport

    class LoseReply:
        def send(self, query, variables, credentials):
            response = underlying.send(query, variables, credentials)
            if query == OPERATIONS["webhook_update"] and variables["input"]["enabled"]:
                raise LinearError("authored_lost_local_reply", uncertain=True)
            return response

    graphql.client.transport = LoseReply()
    with pytest.raises(DriverRefused, match="unconfirmed"):
        admin.enable_webhook("enable", candidate, "authored-endpoint")
    assert (
        graphql.actions.get("enable")["state"] == "uncertain"
        and provider.webhooks[candidate]["enabled"]
    )
    now[0] = ledger.limits.deadline - ledger.limits.cleanup_seconds
    endpoint.observed = replace(endpoint.observed, expires_at=now[0] - 1)
    graphql.refresh_if_needed(action_key="enable")
    before = len(provider.requests)
    with pytest.raises(DriverRefused, match="context"):
        graphql.request("webhook_marker", {"id": str(uuid4())}, action_key="enable")
    assert len(provider.requests) == before
    admin.recover_enable("enable")
    assert graphql.actions.get("enable")["state"] == "confirmed"
    assert (
        sum(
            request["query"] == OPERATIONS["webhook_update"]
            and request["variables"]["input"]["enabled"]
            for request, _ in provider.requests
        )
        == 1
    )
    admin.cleanup("disable", candidate, "disable")
    assert ledger.resource(candidate)["state"] == "disabled"


def test_a04_actual_http_response_size_bound_refuses_before_mutation(adapter):
    _, graphql, ledger, provider, *_ = adapter
    request_bytes = len(json.dumps({"query": SHAPE, "variables": {}}, ensure_ascii=False).encode())
    response_limit = request_bytes + 128
    assert request_bytes < response_limit
    graphql.client.transport = HttpTransport(
        timeout=2, max_response_bytes=response_limit, _test_endpoint=provider.endpoint
    )
    provider.fail_next = (200, b"x" * (response_limit + 1))
    with pytest.raises(LinearError, match="response_too_large"):
        graphql.preflight()
    assert len(ledger.summary()["requests"]) == 1 and not provider.projects


def test_a35_development_project_scope_cannot_open_a_fixture_store(tmp_path):
    os.chmod(tmp_path, 0o700)
    scope = Scope(
        EXPECTED.workspace_id,
        EXPECTED.viewer_id,
        EXPECTED.team_id,
        EXPECTED.project_id,
        EXPECTED.project_id,
    )
    now = time.time()
    with pytest.raises(DriverRefused, match="development_project"):
        RunLedger(
            tmp_path / "forbidden.sqlite", str(uuid4()), scope, adapter_limits_v1(now, now + 1200)
        )
    assert not (tmp_path / "forbidden.sqlite").exists()


def test_a43_unsent_action_can_be_abandoned_without_changing_counters(adapter):
    admin, graphql, ledger, provider, _, candidate, _ = ready(adapter, webhook=True)
    graphql.actions.begin(
        "enable", candidate, "enable_webhook", {"id": candidate, "input": {"enabled": True}}
    )
    before = len(provider.requests), len(ledger.summary()["requests"])
    graphql.actions.abandon("enable")
    replay = admin.enable_webhook("enable", candidate, "missing-expired-reference")
    assert (
        replay["state"] == "abandoned"
        and (len(provider.requests), len(ledger.summary()["requests"])) == before
    )
    admin.cleanup("disable", candidate, "disable")
    admin.cleanup("delete", candidate, "delete")
    assert ledger.resource(candidate)["state"] == "deleted"


def test_a44_abandoned_create_keeps_candidate_budget_and_allows_owned_teardown(adapter):
    admin, _, ledger, provider, *_ = ready(adapter)
    candidate = str(uuid4())
    ledger.begin("staged-hook", "webhook", candidate, "f" * 64)
    before = len(provider.requests), len(ledger.summary()["requests"])
    ledger.abandon_create("staged-hook")
    assert ledger.get("staged-hook")["state"] == "abandoned"
    with pytest.raises(DriverRefused, match="resource_budget"):
        ledger.begin("new-hook", "webhook", str(uuid4()), "f" * 64)
    assert (len(provider.requests), len(ledger.summary()["requests"])) == before
    admin.cleanup("archive", ledger.scope.project, "archive")
    assert ledger.resource(ledger.scope.project)["state"] == "archived"


def test_a43_a44_possible_effect_cannot_be_abandoned(adapter):
    _, graphql, ledger, _, _, candidate, _ = ready(adapter, webhook=True)
    graphql.actions.begin(
        "enable", candidate, "enable_webhook", {"id": candidate, "input": {"enabled": True}}
    )
    graphql.actions.sent("enable")
    with pytest.raises(DriverRefused, match="only_unsent"):
        graphql.actions.abandon("enable")
    assert graphql.actions.get("enable")["state"] == "sent"
    with pytest.raises(DriverRefused, match="only_unsent"):
        ledger.abandon_create("hook")
    assert ledger.get("hook")["state"] == "confirmed"


def test_a45_actual_http_uses_remaining_configured_timeout_without_shared_mutation(
    adapter, monkeypatch
):
    _, _, ledger, provider, _, _, now = adapter
    raw = HttpTransport(timeout=30, _test_endpoint=provider.endpoint)
    transport = FixtureRequestTransport(ledger, raw)
    original, seen = HttpTransport.send, []

    def observe(instance, query, variables, credentials):
        seen.append(instance.timeout)
        return original(instance, query, variables, credentials)

    monkeypatch.setattr(HttpTransport, "send", observe)
    ledger.request("read")
    transport.send(
        OPERATIONS["identity"], {"team": ledger.scope.team}, Credentials("AUTHORED_NOT_LIVE")
    )
    ledger.request("read")  # Reserve the second transport probe before ordinary closure.
    now[0] = ledger.limits.deadline - 0.5
    transport.send(
        OPERATIONS["identity"], {"team": ledger.scope.team}, Credentials("AUTHORED_NOT_LIVE")
    )
    assert seen[0] == 3 and 0 < seen[1] <= 0.5 and raw.timeout == 30
    before = len(provider.requests)
    now[0] = ledger.limits.deadline
    with pytest.raises(DriverRefused, match="deadline"):
        transport.send(
            OPERATIONS["identity"], {"team": ledger.scope.team}, Credentials("AUTHORED_NOT_LIVE")
        )
    assert len(provider.requests) == before
