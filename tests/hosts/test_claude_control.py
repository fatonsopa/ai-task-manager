"""SDK message fixtures; these checks make no model or account call."""

from dataclasses import replace
from types import SimpleNamespace

import anyio
import pytest
from claude_agent_sdk import ResultMessage

from ai_task_manager.adapters.hosts.transports.claude import ClaudeSession
from ai_task_manager.domains.work.errors import AccessDenied


def result(**changes):
    value = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="native-session",
        result="READY",
    )
    return replace(value, **changes)


class FixtureClient:
    def __init__(self, messages):
        self.messages = messages
        self.inputs = []

    async def query(self, value):
        self.inputs.append(value)

    async def receive_response(self):
        for message in self.messages:
            yield message


@pytest.mark.parametrize(
    "message",
    [
        result(is_error=True),
        result(subtype="error_during_execution"),
        result(session_id="foreign"),
        SimpleNamespace(is_error=False),
    ],
)
def test_failed_limited_or_unbound_sdk_turn_cannot_claim_success(message):
    session = ClaudeSession(None, None, SimpleNamespace(native_session="native-session"))
    session.client = FixtureClient([message])
    with pytest.raises(AccessDenied):
        anyio.run(session.turn, "neutral fixture")


def test_matching_actual_sdk_message_shape_is_retained():
    expected = result()
    session = ClaudeSession(None, None, SimpleNamespace(native_session="native-session"))
    session.client = FixtureClient([expected])
    assert anyio.run(session.turn, "neutral fixture") is expected
