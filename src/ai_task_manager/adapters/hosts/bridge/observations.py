"""Ordered receipt stages require their own actual native and public-sink observations."""

import secrets
from dataclasses import asdict
from uuid import uuid4

from ai_task_manager.adapters.decisions.authority import HostObservation
from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.domains.work.models import Scope

from .records import AcceptedInput, PublicOutput


class NotificationObserver:
    def __init__(self, transcript, startup, sink):
        self.transcript, self.startup, self.sink = transcript, startup, sink
        self.journal, self.binding = transcript.journal, transcript.binding
        self.pending, self.receipts, self.outputs, self.turns = {}, {}, {}, {}
        self._claude_active = None
        self._restore()
        transcript.listen(self._observe)

    def _restore(self):
        owner = {
            "scope": asdict(self.binding.scope),
            "session_id": self.binding.core_session,
            "attempt_id": self.binding.attempt_id,
        }
        for row in self.journal._history("notification_input"):
            data = row["payload"]
            if all(data.get(k) == v for k, v in owner.items()):
                value = AcceptedInput(**data["input"])
                self.pending[value.event_id] = value
        for row in self.journal._history("notification"):
            data = row["payload"]
            if all(data.get(k) == v for k, v in owner.items()):
                self.receipts[(data["event_id"], data["stage"])] = row["ref"]
                if data.get("turn_id"):
                    self.turns[data["event_id"]] = data["turn_id"]
        # Recover source observations, never generated replacement text. Exact
        # process identity prevents another controller's frame from filling a gap.
        if self.startup.native_session:
            identity = vars(self.transcript.process.identity)
            expected = {
                "attempt_id": self.binding.attempt_id,
                "worker_id": self.binding.worker_id,
                "configuration_digest": self.binding.configuration_digest,
                "process": identity,
            }
            for row in self.journal._history("trace"):
                data = row["payload"]
                if isinstance(data.get("frame"), dict) and all(
                    data.get(key) == value for key, value in expected.items()
                ):
                    self._observe(data["frame"], row["ref"])

    def prepare(self, event_id, text):
        if not self.startup.ready_ref:
            raise AccessDenied("An actual bound native session is required")
        if event_id in self.pending:
            return self.pending[event_id]
        value = AcceptedInput(event_id, secrets.token_urlsafe(24), str(uuid4()), text)
        self.journal._append(
            "notification_input",
            {
                "scope": asdict(self.binding.scope),
                "session_id": self.binding.core_session,
                "attempt_id": self.binding.attempt_id,
                "input": asdict(value),
            },
        )
        self.pending[event_id] = value
        return value

    def context(self, event_id):
        value = self.pending[event_id]
        return f"Event {value.event_id}\nChallenge {value.challenge}\n{value.text}"

    def _receipt(self, event_id, stage, sources):
        key = (event_id, stage)
        if key in self.receipts:
            return self.receipts[key]
        prior = {"ai_acknowledged": "delivered", "user_reported": "ai_acknowledged"}.get(stage)
        if prior:
            try:
                sources = (*sources, self.receipts[(event_id, prior)])
            except KeyError as exc:
                raise AccessDenied("Native notification stages are out of order") from exc
        source_hash = digest([self.journal._read(s) for s in sources])
        ref = self.journal._append(
            "notification",
            {
                "scope": asdict(self.binding.scope),
                "event_id": event_id,
                "session_id": self.binding.core_session,
                "native_session": self.startup.native_session,
                "stage": stage,
                "trace_hash": source_hash,
                "attempt_id": self.binding.attempt_id,
                "turn_id": self.turns.get(event_id),
            },
            sources=sources,
        )
        self.receipts[key] = ref
        return ref

    def _observe(self, frame, source):
        if not self.startup.native_session:
            return
        host = self.binding.host
        if host == "codex":
            method, params = frame.get("method"), frame.get("params", {})
            if params.get("threadId") != self.startup.native_session:
                return
            item = params.get("item", {})
            if method == "item/completed" and item.get("type") == "userMessage":
                for value in self.pending.values():
                    text = "\n".join(x.get("text", "") for x in item.get("content", []))
                    if item.get("clientId") == value.input_id and text == self.context(
                        value.event_id
                    ):
                        self.turns[value.event_id] = params.get("turnId")
                        self._receipt(value.event_id, "delivered", (source,))
            if method == "item/tool/call":
                self._ack_call(params, source)
            if (
                method == "item/completed"
                and item.get("type") == "agentMessage"
                and item.get("phase") == "final_answer"
            ):
                self._output(params.get("turnId"), item.get("id"), item.get("text"), source)
        elif frame.get("session_id") == self.startup.native_session:
            if frame.get("type") == "user" and frame.get("isReplay") is True:
                for value in self.pending.values():
                    if frame.get("uuid") == value.input_id and frame.get("message", {}).get(
                        "content"
                    ) == self.context(value.event_id):
                        self.turns[value.event_id] = value.input_id
                        self._claude_active = value.event_id
                        self._receipt(value.event_id, "delivered", (source,))
            if frame.get("type") == "assistant":
                for block in frame.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        self._ack_call(
                            {
                                "tool": block.get("name"),
                                "arguments": block.get("input"),
                                "callId": block.get("id"),
                                "turnId": self.turns.get(self._claude_active),
                            },
                            source,
                        )
            if (
                frame.get("type") == "result"
                and frame.get("is_error") is False
                and frame.get("subtype") == "success"
            ):
                self._output(
                    self.turns.get(self._claude_active),
                    frame.get("uuid"),
                    frame.get("result"),
                    source,
                )
                self._claude_active = None

    def _ack_call(self, params, source):
        if params.get("tool") not in {"aitm_acknowledge_event", "mcp__aitm__acknowledge_event"}:
            return
        args = params.get("arguments")
        if not isinstance(args, dict) or not params.get("callId"):
            raise AccessDenied("Native AI acknowledgement is malformed")
        value = self.pending.get(args.get("event_id"))
        if (
            value is None
            or args.get("challenge") != value.challenge
            or params.get("turnId") != self.turns.get(value.event_id)
        ):
            raise AccessDenied("Native acknowledgement belongs to another event")
        self._receipt(value.event_id, "ai_acknowledged", (source,))

    def _output(self, turn_id, item_id, text, source):
        if not all(isinstance(v, str) and v for v in (turn_id, item_id, text)):
            return
        # A report must name its exact pending event, not merely be the next output.
        for event_id in self.pending:
            if (
                (event_id, "ai_acknowledged") not in self.receipts
                or event_id not in text
                or self.turns.get(event_id) != turn_id
            ):
                continue
            self.outputs[event_id] = PublicOutput(
                event_id,
                self.startup.native_session,
                turn_id,
                item_id,
                text,
                source,
            )

    def publish(self, event_id):
        if (event_id, "user_reported") in self.receipts:
            return self.receipts[(event_id, "user_reported")]
        output = self.outputs.get(event_id)
        if output is None:
            raise AccessDenied("No actual public final host output exists for this event")
        # The trusted sink owns its connection and must raise on absent/failed delivery.
        sink_ref = self.sink.publish(output)
        sink = self.journal._read(sink_ref, "trace")["payload"]
        if (
            sink.get("output_hash") != digest(asdict(output))
            or sink.get("native_session") != self.startup.native_session
        ):
            raise AccessDenied("Public sink did not confirm this exact host output")
        return self._receipt(event_id, "user_reported", (output.trace_ref, sink_ref))

    def reference(self, event_id, stage):
        try:
            return self.receipts[(event_id, stage)]
        except KeyError as exc:
            raise AccessDenied("This host observation is still pending") from exc


class NotificationReceipts:
    def __init__(self, journal):
        self.journal = journal

    def verify_notification(self, ref):
        data = self.journal._read(ref, "notification")["payload"]
        return HostObservation(
            Scope(**data["scope"]),
            data["event_id"],
            data["session_id"],
            data["stage"],
            data["trace_hash"],
        )
