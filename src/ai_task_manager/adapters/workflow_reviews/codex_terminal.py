"""One final answer from the owned Codex assignment turn and signed transcript."""

from dataclasses import asdict

from ai_task_manager.domains.work.errors import EvidenceRequired


class CodexReviewTerminal:
    def __init__(self, native):
        self.native = native
        self.answer = None
        self.turn = None

    def observe(self, frame, source):
        if frame.get("method") != "item/completed":
            return
        params = frame.get("params", {})
        item = params.get("item", {})
        if item.get("type") != "agentMessage" or item.get("phase") != "final_answer":
            return
        turn = self.native.session._active_turn
        if (
            not turn
            or params.get("threadId") != self.native.startup.native_session
            or params.get("turnId") != turn
            or not isinstance(item.get("id"), str)
            or not item["id"]
            or not isinstance(item.get("text"), str)
            or self.answer is not None
        ):
            raise EvidenceRequired("Codex review final answer is foreign, repeated or incomplete")
        self.answer, self.turn = source, turn

    def decode(self, terminal):
        if self.answer is None:
            raise EvidenceRequired("Codex review has no observed final answer")
        answer = self.native.journal._read(self.answer, "trace", fresh=False)
        expected = {
            "attempt_id": self.native.binding.attempt_id,
            "worker_id": self.native.binding.worker_id,
            "configuration_digest": self.native.binding.configuration_digest,
            "process": asdict(self.native.process.identity),
        }
        if any(answer["payload"].get(k) != v for k, v in expected.items()):
            raise EvidenceRequired("Codex review answer source changed")
        params = terminal["payload"]["frame"].get("params", {})
        frame = answer["payload"]["frame"]
        output = frame.get("params", {})
        if (
            terminal["payload"]["frame"].get("method") != "turn/completed"
            or params.get("threadId") != self.native.startup.native_session
            or params.get("turn", {}).get("id") != self.turn
            or params["turn"].get("status") != "completed"
            or params["turn"].get("error") is not None
            or frame.get("method") != "item/completed"
            or output.get("threadId") != self.native.startup.native_session
            or output.get("turnId") != self.turn
            or output.get("item", {}).get("type") != "agentMessage"
            or output["item"].get("phase") != "final_answer"
            or answer["observed_at"] > terminal["observed_at"]
        ):
            raise EvidenceRequired("Codex review requires its matching successful terminal turn")
        return output["item"]["text"], answer
