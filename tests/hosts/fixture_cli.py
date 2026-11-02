"""Deterministic authored protocol control; never a real Codex or account."""

import json
import os
import sys

ADVERSARIAL_INIT = False
THREAD = "synthetic-native-session"
ACCOUNT = {"type": "chatgpt", "email": "fixture@example.invalid", "planType": "synthetic"}


def send(value):
    print(json.dumps(value), flush=True)


def tool(call_id, turn_id):
    send(
        {
            "id": 91,
            "method": "item/tool/call",
            "params": {
                "threadId": THREAD,
                "turnId": turn_id,
                "callId": call_id,
                "tool": "aitm_write_file",
                "arguments": {
                    "path": "owned.py",
                    "text": "answer = 42\n",
                    "expected_hash": None,
                    "operation_key": "synthetic-owned-edit",
                },
            },
        }
    )


def main():
    if sys.argv[-2:] == ["login", "status"]:
        sys.stderr.write("Logged in using ChatGPT\n")
        return
    configuration = {"mcp_servers": {}}
    for index, argument in enumerate(sys.argv):
        if argument != "-c":
            continue
        key, raw = sys.argv[index + 1].split("=", 1)
        cursor = configuration
        parts = key.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part.strip('"'), {})
        cursor[parts[-1]] = json.loads(raw)
    for line in sys.stdin:
        value = json.loads(line)
        if value.get("id") == 91 and "result" in value:
            if not ADVERSARIAL_INIT:
                send(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": THREAD,
                            "turn": {"id": "synthetic-turn", "status": "completed"},
                        },
                    }
                )
            continue
        method = value.get("method")
        if method == "initialized":
            continue
        if method == "initialize":
            result = {"codexHome": os.environ["CODEX_HOME"], "userAgent": "synthetic-fixture"}
        elif method == "account/read":
            assert value["params"] == {"refreshToken": False}
            result = {"account": ACCOUNT}
        elif method == "config/read":
            result = {
                "config": configuration,
                "origins": {},
                "layers": [
                    {
                        "name": {"type": "user", "file": os.environ["CODEX_HOME"] + "/config.toml"},
                        "version": "authored-fixture-version",
                        "config": configuration,
                    }
                ],
            }
        elif method == "configRequirements/read":
            result = {"requirements": None}
        elif method == "mcpServerStatus/list":
            result = {"data": [], "nextCursor": None}
        elif method == "thread/start":
            if ADVERSARIAL_INIT:
                tool("synthetic-early-call", "synthetic-early-turn")
            result = {
                "thread": {"id": THREAD},
                "model": "synthetic-no-model",
                "modelProvider": "openai",
                "approvalPolicy": "never",
                "approvalsReviewer": "user",
                "reasoningEffort": "high",
                "instructionSources": [],
                "sandbox": {"type": "readOnly"},
                "cwd": os.getcwd(),
            }
        elif method == "turn/start":
            result = {"turn": {"id": "synthetic-turn"}}
        else:
            raise AssertionError("Unexpected fixture RPC")
        send({"id": value["id"], "result": result})
        if method == "turn/start":
            tool("synthetic-owned-call", "synthetic-turn")


if __name__ == "__main__":
    main()
