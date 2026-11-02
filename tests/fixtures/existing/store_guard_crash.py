"""Owned fault injection into the actual claim-store transaction, never production."""

import json
import os
import sys

from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.application.core import Core
from ai_task_manager.application.intake import prose_digest
from ai_task_manager.config.claim_store import open_claim_store
from ai_task_manager.domains.work.models import Approval, ApprovalPolicy, SessionHandle, TaskProse
from ai_task_manager.domains.work.ports import ExactRenderer
from tests.domains.helpers import ProofFixture
from tests.existing.test_store_aba import Connection

data = json.loads(open(sys.argv[1]).read())
journal = AuthorityJournal(data["authority"], data["key"])
database, _ = open_claim_store(data["database"], journal)
proofs, renderer = ProofFixture(), ExactRenderer()
policy = ApprovalPolicy("writer-frozen", "issue-reference-frozen", "style-frozen")
core = Core(database, policy, approvals=proofs, renderer=renderer)
prose = TaskProse(
    "Verify owned crash recovery", "Retain the pretransaction identity and task state."
)
rendered = renderer.render(prose)
proofs.approvals["synthetic-crash-approval"] = Approval(
    prose_digest(prose, rendered), policy, rendered.representation
)
connect = database.connect


def fault(conn, sql, parameters):
    result = conn.execute(sql, parameters)
    selected = {
        "temporary_write": sql.startswith("UPDATE main.store_identity"),
        "savepoint_rollback": sql.startswith("ROLLBACK TO aitm_claim_store"),
        "domain_change": sql.startswith("INSERT INTO outbox"),
    }
    if selected[data["stage"]]:
        os._exit(86)
    return result


database.connect = lambda: Connection(connect(), fault)
core.intake.submit(
    SessionHandle(data["handle"]), prose, "synthetic-crash-approval", "crash-operation"
)
raise SystemExit("The owned crash barrier was not reached")
