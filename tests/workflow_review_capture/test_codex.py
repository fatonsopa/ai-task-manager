"""Authored app-server processes; no genuine Codex/model acceptance."""

import pytest

from ai_task_manager.adapters.workflow_reviews.capture_store import CaptureStore
from tests.native_installation.test_reviewer_composition import installed as installed
from tests.native_installation.test_reviewer_composition import (
    test_NR01_default_installed_cli_constructs_real_reviewer_and_cold_capture as compose,
)
from tests.native_installation.test_reviewer_composition import (
    test_NR02_reviewer_authority_refuses_before_new_native as refuse,
)
from tests.workflow_review_capture.helpers import reviewer
from tests.workflow_reviews.helpers import ReviewRig


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-6-astra"])
def test_codex_tracked_review_completes_once(tmp_path, model):
    author = ReviewRig(tmp_path / "author")
    producer = None
    try:
        inspection, producer = reviewer(author, tmp_path / "reviewer", host="codex", model=model)
        store = CaptureStore(tmp_path / "captures")
        captured = producer.capture(inspection, store)
        assert captured.host == "codex" and captured.model == model
        assert captured.reviewer_id != inspection.context["author"]["worker_id"]
        assert captured.accepted(("request",), captured.expires_at - 1)
        assert producer.capture(inspection, store) == captured
        assert len(producer.workers.dispatches) == 1
        assert all(n.process._closed for n in producer.workers.hosts.owned.values())
    finally:
        if producer is not None:
            for native in producer.workers.hosts.owned.values():
                if native.process is not None and not native.process._closed:
                    assert native.stop().confirmed
        author.runtime.close()


@pytest.mark.parametrize("fault", ["thread", "turn", "missing", "repeat", "failed", "process"])
def test_codex_decoder_refuses_unmatched_or_incomplete_signed_result(tmp_path, fault):
    from dataclasses import asdict, dataclass
    from types import SimpleNamespace

    from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
    from ai_task_manager.adapters.workflow_reviews.codex_terminal import CodexReviewTerminal
    from ai_task_manager.domains.work.errors import EvidenceRequired

    @dataclass
    class Identity:
        pid: int = 123

    journal = HostJournal(tmp_path / "host.sqlite", tmp_path / "host.key")
    identity = Identity()
    native = SimpleNamespace(
        journal=journal,
        binding=SimpleNamespace(attempt_id="attempt", worker_id="worker", configuration_digest="a"),
        process=SimpleNamespace(identity=identity),
        startup=SimpleNamespace(native_session="thread"),
        session=SimpleNamespace(_active_turn="turn"),
    )
    decoder = CodexReviewTerminal(native)
    output = {
        "method": "item/completed",
        "params": {
            "threadId": "foreign" if fault == "thread" else "thread",
            "turnId": "foreign" if fault == "turn" else "turn",
            "item": {"id": "answer", "type": "agentMessage", "phase": "final_answer", "text": "{}"},
        },
    }
    payload = {
        "attempt_id": "attempt",
        "worker_id": "worker",
        "configuration_digest": "a",
        "process": {"pid": 456} if fault == "process" else asdict(identity),
        "frame": output,
    }
    ref = journal._append("trace", payload)
    terminal = {
        "method": "turn/completed",
        "params": {
            "threadId": "thread",
            "turn": {"id": "turn", "status": "failed" if fault == "failed" else "completed"},
        },
    }
    terminal_ref = journal._append("trace", {**payload, "frame": terminal})
    with pytest.raises(EvidenceRequired):
        if fault != "missing":
            decoder.observe(output, ref)
        if fault == "repeat":
            decoder.observe(output, ref)
        decoder.decode(journal._read(terminal_ref))


@pytest.mark.parametrize("installed", ["gpt-6.1-sol", "gpt-6-astra"], indirect=True)
def test_codex_normal_factory_and_cold_capture(installed, monkeypatch):
    # Existing bounded lookup control, not a complete ordinary owner cycle.
    compose(installed, monkeypatch)


@pytest.mark.parametrize("installed", ["gpt-6.1-sol"], indirect=True)
@pytest.mark.parametrize("fault", ["revoked", "target", "source", "role"])
def test_codex_normal_factory_authority_refusals(installed, fault):
    refuse(installed, fault)


@pytest.mark.parametrize("installed", ["gpt-6.1-sol"], indirect=True)
@pytest.mark.parametrize("change", ["assignee", "hold"])
def test_codex_current_author_change_fences_input(installed, monkeypatch, change):
    from tests.native_installation.test_reviewer_composition import (
        test_NR04_current_author_change_at_model_input_fences_review_and_stops,
    )

    test_NR04_current_author_change_at_model_input_fences_review_and_stops(
        installed, monkeypatch, change
    )
