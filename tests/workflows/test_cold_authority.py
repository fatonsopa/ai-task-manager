"""A current capability must keep verifying its original activation history."""

import json
import sqlite3

import pytest

from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.config.files import read_private
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.installation.sessions.recovery import history
from ai_task_manager.installation.workflows.bootstrap import activation_reference
from ai_task_manager.installation.workflows.cold import ColdRecovery
from tests.workflows.test_cold_operator import cold_owner as cold_owner


@pytest.mark.parametrize("cold_owner", ["recovery"], indirect=True)
def test_missing_original_activation_after_admission_fences_current_capability(cold_owner):
    root, owner, _ = cold_owner
    value = json.loads((root / "recovery-intent.json").read_text())
    value.update(action="stop", operation_key="recovery-original")
    config = read_private(root / "service.json")
    control = config["workflow_control"]
    journal = AuthorityJournal.open_existing(config["authority_database"], config["authority_key"])
    reference = activation_reference(root / "service.json", control)
    activated = history(journal, reference)
    owner.kill()
    owner.wait(timeout=5)
    capability = ColdRecovery.admit(
        root / "service.json", root / "installation.json", value, control, activated
    )
    capability.check()
    with sqlite3.connect(config["authority_database"]) as conn:
        original = conn.execute(
            "SELECT ref,data,mac FROM records WHERE ref=?", (reference,)
        ).fetchone()
        conn.execute("DELETE FROM records WHERE ref=?", (reference,))
    try:
        with pytest.raises(AccessDenied, match="expired or changed"):
            capability.check()
    finally:
        with sqlite3.connect(config["authority_database"]) as conn:
            conn.execute("INSERT INTO records VALUES(?,?,?)", original)
    capability.check()
