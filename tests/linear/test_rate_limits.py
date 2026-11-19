"""Offline quota admission at the actual client/transport boundary."""

import io
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.linear.client import LinearClient
from ai_task_manager.adapters.linear.errors import LinearError
from ai_task_manager.adapters.linear.models import Credentials, WireResponse
from ai_task_manager.adapters.linear.rate_limits import IDENTITY, QuotaStore
from ai_task_manager.adapters.linear.transport import HttpTransport
from tests.linear.helpers import EXPECTED


def test_quota_admission_precedes_final_effect_fence():
    transport = HttpTransport()
    effects = []

    @contextmanager
    def admission(*args, **kwargs):
        raise LinearError("rate_limited", retry_after=3600)
        yield

    transport.admission = admission
    transport.send = lambda *args: effects.append("http") or WireResponse(200, {}, b'{"data":{}}')
    client = LinearClient(transport, lambda: Credentials("fixture-secret"), EXPECTED)
    with pytest.raises(LinearError, match="rate_limited"):
        client.request(
            "mutation Example",
            {},
            mutation=True,
            _before_dispatch=lambda: effects.append("effect-fence"),
        )
    assert effects == []


CREDS = Credentials("fixture-secret")


def headers(requests=5, complexity=60000, *, reset=2000000, cost=2, **extra):
    return {
        "x-ratelimit-requests-limit": "5000",
        "x-ratelimit-requests-remaining": str(requests),
        "x-ratelimit-requests-reset": str(reset),
        "x-ratelimit-complexity-limit": "6000000",
        "x-ratelimit-complexity-remaining": str(complexity),
        "x-ratelimit-complexity-reset": str(reset),
        "x-complexity": str(cost),
        **extra,
    }


def wire_response(status=200, **kwargs):
    return WireResponse(
        status,
        headers(**kwargs),
        json.dumps({"data": {"viewer": {"id": EXPECTED.viewer_id}}}).encode(),
    )


def seeded(tmp_path, **kwargs):
    store = QuotaStore(tmp_path / "quota.sqlite3", clock=lambda: 1000)
    ref = store.reserve(IDENTITY, {}, CREDS, principal=EXPECTED.viewer_id)
    store.complete(ref, wire_response(**kwargs))
    return store


def test_account_reservations_are_atomic_shared_and_survive_restart(tmp_path):
    store = seeded(tmp_path, requests=2, complexity=20000)

    def reserve(index):
        other = QuotaStore(store.path, clock=store.clock)
        try:
            return other.reserve(
                "query Bounded", {"id": index}, CREDS, principal=EXPECTED.viewer_id
            )
        except LinearError as error:
            return error.category

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(reserve, range(5)))
    assert results.count("rate_limited") == 3
    for ref in results:
        if ref != "rate_limited":
            store.complete(ref)  # Unknown dispatches stay charged.
    restarted = QuotaStore(store.path, clock=store.clock)
    with pytest.raises(LinearError, match="rate_limited"):
        restarted.require_model(EXPECTED.viewer_id)
    with pytest.raises(LinearError, match="rate_limited"):
        restarted.reserve(
            "mutation Example", {}, Credentials("rotated-token"), principal=EXPECTED.viewer_id
        )


def test_identity_bootstrap_is_only_unknown_admission_and_is_serialized(tmp_path):
    store = QuotaStore(tmp_path / "quota.sqlite3", clock=lambda: 1000)
    with pytest.raises(LinearError, match="quota_identity_required"):
        store.reserve("query Other", {}, CREDS, principal=EXPECTED.viewer_id)
    with pytest.raises(LinearError, match="quota_identity_required"):
        store.require_model(EXPECTED.viewer_id)
    ref = store.reserve(IDENTITY, {}, CREDS, principal=EXPECTED.viewer_id)
    with pytest.raises(LinearError, match="quota_busy"):
        QuotaStore(store.path, clock=store.clock).reserve(IDENTITY, {}, CREDS)
    store.complete(ref, wire_response())
    store.require_model(EXPECTED.viewer_id)
    # Direct transport uses the verified token alias, without per-case account state.
    store.cancel(store.reserve("query Direct", {}, CREDS))


