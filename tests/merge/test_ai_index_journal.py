from dataclasses import asdict

import pytest

from ai_task_manager.adapters.projects.ai_index.callbacks import Callbacks
from ai_task_manager.adapters.projects.ai_index.journal import Journal
from ai_task_manager.domains.work.errors import (
    AccessDenied,
    InvalidTransition,
    NotFound,
    StaleVersion,
)

STAGES = ["after_state", "after_event", "after_callbacks", "before_commit", "after_commit"]


@pytest.mark.parametrize("stage", STAGES, ids=STAGES)
def test_intent_event_outbox_atomicity(harness, stage):
    harness.publish()
    prior, events = harness.current, harness.journal.events(harness.ref)
    facts = dict(prior["facts"], marker="committed")
    cb = asdict(harness.callback())
    harness.fault.stage = stage
    with pytest.raises(RuntimeError, match="Injected"):
        harness.journal.commit(
            harness.ref,
            prior["version"],
            "atomic",
            "atomic_fact",
            facts,
            {"marker": "committed"},
            (cb,),
        )
    harness.fault.stage = None
    reopened = Journal(harness.journal.path)
    if stage == "after_commit":
        assert reopened.current(harness.ref)["facts"]["marker"] == "committed"
        assert reopened.callback(harness.ref, 1)["state"] == "queued"
        receipt = reopened.commit(
            harness.ref,
            prior["version"],
            "atomic",
            "atomic_fact",
            facts,
            {"marker": "committed"},
            (cb,),
        )
        assert receipt["version"] == prior["version"] + 1
        assert len(reopened.events(harness.ref)) == len(events) + 1
    else:
        assert reopened.current(harness.ref) == prior
        assert reopened.events(harness.ref) == events
        with pytest.raises(NotFound):
            reopened.callback(harness.ref, 1)


VARIANTS = [
    "send_uncertain",
    "application_denied",
    "provider_unconfirmed",
    "provider_confirmed",
    "restart",
    "foreign_receipt_after_send",
    "effect_then_access_denied",
]


@pytest.mark.parametrize("variant", VARIANTS, ids=VARIANTS)
def test_pending_failed_and_uncertain_settlement(harness, variant):
    harness.publish()
    harness.queue()
    if variant in {"send_uncertain", "restart"}:
        harness.downstream.mode = "uncertain"
    elif variant == "effect_then_access_denied":
        harness.downstream.mode = "effect_then_access_denied"
    elif variant == "application_denied":
        harness.downstream.mode = "deny"
    elif variant == "foreign_receipt_after_send":
        harness.downstream.mode = "foreign_receipt"
    intent = harness.callbacks.dispatch(harness.ref, 1)
    state = harness.journal.callback(harness.ref, 1)["state"]
    if variant in {"foreign_receipt_after_send", "effect_then_access_denied"}:
        assert state == "uncertain"
        assert len(harness.downstream.effects) == 1
    elif variant == "application_denied":
        assert state == "denied"
    elif variant in {"send_uncertain", "restart"}:
        assert state == "uncertain"
        if variant == "restart":
            callbacks = Callbacks(
                Journal(harness.journal.path), harness.downstream, harness.observations
            )
            assert callbacks.dispatch(harness.ref, 1) == intent
            assert len(harness.downstream.calls) == 1
        recovered = next(iter(harness.observations.receipts))
        harness.callbacks.settle(harness.ref, 1, recovered)
        assert harness.journal.callback(harness.ref, 1)["state"] == "accepted_intent"
    else:
        assert state == "accepted_intent"
        applied = harness.receipt("applied")
        old_receipt = harness.callbacks.settle(harness.ref, 1, applied)
        if variant == "provider_confirmed":
            for stage in ["provider_confirmed", "lead_delivered", "ai_ack", "user_reported"]:
                harness.callbacks.settle(harness.ref, 1, harness.receipt(stage))
            assert harness.journal.callback(harness.ref, 1)["state"] == "user_reported"
            assert harness.callbacks.settle(harness.ref, 1, applied) == old_receipt
        else:
            assert harness.journal.callback(harness.ref, 1)["state"] == "applied"
    assert harness.callbacks.dispatch(harness.ref, 1) == intent
    assert len(harness.downstream.calls) == 1


VARIANTS = ["exact", "advanced_head", "ambiguous", "duplicate"]


