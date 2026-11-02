"""Actual synthetic terminal acceptance; not human or model acceptance."""

import hashlib
import os
import pty
import select
from dataclasses import asdict

import pytest

from ai_task_manager.adapters.hosts.bridge.public_output import TerminalOutput
from ai_task_manager.adapters.hosts.bridge.records import PublicOutput
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.models import RuntimeUncertain


def observed(journal, text="event-1 finished.\n"):
    source = journal._append("trace", {"fixture": "owned final source"})
    return PublicOutput("event-1", "native-session", "turn", "item", text, source)


@pytest.fixture
def terminal(journal):
    master, slave = pty.openpty()
    sink = TerminalOutput(journal, slave)
    try:
        yield master, sink
    finally:
        sink.close()
        os.close(master)
        os.close(slave)


def read_terminal(master):
    if not select.select([master], [], [], 0.2)[0]:
        return b""
    return os.read(master, 524288)


def test_complete_actual_terminal_output_precedes_signed_sink_receipt(journal, terminal):
    master, sink = terminal
    output = observed(journal)
    ref = sink.publish(output)
    visible = read_terminal(master)
    assert b"event-1 finished." in visible
    row = journal._read(ref, "trace")
    assert row["payload"]["output_hash"] == digest(asdict(output))
    assert row["payload"]["native_session"] == output.native_session
    assert row["payload"]["accepted_bytes"] == len(output.text.encode())
    assert row["sources"] == [output.trace_ref]


def test_completed_delivery_replays_without_a_second_actual_write(journal, terminal):
    master, sink = terminal
    output = observed(journal)
    ref = sink.publish(output)
    assert read_terminal(master)
    assert sink.publish(output) == ref
    assert read_terminal(master) == b""


def test_terminal_control_bytes_are_rendered_inertly(journal, terminal):
    master, sink = terminal
    output = observed(journal, "event-1 \x1b[2J\x07\rrewrite\n")
    ref = sink.publish(output)
    visible = read_terminal(master)
    assert b"\x1b" not in visible and b"\x07" not in visible
    assert b"\\x1b[2J\\x07\\x0drewrite" in visible
    data = journal._read(ref)["payload"]
    rendered = b"event-1 \\x1b[2J\\x07\\x0drewrite\n"
    assert data["rendered_sha256"] == hashlib.sha256(rendered).hexdigest()


def test_nonterminal_output_is_not_a_public_terminal(journal, tmp_path):
    with (tmp_path / "ordinary-file").open("wb") as stream:
        with pytest.raises(AccessDenied):
            TerminalOutput(journal, stream.fileno())


def test_uncertain_publication_intent_cannot_print_again(journal, terminal):
    master, sink = terminal
    output = observed(journal)
    journal.intend(
        "public-output:" + digest(asdict(output)), {"output_hash": digest(asdict(output))}
    )
    with pytest.raises(RuntimeUncertain):
        sink.publish(output)
    assert read_terminal(master) == b""


def test_disconnect_retains_intent_without_a_delivery_receipt(journal):
    master, slave = pty.openpty()
    sink = TerminalOutput(journal, slave)
    output = observed(journal)
    os.close(master)
    try:
        with pytest.raises((RuntimeUncertain, AccessDenied)):
            sink.publish(output)
        assert not [
            r for r in journal._history("trace") if r["payload"].get("effect") == "public_output"
        ]
    finally:
        sink.close()
        os.close(slave)


def test_partial_actual_write_never_becomes_complete_or_reprints(journal, terminal, monkeypatch):
    from ai_task_manager.adapters.hosts.bridge import public_output

    master, sink = terminal
    output = observed(journal)
    real_write = os.write
    calls = []

    def interrupted(fd, content):
        calls.append(len(content))
        if len(calls) == 1:
            return real_write(fd, content[:3])
        raise OSError("Synthetic terminal interruption after a partial real write")

    monkeypatch.setattr(public_output.os, "write", interrupted)
    with pytest.raises(RuntimeUncertain, match="incomplete"):
        sink.publish(output)
    assert read_terminal(master) == b"eve"
    assert not [
        r for r in journal._history("trace") if r["payload"].get("effect") == "public_output"
    ]
    monkeypatch.setattr(public_output.os, "write", real_write)
    with pytest.raises(RuntimeUncertain, match="printed twice"):
        sink.publish(output)
    assert read_terminal(master) == b""


def test_owned_native_notification_reaches_actual_terminal_before_user_report(
    native, binding, journal, terminal
):
    from tests.hosts.test_notifications import (
        acknowledged,
        delivered,
        reported,
        setup_observer,
    )

    master, sink = terminal
    _, emit, _, _, observer, value = setup_observer(native, binding, journal)
    observer.sink = sink
    delivered(emit, observer, value)
    acknowledged(emit, value)
    reported(emit)
    reference = observer.publish(value.event_id)
    assert b"event-1 finished; evidence is retained." in read_terminal(master)
    assert journal._read(reference, "notification")["payload"]["stage"] == "user_reported"
