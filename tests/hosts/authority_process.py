"""Separate-process authority fixture; never a production proof issuer."""

import json
import sys
from pathlib import Path

import ai_task_manager
from ai_task_manager.application.core import Core
from ai_task_manager.domains.work.models import ApprovalPolicy, Evidence, Scope, SessionHandle
from ai_task_manager.storage.database import Database
from tests.domains.helpers import ProofFixture


def main():
    payload = json.loads(Path(sys.argv[1]).read_text())
    runtime_root = str(Path(ai_task_manager.__file__).resolve().parent)
    if "runtime_root" in payload:
        assert runtime_root == payload["runtime_root"]
        Path(payload["import_record"]).write_text(json.dumps({"runtime_root": runtime_root}))
    proofs = ProofFixture()
    data = payload["proof"]
    proofs.evidence["hold-fixture"] = Evidence(
        data["kind"], Scope(**data["scope"]), data["subject"], data["payload"]
    )
    core = Core(
        Database(payload["database"]),
        ApprovalPolicy(**payload["policy"]),
        evidence=proofs,
        clock=lambda: 1000,
    )
    original = core.context.db.connect

    def connect():
        value = original()
        print("CONNECT", flush=True)
        return value

    core.context.db.connect = connect
    print("READY", flush=True)
    if sys.stdin.readline().strip() != "GO":
        raise RuntimeError("Explicit test barrier missing")
    core.decisions.record(
        SessionHandle(payload["handle"]),
        payload["task_id"],
        payload["version"],
        "hold-fixture",
        "child-hold",
    )
    print("COMMITTED", flush=True)


if __name__ == "__main__":
    main()
