"""The historical migration actor retains its genuine schema3 positive work fence."""

import hashlib
from types import SimpleNamespace

import pytest

from ai_task_manager.domains.work.errors import InvalidTransition, SchemaError
from ai_task_manager.domains.work.models import AttemptState
from ai_task_manager.storage import issue_claims
from tests.scheduling.helpers import (
    LEGACY_CANONICAL_SHA256,
    LEGACY_CANONICAL_SOURCE,
    LEGACY_EXECUTION_OPEN_SHA256,
    LEGACY_EXECUTION_OPEN_SOURCE,
    legacy_execution,
)
from tests.scheduling.test_migration import legacy_store


def test_frozen_own_product_guard_source_hashes_match():
    assert hashlib.sha256(LEGACY_CANONICAL_SOURCE.encode()).hexdigest() == LEGACY_CANONICAL_SHA256
    assert (
        hashlib.sha256(LEGACY_EXECUTION_OPEN_SOURCE.encode()).hexdigest()
        == LEGACY_EXECUTION_OPEN_SHA256
    )


@pytest.mark.parametrize("state", ["delivered", "terminal", "corrupt"])
def test_frozen_guard_retains_positive_delivery_terminal_and_corruption_refusals(tmp_path, state):
    old, handle, task, attempt = legacy_store(tmp_path / "core.sqlite", AttemptState.RUNNING)
    execution = legacy_execution(SimpleNamespace(db=old))
    assert execution.validate_work_lease(handle, attempt.id, attempt.version) == attempt
    with old.transaction() as conn:
        from ai_task_manager.storage.sessions import resolve

        _, scope = resolve(conn, handle)
        issue_claims.fence(
            conn,
            scope,
            task.issue_id,
            delivered=state == "delivered",
            terminal=state == "terminal",
            source="authored-history-control",
        )
        if state == "corrupt":
            conn.execute("UPDATE issue_lifecycle SET delivered=2")
    with pytest.raises(SchemaError if state == "corrupt" else InvalidTransition):
        execution.validate_work_lease(handle, attempt.id, attempt.version)
