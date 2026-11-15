"""Declared local protocol tail; never genuine model judgment or live acceptance."""

import hashlib
import json
import os
import selectors
import sys
import time
from dataclasses import asdict
from pathlib import Path

from ai_task_manager.adapters.hosts.bridge.auth_probe import claude_account_hash
from ai_task_manager.adapters.hosts.bridge.human import OperatorChannel
from ai_task_manager.adapters.workflow_control.identity import file_binding
from ai_task_manager.config.files import digest, read_private, write_private
from ai_task_manager.config.service import load_service
from ai_task_manager.installation.native import NativeManifest, lead_startup, normal
from ai_task_manager.installation.native.manifest import installation_intent
from ai_task_manager.installation.native.store import NativeStore
from tests.application.helpers import reviewed_control
from tests.native_installation.helpers import bundle, refresh_digest
from tests.native_installation.test_lead_startup import DeclaredRestrictions

REPORT = "Owned non-code report produced through the managed write_file tool.\n"
REVIEW_BODY = """## Description

The owned report needs a separate review of its actual content and required work.

## Expected outcome

A distinct tracked reviewer inspects the current report.
Its verdict passes through the installed native review capture.

## Acceptance criteria

* Review the exact current author task, approved prose and repository source.
* Accept only the expected report created through the managed file tool.
* Preserve a rejected or malformed result and stop the owned reviewer process.
"""


def native_script(role):
    """Emit actual SDK/MCP messages; the runtime alone issues completion proofs."""
    if role not in {"author", "reviewer"}:
        raise ValueError("Only the two declared local roles are available")
    return (
        f"ROLE = {role!r}\nREPORT = {REPORT!r}\n"
        + r"""
import json, os, sys
SESSION = "declared-cycle-" + ROLE

def send(value):
    print(json.dumps(value), flush=True)

def result(text):
    send({"type":"result", "subtype":"success", "is_error":False,
          "duration_ms":1, "duration_api_ms":1, "num_turns":1,
          "session_id":SESSION, "result":text})

def mcp(request_id, message):
    send({"type":"control_request", "request_id":request_id,
          "request":{"subtype":"mcp_message", "server_name":"aitm", "message":message}})

if sys.argv[-2:] == ["auth", "status"]:
    send({"loggedIn":True, "authMethod":"claude.ai", "apiProvider":"firstParty",
          "configDirectory":os.environ["CLAUDE_CONFIG_DIR"],
          "email":"fixture@example.invalid", "orgId":"authored-org", "userId":"authored-user"})
    raise SystemExit(0)
model = sys.argv[sys.argv.index("--model") + 1]
tools = sys.argv[sys.argv.index("--allowedTools") + 1].split(",")
for line in sys.stdin:
    value = json.loads(line)
    if value.get("type") == "control_request":
        request = value["request"]
        if request["subtype"] == "interrupt":
            break
        if request["subtype"] != "initialize":
            raise RuntimeError("Unknown declared control request")
        send({"type":"control_response", "response":{"subtype":"success",
              "request_id":value["request_id"], "response":{"commands":[],
              "output_style":"default", "available_output_styles":[]}}})
    elif value.get("type") == "control_response":
        response = value["response"]
        answer = response.get("response", {}).get("mcp_response", {})
        if (response.get("subtype") != "success" or "error" in answer
                or answer.get("result", {}).get("isError")):
            raise RuntimeError("Declared managed file operation refused")
        if response["request_id"] == "cycle-init":
            mcp("cycle-ready", {"jsonrpc":"2.0", "method":"notifications/initialized"})
        elif response["request_id"] == "cycle-ready":
            args = {"path":"report.txt", "text":REPORT, "expected_hash":None,
                    "operation_key":"declared-cycle-report"}
            send({"type":"assistant", "session_id":SESSION,
                  "message":{"id":"cycle-write", "type":"message", "role":"assistant",
                  "model":model, "content":[{"type":"tool_use", "id":"cycle-file",
                  "name":"mcp__aitm__write_file", "input":args}],
                  "usage":{"input_tokens":1,"output_tokens":1}}})
            mcp("cycle-write", {"jsonrpc":"2.0", "id":2, "method":"tools/call",
                "params":{"name":"write_file", "arguments":args}})
        elif response["request_id"] == "cycle-write":
            result("The owned report was written through the managed file tool.")
        else:
            raise RuntimeError("Unknown declared MCP reply")
    elif value.get("type") == "user":
        text = value["message"]["content"]
        send({"type":"system", "subtype":"init", "session_id":SESSION,
              "cwd":os.getcwd(), "model":model, "apiKeySource":"none",
              "claude_code_version":"2.1.301", "permissionMode":"dontAsk", "tools":tools,
              "mcp_servers":[{"name":"aitm","source":"sdk","status":"connected"}]})
        if ROLE == "author" and "Required work units: " in text:
            mcp("cycle-init", {"jsonrpc":"2.0", "id":1, "method":"initialize",
                "params":{"protocolVersion":"2024-11-05", "capabilities":{},
                "clientInfo":{"name":"declared-cycle", "version":"1"}}})
        elif ROLE == "reviewer" and text.startswith("Independently review"):
            supplied = json.loads(text.split("\n", 1)[1])
            accepted = supplied["artifacts"] == {"report.txt":REPORT}
            verdict = {"context_digest":supplied["context_digest"],
                "criteria":[{"id":item[0], "judgment":"accept" if accepted else "reject",
                "reason":"Declared fixture compared the actual captured report bytes."}
                for item in supplied["context"]["criteria"]],
                "reason":"Scripted local protocol check, not genuine model judgment."}
            result(json.dumps(verdict))
        else:
            send({"type":"assistant", "session_id":SESSION,
                  "message":{"id":"ready", "type":"message", "role":"assistant",
                  "model":model, "content":[{"type":"text","text":"READY"}],
                  "usage":{"input_tokens":1,"output_tokens":1}}})
            result("READY")
"""
    )