def test_late_headers_and_cancel_cannot_restore_exhausted_quota_or_cooldown(tmp_path):
    store = seeded(tmp_path)
    first = store.reserve("query First", {}, CREDS)
    second = store.reserve("query Second", {}, CREDS)
    third = store.reserve("query Third", {}, CREDS)
    store.complete(second, wire_response(429, requests=0, complexity=0, **{"retry-after": "7200"}))
    store.cancel(third)  # Definitely not dispatched; still cannot override provider zero.
    store.complete(first, wire_response(requests=5000, complexity=6000000, reset=9999999))
    with pytest.raises(LinearError, match="rate_limited") as caught:
        store.require_model(EXPECTED.viewer_id)
    assert caught.value.retry_after == 7200
    with pytest.raises(LinearError, match="rate_limited"):
        store.reserve(IDENTITY, {}, CREDS)


def test_successful_cost_reconciliation_and_endpoint_budget(tmp_path):
    store = seeded(tmp_path, complexity=10002)
    query = "mutation First($input: X!) { issueCreate(input:$input) { success } }"
    ref = store.reserve(query, {"input": {"title": "one"}}, CREDS)
    store.complete(
        ref,
        wire_response(
            complexity=10000,
            cost=2,
            **{
                "x-ratelimit-endpoint-name": "issueCreate",
                "x-ratelimit-endpoint-requests-limit": "100",
                "x-ratelimit-endpoint-requests-remaining": "0",
                "x-ratelimit-endpoint-requests-reset": "2000000",
            },
        ),
    )
    # Endpoint exhaustion does not forecast unrelated native/provider work.
    store.require_model(EXPECTED.viewer_id)
    for candidate in (query, "mutation Variant { alias: issueCreate(input:{}) { success } }"):
        with pytest.raises(LinearError, match="rate_limited"):
            store.reserve(candidate, {"input": {"title": "different"}}, CREDS)
    unrelated = store.reserve('query Metadata { issue(id:"x") { id } }', {}, CREDS)
    store.cancel(unrelated)
    identity = store.reserve(IDENTITY, {}, CREDS)
    store.cancel(identity)


class Reply(io.BytesIO):
    code = 200

    def __init__(self, response):
        super().__init__(response.body)
        self.code, self.headers = response.status, response.headers


class Opener:
    def __init__(self, response):
        self.response, self.calls = response, []

    def open(self, request, timeout):
        self.calls.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return Reply(self.response)


def test_real_transport_blocks_dispatch_and_effect_fence_and_preserves_response(tmp_path):
    store = seeded(tmp_path, requests=1)
    transport = HttpTransport(_quota_store=store)
    result = wire_response(requests=0)
    transport.opener = Opener(result)
    client = LinearClient(transport, lambda: CREDS, EXPECTED)
    effects = []
    assert client.request("query One", {}) == json.loads(result.body)["data"]
    with pytest.raises(LinearError, match="rate_limited"):
        client.request(
            "mutation Two", {}, mutation=True, _before_dispatch=lambda: effects.append("fence")
        )
    assert effects == [] and len(transport.opener.calls) == 1
    with pytest.raises(LinearError, match="rate_limited"):
        transport.send("query Direct", {}, CREDS)
    assert len(transport.opener.calls) == 1


def test_admission_deadline_and_current_credentials_precede_effects(tmp_path):
    store = seeded(tmp_path)
    transport = HttpTransport(_quota_store=store)
    transport.opener = Opener(wire_response())
    with pytest.raises(LinearError, match="quota_admission_deadline"):
        with transport.admission("query One", {}, CREDS, not_after=0):
            pytest.fail("Expired admission must not enter")
    credentials = iter([CREDS, Credentials("changed")])
    client = LinearClient(transport, lambda: next(credentials), EXPECTED)
    with pytest.raises(LinearError, match="credential_binding_changed"):
        client.request(
            "mutation Two",
            {},
            mutation=True,
            _before_dispatch=lambda: pytest.fail("Stale credential reached fence"),
        )
    assert transport.opener.calls == []
    store.require_model(EXPECTED.viewer_id)  # No-dispatch cancellation preserves capacity.


