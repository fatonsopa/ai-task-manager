"""Authored credential-free Claude protocol fixture; not an account or model."""

import json
import os
import sys

MODE = "normal"
SESSION = "authored-bootstrap-session"
event_id = None
args = None


def send(value):
    print(json.dumps(value), flush=True)


if sys.argv[-2:] == ["auth", "status"]:
    send(
        {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "configDirectory": os.environ["CLAUDE_CONFIG_DIR"],
            "email": (
                "foreign@example.invalid"
                if MODE == "account-change" and os.path.exists("account-switch")
                else "fixture@example.invalid"
            ),
            "orgId": "authored-org",
            "userId": "authored-user",
        }
    )
    raise SystemExit(0)

model = sys.argv[sys.argv.index("--model") + 1]
tools = sys.argv[sys.argv.index("--allowedTools") + 1].split(",")
for line in sys.stdin:
    value = json.loads(line)
    if value.get("type") == "control_request":
        request = value["request"]
        if request["subtype"] == "initialize":
            send(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": value["request_id"],
                        "response": {
                            "commands": [],
                            "output_style": "default",
                            "available_output_styles": [],
                        },
                    },
                }
            )
        elif request["subtype"] == "interrupt":
            break
        else:
            raise RuntimeError("Unreviewed authored fixture control request")
    elif value.get("type") == "control_response":
        response = value["response"]
        mcp_response = response.get("response", {}).get("mcp_response", {})
        if response.get("subtype") != "success" or "error" in mcp_response:
            raise RuntimeError("Declared MCP control failed: " + repr(value))
        if response["request_id"] == "mcp-init":
            send(
                {
                    "type": "control_request",
                    "request_id": "mcp-ready",
                    "request": {
                        "subtype": "mcp_message",
                        "server_name": "aitm",
                        "message": {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    },
                }
            )
        elif response["request_id"] == "mcp-ready":
            send(
                {
                    "type": "control_request",
                    "request_id": "worker-denial",
                    "request": {
                        "subtype": "mcp_message",
                        "server_name": "aitm",
                        "message": {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "tools/call",
                            "params": {
                                "name": "write_file",
                                "arguments": {"path": "forbidden.txt", "content": "denied"},
                            },
                        },
                    },
                }
            )
        elif response["request_id"] == "worker-denial":
            if not mcp_response.get("result", {}).get("isError"):
                raise RuntimeError("Lead obtained worker file authority")
            send(
                {
                    "type": "control_request",
                    "request_id": "actual-declared-ack",
                    "request": {
                        "subtype": "mcp_message",
                        "server_name": "aitm",
                        "message": {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": "acknowledge_event", "arguments": args},
                        },
                    },
                }
            )
        else:
            if mcp_response.get("result", {}).get("isError"):
                raise RuntimeError("Declared acknowledgement refused: " + repr(value))
            send(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "duration_ms": 1,
                    "duration_api_ms": 1,
                    "num_turns": 1,
                    "session_id": SESSION,
                    "uuid": "report-result",
                    "result": "Reported " + event_id,
                }
            )
    elif value.get("type") == "user":
        if value.get("uuid"):
            text = value["message"]["content"]
            event_id, challenge = text.splitlines()[0][6:], text.splitlines()[1][10:]
            args = {"event_id": event_id, "challenge": challenge}
            send({**value, "session_id": SESSION, "isReplay": True})
            send(
                {
                    "type": "assistant",
                    "session_id": SESSION,
                    "message": {
                        "id": "ack-message",
                        "type": "message",
                        "role": "assistant",
                        "model": model,
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "ack-call",
                                "name": "mcp__aitm__acknowledge_event",
                                "input": args,
                            }
                        ],
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                }
            )
            send(
                {
                    "type": "control_request",
                    "request_id": "mcp-init",
                    "request": {
                        "subtype": "mcp_message",
                        "server_name": "aitm",
                        "message": {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2024-11-05",
                                "capabilities": {},
                                "clientInfo": {"name": "declared-fixture", "version": "1"},
                            },
                        },
                    },
                }
            )
            continue
        send(
            {
                "type": "system",
                "subtype": "init",
                "session_id": SESSION,
                "cwd": os.getcwd(),
                "model": model,
                "apiKeySource": "none",
                "claude_code_version": "2.1.301",
                "permissionMode": "dontAsk",
                "tools": tools + (["Bash"] if MODE == "native-tool" else []),
                "mcp_servers": [{"name": "aitm", "source": "sdk", "status": "connected"}],
            }
        )
        if MODE == "fallback":
            send(
                {
                    "type": "system",
                    "subtype": "model_refusal_fallback",
                    "session_id": SESSION,
                    "original_model": model,
                    "fallback_model": "authored-other",
                }
            )
        send(
            {
                "type": "assistant",
                "session_id": SESSION,
                "message": {
                    "id": "authored-message",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [{"type": "text", "text": "READY"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }
        )
        send(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "duration_ms": 1,
                "duration_api_ms": 1,
                "num_turns": 1,
                "session_id": SESSION,
                "result": "READY",
            }
        )