def _seal(config, b, spec, script, *, role, deadline):
    executable = b.root / "declared-native"
    executable.write_text("#!" + sys.executable + "\n" + script)
    executable.chmod(0o700)
    store = NativeStore.create(b.root / "normal.sqlite")
    spec.update(role=role, deadline=deadline)
    write_private(b.installation, spec, replace=True)
    b.data.update(
        authority_store=store.pointer,
        config=file_binding(config),
        installation=installation_intent(b.installation),
        executable=str(executable),
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        account_hash=claude_account_hash(
            {"email": "fixture@example.invalid", "orgId": "authored-org", "userId": "authored-user"}
        ),
    )
    write_private(b.manifest, b.data, replace=True)
    refresh_digest(b.installation, b.manifest, b.data)
    manifest = NativeManifest(b.manifest, config, b.installation)
    normal.enroll(
        store,
        manifest,
        {"host": 60, "relay": 60, "auth": 5, "neutral": 8, "assignment": 12},
        identity_refreshes=(),
        conformance_ref="declared-cycle-contract",
        role=role,
    )
    return store, manifest


def prepare_tail(
    *, config, call, root, repository, source_head, author_task, deadline, monkeypatch
):
    """Prepare a distinct reviewer through public CLI and trusted installation APIs."""
    root, repository, config = Path(root), Path(repository), Path(config)
    assert deadline > time.time() and author_task["observed_status"] == "Todo"
    prepared = call(
        "writing",
        "prepare",
        {
            "kind": "issue_chore",
            "purpose": "task",
            "title": "Review the owned non-code report",
            "body": REVIEW_BODY,
            "code_bearing": False,
            "required_work": ["review"],
        },
    )
    call(
        "writing",
        "review",
        {"writing_id": prepared["writing_id"], "review": reviewed_control(prepared["packet"])},
    )
    approval = call("writing", "finalize", {"writing_id": prepared["writing_id"]})
    task = call(
        "task",
        "submit",
        {"writing_ref": approval["approval_ref"], "operation_key": "cycle-reviewer-submit"},
    )
    task = call(
        "task",
        "action",
        {
            "task_id": task["id"],
            "action": "enqueue",
            "expected_version": task["version"],
            "decision_revision": task["decision_revision"],
            "operation_key": "cycle-reviewer-queue",
        },
    )
    assert (
        task["id"] != author_task["id"]
        and task["observed_status"] == "Todo"
        and task["sync_state"] == "confirmed"
    )
    service = load_service(config)
    b = bundle(root / "reviewer", "claude", "claude-opus-5-5", task_id=task["id"])
    spec = {
        "version": 1,
        "config": file_binding(config),
        "scope": asdict(service.scope),
        "task_id": task["id"],
        "prose_digest": task["prose_digest"],
        "native_ref": "normal-native",
        "native_digest": "0" * 64,
        "native_manifest": str(b.manifest),
        "repository": str(repository),
        "source_head": source_head,
        "state_root": str(b.root / "state"),
        "principal_ref": "declared-reviewer",
        "plan_ref": "complete-cycle-review",
        "files": ["report.txt"],
        "artifacts": ["report.txt"],
        "installation_path": str(b.installation),
        "review_target": {
            "task_id": author_task["id"],
            "prose_digest": author_task["prose_digest"],
            "source_head": source_head,
        },
    }
    _seal(config, b, spec, native_script("reviewer"), role="reviewer", deadline=deadline)
    original = normal.NormalAuthoritySource
    monkeypatch.setattr(
        normal,
        "NormalAuthoritySource",
        lambda *a, **kw: original(*a, **{**kw, "conformance": DeclaredRestrictions()}),
    )
    write_private(
        root / "cycle-tail.json",
        {"reviewer_task": task, "reviewer_installation": str(b.installation), "deadline": deadline},
    )
    return {
        "reviewer_installation": file_binding(b.installation),
        "author_script": native_script("author"),
    }


