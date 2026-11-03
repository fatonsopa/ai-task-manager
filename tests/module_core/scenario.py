"""Execute installed CLI operations and retain every response before checking it."""

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from tests.application.helpers import FEATURE_BODY, reviewed_control
from tests.module_core.fixture import CLI, Demonstration


def run(root, database, writer_root):
    root = Path(root)
    fixture = Demonstration(root, database, writer_root)
    transcript = root / "commands.jsonl"
    expected_errors = json.loads(Path(__file__).with_name("expectations-v3.json").read_text())[
        "public_error_contract"
    ]
    latest_effects = None

    def effects():
        with sqlite3.connect("file:" + str(database) + "?mode=ro", uri=True) as conn:
            tables = {
                name: conn.execute("SELECT * FROM " + name + " ORDER BY rowid").fetchall()
                for name in ("tasks", "attempts", "outbox", "operations")
            }
        return {
            "core_state_sha256": hashlib.sha256(json.dumps(tables).encode()).hexdigest(),
            "provider_mutations": sum(
                "mutation " in request[0]["query"] for request in fixture.provider.requests
            ),
        }

    def refusal(response, case):
        expected = expected_errors[case]
        assert response[0] == expected["exit_code"], (case, response)
        assert response[1]["outcome"] == expected["outcome"], (case, response)
        assert response[1]["error"]["category"] == expected["outcome"]
        assert latest_effects[0] == latest_effects[1], (case, latest_effects)

    def call(group, action, data=None, *, mode="normal", extra=()):
        nonlocal latest_effects
        before_effects = effects()
        args = [
            sys.executable,
            "-I",
            "-c",
            CLI,
            fixture.endpoint,
            mode,
            "--config",
            str(fixture.config),
            "--json",
            *extra,
            group,
            action,
        ]
        if data is not None:
            args += ["--input", "-"]
        child = subprocess.run(
            args,
            input=json.dumps(data) if data is not None else None,
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": "/usr/bin:/bin"},
        )
        latest_effects = (before_effects, effects())
        record = {
            "before_effects": latest_effects[0],
            "after_effects": latest_effects[1],
            "command": args,
            "input": data,
            "returncode": child.returncode,
            "stdout": child.stdout,
            "stderr": child.stderr,
        }
        with transcript.open("a") as out:
            out.write(json.dumps(record) + "\n")
        if mode == "drop-reply" and child.returncode == 70:
            return 70, {"lost_reply": True}
        envelope = json.loads(child.stdout or child.stderr)
        return child.returncode, envelope

    try:
        before = call("task", "list")
        assert before == (
            0,
            {"ok": True, "outcome": "confirmed", "data": [], "quarantined_count": 0},
        ), before
        prepared = call(
            "writing",
            "prepare",
            {
                "kind": "issue_feature",
                "purpose": "task",
                "title": "Verify scoped task ownership",
                "body": FEATURE_BODY,
                "code_bearing": False,
                "required_work": ["complete"],
            },
        )
        assert prepared[0] == 0, prepared
        item = prepared[1]["data"]
        reviewed = call(
            "writing",
            "review",
            {
                "writing_id": item["writing_id"],
                "review": reviewed_control(item["packet"]),
            },
        )
        assert reviewed[0] == 0 and reviewed[1]["data"]["accepted"], reviewed
        finalized = call("writing", "finalize", {"writing_id": item["writing_id"]})
        assert finalized[0] == 0, finalized
        submitted = call(
            "task",
            "submit",
            {
                "writing_ref": finalized[1]["data"]["approval_ref"],
                "operation_key": "m1-create",
            },
        )
        assert submitted[0] == 0, submitted
        task = submitted[1]["data"]
        assert task["observed_status"] == "Backlog" and task["sync_state"] == "confirmed", task
        shown = call("task", "show", {"task_id": task["id"]})
        assert shown[0] == 0 and shown[1]["data"] == task, shown
        checks = {"create_read": "passed"}
        assert len(fixture.provider.issues) == 1
        arguments = {
            "task_id": task["id"],
            "action": "enqueue",
            "expected_version": task["version"],
            "decision_revision": task["decision_revision"],
            "operation_key": "m1-queue",
        }
        queued = call("task", "action", arguments)
        assert queued[0] == 0 and queued[1]["data"]["observed_status"] == "Todo", queued
        current = queued[1]["data"]
        assert current["queued_once"] and current["execution_state"] == "idle", current
        assert current["scheduling"]["queue_sequence"] is not None
        assert current["scheduling"]["state"] == "unknown" and not current["work_authorized"]
        checks["explicit_queue"] = "passed"
        replay = call("task", "action", arguments)
        assert replay == queued, replay
        changed = call("task", "action", {**arguments, "action": "dequeue"})
        refusal(changed, "changed_operation")
        for field in ("expected_version", "decision_revision"):
            stale = {
                **arguments,
                "expected_version": current["version"],
                "decision_revision": current["decision_revision"],
                "operation_key": "m1-stale-" + field,
                "action": "dequeue",
            }
            stale[field] = current[field if field == "decision_revision" else "version"] + 1
            refused = call("task", "action", stale)
            refusal(refused, "stale_version_or_revision")
        checks["stale_decision"] = "passed"
        boundary = {
            **arguments,
            "expected_version": current["version"],
            "decision_revision": current["decision_revision"],
        }
        illegal = call(
            "task",
            "action",
            {**boundary, "action": "non_code_complete", "operation_key": "m1-illegal-complete"},
        )
        refusal(illegal, "illegal_transition")
        human = call(
            "task",
            "action",
            {
                **boundary,
                "action": "hold",
                "human_ref": "caller-claim",
                "operation_key": "m1-missing-human",
            },
        )
        refusal(human, "missing_human")
        missing = call(
            "task", "submit", {"writing_ref": "caller-claim", "operation_key": "m1-missing-writing"}
        )
        refusal(missing, "missing_writing")
        worker = call(
            "worker",
            "start",
            {
                k: v
                for k, v in {
                    **boundary,
                    "host": "claude",
                    "operation_key": "m1-no-host",
                }.items()
                if k != "action"
            },
        )
        refusal(worker, "missing_workers")
        for name in ("illegal_transition", "missing_human", "missing_evidence"):
            checks[name] = "passed_refusal"
        from tests.linear.helpers import uid

        fixture.provider.viewer_id = uid(999)
        wrong_user = call("task", "list")
        fixture.provider.viewer_id = fixture.components.binding.expected.viewer_id
        refusal(wrong_user, "wrong_user")
        wrong_session = call("task", "list", extra=("--session-file", str(root / "foreign.json")))
        refusal(wrong_session, "wrong_session")
        checks["wrong_scope"] = "passed"
        unchanged = call("task", "show", {"task_id": task["id"]})
        assert unchanged == (0, {"ok": True, "outcome": "confirmed", "data": current}), unchanged
        lost_request = {
            "writing_ref": finalized[1]["data"]["approval_ref"],
            "operation_key": "m1-lost-create",
        }
        lost = call("task", "submit", lost_request, mode="drop-reply")
        assert lost[0] == 70, lost
        assert len(fixture.provider.issues) == 2
        recovered = call("task", "submit", lost_request)
        again = call("task", "submit", lost_request)
        assert recovered[0] == 0 and recovered == again, (recovered, again)
        assert len(fixture.provider.issues) == 2
        checks["idempotency"] = checks["restart"] = "passed"
        listing = call("task", "list")
        assert listing[0] == 0 and len(listing[1]["data"]) == 2
        mcp_result = mcp_parity(fixture, listing[1], root)
        checks["cli_mcp"] = "passed"
        result = {
            "status": "independent_demonstration_passed_connections_pending",
            "checks": checks,
            "initial_task": task,
            "queued_task": current,
            "final_tasks": listing[1]["data"],
            "mcp": mcp_result,
            "fixture_scope": "Authored loopback provider and writing review; no live acceptance",
            "transcript": str(transcript),
            "pending": [
                "Installed human instruction producer",
                "Actual worker integration",
                "Live Linear connection",
                "Independent review and integrated acceptance",
            ],
        }
        (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
    finally:
        fixture.close()
        (root / "cleanup.json").write_text(
            json.dumps(
                {"provider_thread_stopped": True, "native_workers_started": 0, "docker_started": 0}
            )
            + "\n"
        )


def mcp_parity(fixture, expected, root):
    import anyio
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    from ai_task_manager.application.service.models import SCHEMAS

    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-I",
            "-c",
            CLI,
            fixture.endpoint,
            "normal",
            "--config",
            str(fixture.config),
            "mcp",
            "serve",
        ],
        env={"PATH": "/usr/bin:/bin"},
        cwd=root,
    )
    seen = []

    async def check():
        async with Client(params, read_timeout_seconds=15) as client:
            schemas = await client.list_tools()
            assert {item.name: item.input_schema for item in schemas.tools} == SCHEMAS
            response = await client.call_tool("task_list", {})
            assert response.structured_content == expected, response.structured_content
            inventory = subprocess.check_output(["ps", "-axo", "pid=,command="], text=True)
            seen.extend(
                int(line.split()[0])
                for line in inventory.splitlines()
                if str(fixture.config) in line and "mcp serve" in line
            )
            assert seen

    anyio.run(check)
    inventory = subprocess.check_output(["ps", "-axo", "pid=,stat="], text=True)
    live = {
        int(row.split()[0])
        for row in inventory.splitlines()
        if not row.split()[1].startswith(("Z", "X"))
    }
    assert not set(seen) & live
    result = {
        "schema_equal": True,
        "tasks_equal": True,
        "owned_pids": seen,
        "remaining_owned_pids": sorted(set(seen) & live),
    }
    (root / "mcp.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
