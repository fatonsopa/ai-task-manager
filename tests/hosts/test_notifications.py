from dataclasses import asdict

import pytest

from ai_task_manager.adapters.hosts.bridge.observations import (
    NotificationObserver,
    NotificationReceipts,
)
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied
from tests.hosts.test_startup import codex_start


class FixtureSink:
    def __init__(self, journal):
        self.journal, self.outputs = journal, []
        self.available = True

    def publish(self, output):
        if not self.available:
            raise AccessDenied("Fixture output surface is disconnected")
        self.outputs.append(output)
        return self.journal._append(
            "trace",
            {
                "output_hash": digest(asdict(output)),
                "native_session": output.native_session,
                "fixture_sink": True,
            },
        )


def setup_observer(native, binding, journal):
    _, transcript, emit, startup = codex_start(native, binding)
    sink = FixtureSink(journal)
    observer = NotificationObserver(transcript, startup, sink)
    value = observer.prepare(
        "event-1", "A fixture worker finished; inspect and report its evidence."
    )
    return transcript, emit, startup, sink, observer, value


def delivered(emit, observer, value, **overrides):
    params = {
        "threadId": "native-thread",
        "turnId": "turn-1",
        "item": {
            "type": "userMessage",
            "id": "input",
            "clientId": value.input_id,
            "content": [{"type": "text", "text": observer.context(value.event_id)}],
        },
    }
    params.update(overrides)
    emit({"method": "item/completed", "params": params})


def acknowledged(emit, value, **overrides):
    params = {
        "threadId": "native-thread",
        "turnId": "turn-1",
        "callId": "call-1",
        "tool": "aitm_acknowledge_event",
        "arguments": {"event_id": value.event_id, "challenge": value.challenge},
    }
    params.update(overrides)
    emit({"id": 91, "method": "item/tool/call", "params": params})


def reported(emit, **overrides):
    params = {
        "threadId": "native-thread",
        "turnId": "turn-1",
        "item": {
            "type": "agentMessage",
            "id": "output",
            "phase": "final_answer",
            "text": "event-1 finished; evidence is retained.",
        },
    }
    params.update(overrides)
    emit({"method": "item/completed", "params": params})


def test_actual_context_ack_and_public_sink_are_distinct_ordered_sources(native, binding, journal):
    _, emit, _, sink, observer, value = setup_observer(native, binding, journal)
    with pytest.raises(AccessDenied):
        observer.reference("event-1", "delivered")
    delivered(emit, observer, value)
    assert observer.reference("event-1", "delivered")
    with pytest.raises(AccessDenied):
        observer.reference("event-1", "ai_acknowledged")
    acknowledged(emit, value)
    with pytest.raises(AccessDenied):
        observer.reference("event-1", "user_reported")
    reported(emit)
    reference = observer.publish("event-1")
    assert observer.publish("event-1") == reference and len(sink.outputs) == 1
    result = NotificationReceipts(journal).verify_notification(reference)
    assert result.stage == "user_reported" and result.session_id == binding.core_session


@pytest.mark.parametrize("kind", ["queued", "cursor", "assistant_text", "wrong_session"])
def test_nonobservations_do_not_imply_delivery(native, binding, journal, kind):
    _, emit, _, _, observer, value = setup_observer(native, binding, journal)
    if kind == "wrong_session":
        delivered(emit, observer, value, threadId="foreign")
    else:
        emit({"method": kind, "params": {"threadId": "native-thread", "event_id": "event-1"}})
    with pytest.raises(AccessDenied):
        observer.reference("event-1", "delivered")


@pytest.mark.parametrize(
    "change", [{"turnId": "foreign"}, {"arguments": {"event_id": "event-1", "challenge": "forged"}}]
)
def test_wrong_challenge_or_turn_cannot_acknowledge(native, binding, journal, change):
    _, emit, _, _, observer, value = setup_observer(native, binding, journal)
    delivered(emit, observer, value)
    with pytest.raises(AccessDenied):
        acknowledged(emit, value, **change)
    with pytest.raises(AccessDenied):
        observer.reference("event-1", "ai_acknowledged")


def test_ack_before_delivery_refuses(native, binding, journal):
    _, emit, _, _, observer, value = setup_observer(native, binding, journal)
    with pytest.raises(AccessDenied):
        acknowledged(emit, value)


def test_disconnected_output_stays_pending_until_real_sink_acceptance(native, binding, journal):
    _, emit, _, sink, observer, value = setup_observer(native, binding, journal)
    delivered(emit, observer, value)
    acknowledged(emit, value)
    reported(emit)
    sink.available = False
    with pytest.raises(AccessDenied):
        observer.publish("event-1")
    with pytest.raises(AccessDenied):
        observer.reference("event-1", "user_reported")
    sink.available = True
    assert observer.publish("event-1")


def test_restart_restores_exact_pending_context_and_completed_receipts(native, binding, journal):
    transcript, emit, startup, sink, observer, value = setup_observer(native, binding, journal)
    delivered(emit, observer, value)
    acknowledged(emit, value)
    restarted = NotificationObserver(transcript, startup, sink)
    assert restarted.prepare(value.event_id, value.text) == value
    assert restarted.reference("event-1", "ai_acknowledged") == observer.reference(
        "event-1", "ai_acknowledged"
    )
    with pytest.raises(AccessDenied):
        restarted.reference("event-1", "user_reported")


def test_restart_restores_owned_final_output_before_pending_publication(native, binding, journal):
    transcript, emit, startup, sink, observer, value = setup_observer(native, binding, journal)
    delivered(emit, observer, value)
    acknowledged(emit, value)
    reported(emit)
    restarted = NotificationObserver(transcript, startup, sink)
    assert restarted.publish(value.event_id)
    assert len(sink.outputs) == 1


@pytest.mark.parametrize("field", ["attempt_id", "worker_id", "configuration_digest", "process"])
def test_restart_ignores_stored_final_frame_from_another_owner(native, binding, journal, field):
    transcript, emit, startup, sink, observer, value = setup_observer(native, binding, journal)
    delivered(emit, observer, value)
    acknowledged(emit, value)
    payload = {
        "attempt_id": binding.attempt_id,
        "worker_id": binding.worker_id,
        "configuration_digest": binding.configuration_digest,
        "process": vars(transcript.process.identity),
        "frame": {
            "method": "item/completed",
            "params": {
                "threadId": "native-thread",
                "turnId": "turn-1",
                "item": {
                    "type": "agentMessage",
                    "id": "foreign",
                    "phase": "final_answer",
                    "text": "event-1 finished; foreign source.",
                },
            },
        },
    }
    payload[field] = {"pid": -1} if field == "process" else "foreign"
    journal._append("trace", payload)
    restarted = NotificationObserver(transcript, startup, sink)
    with pytest.raises(AccessDenied, match="No actual public"):
        restarted.publish(value.event_id)
    assert not sink.outputs