def test_no_response_send_remains_consumed_and_returned_response_wins_accounting_error(tmp_path):
    store = seeded(tmp_path, requests=1)
    transport = HttpTransport(_quota_store=store)
    transport.opener = Opener(OSError("controlled"))
    with pytest.raises(LinearError, match="transport_failure") as caught:
        transport.send("query One", {}, CREDS)
    assert caught.value.uncertain
    with pytest.raises(LinearError, match="rate_limited"):
        store.require_model(EXPECTED.viewer_id)
    store = seeded(tmp_path / "other")
    transport = HttpTransport(_quota_store=store)
    expected = wire_response()
    transport.opener = Opener(expected)
    store.complete = lambda *args: (_ for _ in ()).throw(OSError("controlled accounting failure"))
    assert transport.send("query One", {}, CREDS) == expected
    with pytest.raises(LinearError, match="quota_state_unavailable"):
        store.require_model(EXPECTED.viewer_id)
    with pytest.raises(LinearError, match="quota_state_unavailable"):
        HttpTransport(_quota_store=QuotaStore(store.path)).send("query Again", {}, CREDS)


@pytest.mark.parametrize("kind", ["normal", "probe"])
def test_actual_native_permit_owners_check_quota_before_invocation_claim(monkeypatch, kind):
    def exhausted(scope):
        assert scope.principal == EXPECTED.viewer_id
        raise LinearError("rate_limited")

    scope = SimpleNamespace(provider="linear", principal=EXPECTED.viewer_id)
    binding = SimpleNamespace(scope=scope)
    if kind == "normal":
        from ai_task_manager.installation.native import admissions

        monkeypatch.setattr(admissions, "require_model", exhausted)
        owner = admissions.NormalAdmissions.__new__(admissions.NormalAdmissions)
        owner.check_admission = lambda plan: None
        with pytest.raises(LinearError, match="rate_limited"):
            owner.verify_policy(None, binding, None, None)
    else:
        from ai_task_manager.installation.native import probe_permits

        monkeypatch.setattr(probe_permits, "require_model", exhausted)
        owner = probe_permits.ProbePermits.__new__(probe_permits.ProbePermits)
        owner.binding, owner.policy, owner.context = binding, None, None
        owner.authority = SimpleNamespace(require_current=lambda *a, **k: None)
        with pytest.raises(LinearError, match="rate_limited"):
            owner._claim("neutral")


def test_initial_identity_429_without_viewer_retains_cooldown_across_tokens(tmp_path):
    store = QuotaStore(tmp_path / "quota.sqlite3", clock=lambda: 1000)
    ref = store.reserve(IDENTITY, {}, CREDS)
    store.complete(ref, WireResponse(429, {"retry-after": "7200"}, b'{"errors":[]}'))
    with pytest.raises(LinearError, match="rate_limited"):
        QuotaStore(store.path, clock=store.clock).reserve(
            IDENTITY, {}, Credentials("different-token"), principal=EXPECTED.viewer_id
        )


def test_only_identity_after_retained_reset_can_establish_new_window(tmp_path):
    store = seeded(tmp_path, requests=1)
    ref = store.reserve("query One", {}, CREDS)
    store.complete(ref, wire_response(requests=0))
    store.clock = lambda: 2001
    with pytest.raises(LinearError, match="rate_limited"):
        store.reserve("query Other", {}, CREDS)
    with pytest.raises(LinearError, match="rate_limited"):
        store.require_model(EXPECTED.viewer_id)
    identity = store.reserve(IDENTITY, {}, CREDS)
    store.complete(identity, wire_response(requests=10, reset=3000000))
    store.require_model(EXPECTED.viewer_id)


def test_unknown_rotated_token_identity_cannot_reset_known_user_budget(tmp_path):
    store = seeded(tmp_path, requests=1)
    ref = store.reserve("query One", {}, CREDS)
    store.complete(ref, wire_response(requests=0))
    # An unbound direct transport can only learn identity; it cannot create a new budget.
    other = store.reserve(IDENTITY, {}, Credentials("new-unbound-token"))
    store.complete(other, wire_response(requests=5000, complexity=6000000, reset=3000000))
    with pytest.raises(LinearError, match="rate_limited"):
        store.require_model(EXPECTED.viewer_id)
    with pytest.raises(LinearError, match="rate_limited"):
        store.reserve("mutation Example", {}, Credentials("new-unbound-token"))