def finish_tail(*, config, call, root, author_task, workflow_result, monkeypatch):
    """Observe Done, actual review capture, lead receipts and bytes on an owned PTY."""
    root, config = Path(root), Path(config)
    assert workflow_result["state"] == "done", workflow_result
    current = call("task", "show", {"task_id": author_task["id"]})
    assert current["observed_status"] == "Done" and current["sync_state"] == "confirmed"
    events = call("event", "list", {})
    candidates = [
        item
        for item in events
        if item["task_id"] == author_task["id"]
        and item["kind"] == "provider_status_confirmed"
        and item["payload"].get("observed_status") == "Done"
    ]
    assert len(candidates) == 1
    event = max(candidates, key=lambda item: item["sequence"])
    service = load_service(config)
    state = read_private(root / "cycle-tail.json")
    # Inspect only the actual producer's published evidence; never construct a
    # capture, completion proof or expected verdict in the test process.
    installations = [read_private(path) for path in root.glob("*/installation.json")]
    authors = [
        item
        for item in installations
        if item.get("role", "worker") == "worker" and item.get("task_id") == author_task["id"]
    ]
    assert len(authors) == 1
    captures = list((Path(authors[0]["state_root"]) / "reviews").glob("*/binding.json"))
    assert len(captures) == 1
    binding = read_private(captures[0])
    capture_path, trace_path = Path(binding["capture_path"]), Path(binding["trace_path"])
    assert hashlib.sha256(capture_path.read_bytes()).hexdigest() == binding["capture_sha256"]
    assert hashlib.sha256(trace_path.read_bytes()).hexdigest() == binding["trace_sha256"]
    capture, trace = read_private(capture_path), read_private(trace_path)
    supplied = json.loads(trace["prompt"].split("\n", 1)[1])
    assert supplied["artifacts"] == {"report.txt": REPORT}
    assert capture["record"]["reviewer_id"] != supplied["context"]["author"]["worker_id"]
    assert trace["reviewer_attempt"]["task_id"] == state["reviewer_task"]["id"]
    assert trace["reviewer_attempt"]["state"] == "done"
    assert all(item["judgment"] == "accept" for item in capture["record"]["criteria"])
    deadline = min(state["deadline"], time.time() + 45)
    b = bundle(root / "lead", "claude", "claude-opus-5-5", task_id=author_task["id"])
    spec = read_private(b.installation)
    spec.update(
        scope=asdict(service.scope),
        lead={
            "session_id": service.session_id,
            "event_id": event["id"],
            "journal": str(b.root / "host.sqlite"),
            "key": str(b.root / "host.key"),
            "directory": str(b.root / "process"),
            "cwd": str(b.root),
        },
    )
    script = (
        Path(__file__).parents[1].joinpath("native_installation/fixture_lead_native.py").read_text()
    )
    store, manifest = _seal(config, b, spec, script, role="lead", deadline=deadline)
    source = normal.NormalAuthoritySource
    monkeypatch.setattr(
        lead_startup,
        "NormalAuthoritySource",
        lambda *a, **kw: source(*a, **{**kw, "conformance": DeclaredRestrictions()}),
    )
    master, slave = os.openpty()
    original_open = os.open
    try:

        def reply(self, request, timeout):
            return {
                "request_id": request["request_id"],
                "challenge": request["challenge"],
                "accepted": True,
                "instruction_hash": digest(request["instruction"]),
            }, os.getpid()

        with monkeypatch.context() as patch:
            patch.setattr(OperatorChannel, "_exchange", reply)
            patch.setattr(
                lead_startup.os,
                "open",
                lambda path, *a, **kw: (
                    os.dup(slave) if path == "/dev/tty" else original_open(path, *a, **kw)
                ),
            )
            reported = call("lead", "report", extra=("--installation", str(b.installation)))
        assert reported["event_id"] == event["id"]
        os.set_blocking(master, False)
        public = bytearray()
        with selectors.DefaultSelector() as selected:
            selected.register(master, selectors.EVENT_READ)
            end = time.monotonic() + 2
            while time.monotonic() < end and selected.select(max(0, end - time.monotonic())):
                public.extend(os.read(master, 65536))
        assert ("Reported " + event["id"]).encode() in public
        receipts = service.core.notifications.receipts(service.handle, event["id"])
        assert set(receipts) == {"queued", "delivered", "ai_acknowledged", "user_reported"}
        from ai_task_manager.installation.native.controller import NormalInvocationController

        permission = store.permission(manifest.data["authorization_ref"])
        calls = NormalInvocationController(store, permission).snapshot()["calls"]
        assert calls and all(item["process_stopped"] for item in calls)
        result = {
            "task": current,
            "event": event,
            "receipts": receipts,
            "review_binding": binding,
            "public_output": public.decode(),
            "lead_calls": calls,
            "declared_protocol_only": True,
            "genuine_model_acceptance": False,
        }
        write_private(root / "cycle-tail-result.json", result)
        return result
    finally:
        os.close(master)
        os.close(slave)
