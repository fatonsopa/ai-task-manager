"""Bounded native app-server RPC with correlated dynamic tool calls."""

import json

from ai_task_manager.domains.work.errors import AccessDenied


class CodexSession:
    def __init__(self, process, transcript, startup, gateway):
        self.process, self.transcript, self.startup, self.gateway = (
            process,
            transcript,
            startup,
            gateway,
        )
        self._counter, self._responses, self._calls = 0, {}, set()
        self._pending = set()
        self._active_turn = None

    def _send(self, frame):
        self.process.write(json.dumps(frame, allow_nan=False) + "\n")

    def request(self, method, params):
        self._counter += 1
        identity = self._counter
        self._pending.add(identity)
        try:
            if method in getattr(self.startup, "METHODS", ()):
                self.startup.expect(identity, method, params)
            self._send({"id": identity, "method": method, "params": params})
            while identity not in self._responses:
                self.pump()
            response = self._responses.pop(identity)
        finally:
            self._pending.discard(identity)
        if "error" in response:
            raise AccessDenied("Native host request refused")
        return response.get("result")

    def pump(self):
        frame = self.transcript.read()
        if frame is None:
            return None
        if "id" in frame and "method" not in frame:
            if (
                type(frame["id"]) is not int
                or frame["id"] not in self._pending
                or frame["id"] in self._responses
                or ("result" in frame) == ("error" in frame)
            ):
                raise AccessDenied("Native response identity is unrequested, repeated or invalid")
            self._responses[frame["id"]] = frame
        elif "id" in frame:
            method, params = frame.get("method"), frame.get("params", {})
            if method != "item/tool/call":
                self._send(
                    {
                        "id": frame["id"],
                        "error": {"code": -32601, "message": "Unsupported managed host request"},
                    }
                )
                raise AccessDenied(
                    "Native approval, auth refresh or unknown request requires operator review"
                )
            if (
                params.get("threadId") != self.startup.native_session
                or not params.get("turnId")
                or not params.get("callId")
                or params["callId"] in self._calls
                or (self._active_turn is not None and params["turnId"] != self._active_turn)
            ):
                raise AccessDenied("Dynamic tool request has a foreign or repeated native identity")
            self._calls.add(params["callId"])
            name = params.get("tool", "")
            try:
                if not name.startswith("aitm_"):
                    raise AccessDenied("Native tool bypass is unsupported")
                value = self.gateway.call(name.removeprefix("aitm_"), params.get("arguments"))
                result = {
                    "success": True,
                    "contentItems": [{"type": "inputText", "text": json.dumps(value)}],
                }
            except Exception as exc:
                result = {
                    "success": False,
                    "contentItems": [{"type": "inputText", "text": type(exc).__name__}],
                }
            self._send({"id": frame["id"], "result": result})
        return frame

    def initialize(self, cwd):
        self.process.start()
        self.request(
            "initialize",
            {
                "clientInfo": {"name": "ai-task-manager", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        self._send({"method": "initialized"})
        self.request("account/read", {"refreshToken": False})
        if self.startup.configuration is not None:
            self.request("config/read", {"includeLayers": True, "cwd": str(cwd)})
            self.request("configRequirements/read", {})
            self.startup.configuration.pre_thread()
        result = self.request(
            "thread/start",
            {
                "cwd": str(cwd),
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "ephemeral": True,
                "allowProviderModelFallback": False,
                "dynamicTools": [
                    {
                        "type": "function",
                        "name": "aitm_" + name,
                        "description": "Use the assigned managed task operation.",
                        "inputSchema": schema,
                        "deferLoading": False,
                    }
                    for name, schema in self.gateway.schemas.items()
                ],
            },
        )
        if self.startup.configuration is not None:
            cursor = None
            while True:
                page = self.request(
                    "mcpServerStatus/list",
                    {
                        "threadId": self.startup.native_session,
                        "detail": "full",
                        "cursor": cursor,
                        "limit": 64,
                    },
                )
                cursor = page.get("nextCursor")
                if cursor is None:
                    break
        return result

    def turn(self, text, *, input_id=None):
        params = {
            "threadId": self.startup.native_session,
            "input": [{"type": "text", "text": text}],
        }
        if input_id is not None:
            params["clientUserMessageId"] = input_id
        response = self.request("turn/start", params)
        self._active_turn = response.get("turn", {}).get("id")
        if not self._active_turn:
            raise AccessDenied("Native turn identity was not observed")
        while True:
            frame = self.pump()
            if not frame:
                continue
            if frame.get("method") == "turn/completed":
                params = frame.get("params", {})
                if (
                    params.get("threadId") != self.startup.native_session
                    or params.get("turn", {}).get("id") != self._active_turn
                ):
                    raise AccessDenied("Foreign native terminal turn")
                self._active_turn = None
                if params["turn"].get("status") != "completed":
                    raise AccessDenied("Native model turn failed or was interrupted")
                return params["turn"]

    def close(self):
        return self.process.stop()