@pytest.mark.parametrize("variant", VARIANTS, ids=VARIANTS)
def test_committed_publication_lost_reply_recovery(harness, variant):
    harness.launch()
    harness.prepare()
    payload = {
        "old": "a" * 40,
        "new": "b" * 40,
        "checked": "b" * 40,
        "range_ref": "range",
        "confirmed": variant != "ambiguous",
    }
    if variant == "advanced_head":
        payload["current_dev"] = "f" * 40
    evidence = harness.fact("publication", payload, "publication-proof")
    version = harness.version()
    harness.fault.stage = "after_commit"
    with pytest.raises(RuntimeError):
        harness.operations.observe(harness.ref, version, "publish", evidence)
    harness.fault.stage = None
    receipt = harness.operations.observe(harness.ref, version, "publish", evidence)
    assert receipt["version"] == version + 1
    if variant == "ambiguous":
        assert "publication" not in harness.current["facts"]
        assert harness.current["facts"]["publication_uncertain"] is True
    else:
        assert harness.current["facts"]["publication"]["new"] == "b" * 40
    if variant == "duplicate":
        harness.operations.model_ended(harness.ref, harness.version(), "ended", "eof")
        assert harness.operations.observe(harness.ref, version, "publish", evidence) == receipt
    with pytest.raises(AccessDenied):
        with harness.operations.authority_boundary(harness.ref):
            raise AssertionError("Known/uncertain publication cannot be entered again")
    assert harness.registrations.register(harness.binding) == harness.registration
    assert (
        len([e for e in harness.journal.events(harness.ref) if e["kind"] == "launch_intent"]) == 1
    )


VARIANTS = [
    "registration",
    "launch_intent",
    "launch_observation",
    "publication_intent",
    "publication_observation",
    "callback_persistence",
    "quiescence",
    "launch_before_intent",
    "publication_before_launch",
    "callback_before_publication",
    "settle_before_dispatch",
    "provider_before_application",
    "cleanup_warning",
    "source_skip",
]


@pytest.mark.parametrize("variant", VARIANTS, ids=VARIANTS)
def test_required_fact_omission_and_order(harness, variant):
    if variant == "registration":
        with pytest.raises(NotFound):
            harness.operations.intend_launch("unknown", 0, "start")
    elif variant in {"launch_intent", "launch_before_intent"}:
        with pytest.raises(InvalidTransition):
            harness.observe("launched", {"process_ref": "p", "children": []})
    elif variant in {"launch_observation", "publication_before_launch"}:
        with pytest.raises(InvalidTransition):
            harness.prepare()
    elif variant == "publication_intent":
        harness.launch()
        with pytest.raises(AccessDenied):
            harness.observe(
                "publication",
                {
                    "old": "a" * 40,
                    "new": "b" * 40,
                    "checked": "b" * 40,
                    "range_ref": "range",
                    "confirmed": True,
                },
            )
    elif variant == "publication_observation":
        harness.launch()
        harness.prepare()
        with pytest.raises(InvalidTransition):
            harness.observe("result", {"answer": "MERGED"})
    elif variant in {"callback_persistence", "callback_before_publication"}:
        if variant == "callback_persistence":
            with pytest.raises(NotFound):
                harness.callbacks.dispatch(harness.ref, 1)
        else:
            with pytest.raises(AccessDenied):
                harness.callbacks.submit(harness.callback(), harness.version())
    elif variant == "quiescence":
        harness.publish()
        harness.process("unknown")
        assert harness.operations.reconcile_stopped("attempt") is False
        assert "quiescence_evidence" not in harness.current["facts"]
    elif variant in {"settle_before_dispatch", "provider_before_application"}:
        harness.publish()
        harness.queue()
        if variant == "provider_before_application":
            harness.callbacks.dispatch(harness.ref, 1)
        ref = harness.receipt(
            "provider_confirmed" if variant == "provider_before_application" else "applied"
        )
        with pytest.raises(StaleVersion):
            harness.callbacks.settle(harness.ref, 1, ref)
    else:
        harness.publish("source_skip" if variant == "source_skip" else "pass")
        harness.observe("database", {"state": "warning"})
        harness.observe("result", {"answer": "MERGED"})
        assert harness.current["facts"]["result"] == "MERGED"
        assert harness.current["facts"]["heavy_obligation"] == "pending"