@pytest.mark.parametrize("status", [200, 429])
def test_bootstrap_refusal_retains_reset_without_viewer_or_retry_after(tmp_path, status):
    store = QuotaStore(tmp_path / "quota.sqlite3", clock=lambda: 1000)
    ref = store.reserve(IDENTITY, {}, CREDS, principal=EXPECTED.viewer_id)
    body = b'{"errors":[{"extensions":{"code":"RATELIMITED"}}]}'
    store.complete(ref, WireResponse(status, headers(requests=0), body))
    with pytest.raises(LinearError, match="rate_limited"):
        store.reserve(IDENTITY, {}, CREDS, principal=EXPECTED.viewer_id)
    store.clock = lambda: 2001
    ref = store.reserve(IDENTITY, {}, CREDS, principal=EXPECTED.viewer_id)
    store.complete(ref, wire_response(reset=3000000))
    store.require_model(EXPECTED.viewer_id)


def test_default_store_uses_os_user_home_and_non_linear_models_are_unaffected(monkeypatch):
    from ai_task_manager.adapters.linear import rate_limits

    original = QuotaStore().path
    monkeypatch.setenv("HOME", "/temporary/native-case-home")
    assert QuotaStore().path == original
    monkeypatch.setattr(rate_limits, "QuotaStore", lambda: pytest.fail("No Linear consumer"))
    rate_limits.require_model(SimpleNamespace(provider="memory", principal="unrelated"))


@pytest.mark.parametrize("kind", ["fixture", "owned", "budgeted"])
def test_selected_live_wrappers_keep_one_admitted_transport_before_fence(
    tmp_path, monkeypatch, kind
):
    import time

    from ai_task_manager.adapters.linear import transport as transport_module
    from tests.linear.live.fixture_http import FixtureRequestTransport
    from tests.linear.live.primary_driver import OwnedProductTransport
    from tests.linear.live.run_primary import BudgetedTransport

    store = seeded(tmp_path, requests=1)
    opener = Opener(wire_response(requests=0))
    monkeypatch.setattr(transport_module, "build_opener", lambda *args: opener)
    source = HttpTransport(_quota_store=store)
    effects = []
    ledger = SimpleNamespace(
        request_deadline=lambda: time.time() + 5,
        clock=time.time,
        request=lambda *args: effects.append("ledger-send"),
    )
    if kind == "fixture":
        wrapped = FixtureRequestTransport(ledger, source)
    elif kind == "owned":
        wrapped = OwnedProductTransport(ledger, source)
    else:
        wrapped = BudgetedTransport(ledger, FixtureRequestTransport(ledger, source))
    client = LinearClient(wrapped, lambda: CREDS, EXPECTED)
    assert client.request("query One", {})
    assert len(opener.calls) == 1  # A second reservation would have blocked this only slot.
    previous = list(effects)
    with pytest.raises(LinearError, match="rate_limited"):
        client.request(
            "mutation Example",
            {},
            mutation=True,
            _before_dispatch=lambda: effects.append("final-fence"),
        )
    assert effects == previous and len(opener.calls) == 1


def test_unverified_token_cannot_borrow_known_users_budget_for_read_or_model(tmp_path):
    store = seeded(tmp_path)
    token = Credentials("new-unverified-token")
    with pytest.raises(LinearError, match="quota_identity_required"):
        store.reserve("query Metadata", {}, token, principal=EXPECTED.viewer_id)
    identity = store.reserve(IDENTITY, {}, token, principal=EXPECTED.viewer_id)
    store.complete(identity, wire_response())
    store.cancel(store.reserve("query Metadata", {}, token))


def test_global_identity_renewal_preserves_other_endpoint_exhaustion(tmp_path):
    store = seeded(tmp_path)
    query = "mutation A { issueCreate(input:{}) { success } }"
    ref = store.reserve(query, {}, CREDS)
    store.complete(
        ref,
        wire_response(
            requests=0,
            **{
                "x-ratelimit-endpoint-name": "issueCreate",
                "x-ratelimit-endpoint-requests-limit": "100",
                "x-ratelimit-endpoint-requests-remaining": "0",
                "x-ratelimit-endpoint-requests-reset": "9000000",
            },
        ),
    )
    store.clock = lambda: 2001
    identity = store.reserve(IDENTITY, {}, CREDS)
    store.complete(identity, wire_response(requests=10, reset=3000000))
    store.require_model(EXPECTED.viewer_id)
    with pytest.raises(LinearError, match="rate_limited"):
        store.reserve(query, {}, CREDS)
