"""Explicit code-bound callable factories do not acquire a new close protocol."""

from pathlib import Path

import ai_task_manager.installation.native as native
from ai_task_manager.installation.workflows import InstalledWorkflow, NativeRegistry
from tests.native_installation import test_reviewer_composition

installed = test_reviewer_composition.installed


def explicit_owner(s):
    normal = native.resolve(s["config"], s["primary"].installation)
    calls = []

    def factory(inspection):
        calls.append(inspection.digest)
        return normal.reviewer_factory(inspection)

    registry = NativeRegistry(
        tuple(normal.values.values()),
        service_options=normal.service_options,
        reviewer_factory=factory,
    )
    owner = InstalledWorkflow(s["config"], s["primary"].installation, registry)
    return owner, normal.reviewer_factory, calls


def test_RC01_unused_plain_callable_has_no_shutdown_obligation(installed):
    owner, normal, calls = explicit_owner(installed)
    assert not calls and not normal.producers
    assert owner.service.workflows.close() == []
    assert not calls


def test_RC02_explicit_callable_still_constructs_actual_producer(installed):
    s = installed
    owner, normal, calls = explicit_owner(s)
    try:
        captured = owner._review_source(Path(owner.spec.data["state_root"])).resolve(
            s["inspection"]
        )
        assert captured.reviewer_id != s["inspection"].context["author"]["worker_id"]
        assert calls == [s["inspection"].digest]
        assert len(normal.producers) == 1
        assert owner.service.workflows.close() == []
    finally:
        # The explicit injection caller retains its own producer owner as before.
        outcomes = normal.close()
        assert outcomes and all(row["quiescent"] for row in outcomes)
