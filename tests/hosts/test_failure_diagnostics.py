"""Safe causal projection and SDK-shape controls; no native account/model calls."""

import json
from types import SimpleNamespace

import anyio
import pytest

from ai_task_manager.adapters.hosts.bridge.workspace import Workspace
from ai_task_manager.adapters.hosts.transports import claude
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.observability import failures
from ai_task_manager.observability.failures import FailureStage, capture_failure
from tests.hosts.test_claude_control import FixtureClient, result

SECRET = "private-fixture-marker-do-not-copy"


class ForeignFailure(Exception):
    def __getattribute__(self, name):
        raise AssertionError("Arbitrary exception getter")

    def __str__(self):
        raise AssertionError("Arbitrary exception stringification")

    def __repr__(self):
        raise AssertionError("Arbitrary exception repr")


def test_owned_origin_and_code_are_retained_without_message_locals_or_path(tmp_path):
    try:
        Workspace(tmp_path).read("../" + SECRET)
    except AccessDenied as error:
        captured = capture_failure(error, FailureStage.NATIVE_TURN)
    text = json.dumps(captured)
    assert SECRET not in text and str(tmp_path) not in text
    cause = captured["causes"][0]
    assert cause["code"] == "access_denied"
    assert cause["frames"]
    assert all(f["module"].startswith("ai_task_manager.") for f in cause["frames"])
    assert all(len(f["code_sha256"]) == 64 and f["line"] > 0 for f in cause["frames"])
    assert set(cause["frames"][0]) == {"module", "function", "line", "code_sha256"}


def test_foreign_exception_getters_names_and_frames_are_not_formatted():
    error = ForeignFailure(SECRET)
    BaseException.__cause__.__set__(error, ValueError(SECRET))
    data = capture_failure(error, SECRET)
    assert data["stage"] == "unknown"
    assert data["causes"][0]["code"] == "unclassified"
    assert data["causes"][0]["type"] is None
    assert data["causes"][1]["code"] == "value_error"
    assert SECRET not in json.dumps(data)


def test_cyclic_and_oversized_causal_chains_are_bounded():
    error = ValueError(SECRET)
    error.__cause__ = error
    data = capture_failure(error, FailureStage.NATIVE_STOP)
    assert len(data["causes"]) == 1 and data["chain_truncated"]
    error = ValueError(SECRET)
    for _ in range(50):
        outer = RuntimeError(SECRET)
        outer.__cause__ = error
        error = outer
    data = capture_failure(error, FailureStage.NATIVE_STOP)
    assert len(data["causes"]) == failures.MAX_CAUSES and data["chain_truncated"]
    assert SECRET not in json.dumps(data)


def test_suppressed_context_remains_suppressed():
    try:
        try:
            raise ValueError(SECRET)
        except ValueError:
            raise AccessDenied(SECRET) from None
    except AccessDenied as error:
        data = capture_failure(error, FailureStage.NATIVE_TURN)
    assert [x["code"] for x in data["causes"]] == ["access_denied"]


def test_foreign_traceback_walk_is_bounded_and_does_not_copy_its_source_path():
    def recursive(depth):
        if depth:
            return recursive(depth - 1)
        raise RuntimeError(SECRET)

    try:
        recursive(100)
    except RuntimeError as error:
        data = capture_failure(error, FailureStage.NATIVE_TURN)
    assert data["causes"][0]["frames"] == []
    assert data["causes"][0]["traceback_truncated"]
    assert SECRET not in json.dumps(data)


def test_projection_failure_cannot_replace_original_error(monkeypatch, tmp_path):
    def broken(code):
        raise ValueError(SECRET)

    monkeypatch.setattr(failures.marshal, "dumps", broken)
    try:
        Workspace(tmp_path).read("../" + SECRET)
    except AccessDenied as error:
        data = capture_failure(error, FailureStage.NATIVE_TURN)
    assert data["projection_failed"] and SECRET not in json.dumps(data)


class MemoryJournal:
    def __init__(self, fail=False):
        self.records, self.fail = [], fail

    def _append(self, kind, payload):
        if self.fail:
            raise OSError(SECRET)
        self.records.append(payload)
        return "authored-journal-ref"


def session(messages, *, journal=None):
    journal = journal or MemoryJournal()
    value = claude.ClaudeSession(
        None,
        None,
        SimpleNamespace(
            native_session="native-session",
            journal=journal,
            binding=SimpleNamespace(attempt_id="authored-attempt"),
        ),
    )
    value.client = FixtureClient(messages)
    return value, journal


@pytest.mark.parametrize(
    "subtype", ["error_max_turns", "error_max_budget_usd", "untrusted-" + SECRET]
)
def test_terminal_projection_preserves_exposed_codes_and_omits_provider_text(subtype):
    message = result(
        subtype=subtype,
        is_error=True,
        num_turns=4,
        total_cost_usd=0.25,
        errors=[SECRET],
        usage={"input_tokens": 3, "output_tokens": 7, SECRET: SECRET},
        result=SECRET,
        terminal_reason="max_turns",
    )
    current, journal = session([message])
    with pytest.raises(AccessDenied):
        anyio.run(current.turn, SECRET)
    payload = journal.records[-1]
    terminal = payload["terminal"]
    assert terminal["subtype"] == (
        subtype if not subtype.startswith("untrusted") else "unrecognized"
    )
    assert terminal["session_matches"] is True and terminal["num_turns"] == 4
    assert terminal["error_count"] == 1 and terminal["usage"] == {
        "input_tokens": 3,
        "output_tokens": 7,
    }
    assert SECRET not in json.dumps(payload)


def test_absent_result_remains_unknown_and_mismatched_session_is_explicit():
    current, journal = session([object()])
    with pytest.raises(AccessDenied):
        anyio.run(current.turn, SECRET)
    terminal = journal.records[-1]["terminal"]
    assert terminal["result_present"] is False
    assert terminal["subtype"] is None and terminal["usage"] is None
    assert terminal["session_matches"] is None
    current, journal = session([result(session_id=SECRET)])
    with pytest.raises(AccessDenied):
        anyio.run(current.turn, SECRET)
    assert journal.records[-1]["terminal"]["session_matches"] is False
    assert SECRET not in json.dumps(journal.records)


def test_journal_failure_preserves_exact_invocation_error_and_no_durable_claim():
    original = AccessDenied(SECRET)
    current, _ = session([], journal=MemoryJournal(fail=True))

    async def fail(value):
        raise original

    current.client.query = fail
    with pytest.raises(AccessDenied) as caught:
        anyio.run(current.turn, SECRET)
    assert caught.value is original
    diagnostic = current.failure_diagnostics[-1]
    assert diagnostic["retained"] is False and "reference" not in diagnostic
    assert diagnostic["retention_failure"]["causes"][0]["code"] == "os_error"
    assert SECRET not in json.dumps(diagnostic)


def test_diagnostics_are_finite_without_changing_failure_identity():
    original = AccessDenied(SECRET)
    current, journal = session([])

    async def fail(value):
        raise original

    current.client.query = fail
    for _ in range(12):
        with pytest.raises(AccessDenied) as caught:
            anyio.run(current.turn, SECRET)
        assert caught.value is original
    assert len(journal.records) == len(current.failure_diagnostics) == 8
    assert current.diagnostics_truncated


def test_assertion_failure_has_explicit_safe_code_without_its_message():
    captured = capture_failure(AssertionError(SECRET), FailureStage.STARTUP_MONITOR)
    assert captured["causes"][0]["code"] == "assertion_error"
    assert captured["causes"][0]["type"] == "AssertionError"
    assert SECRET not in json.dumps(captured)
