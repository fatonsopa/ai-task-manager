"""Structured host traces distinguish observed model turns from process startup."""

import json
from dataclasses import dataclass

from ai_task_manager.supervisor.models import RuntimeRefused


@dataclass(frozen=True)
class HostTurn:
    host: str
    session_id: str
    model: str | None
    final_text: str
    tools: tuple[str, ...]
    usage: dict
    estimated_cost: float | None


def records(text):
    if not isinstance(text, str) or len(text.encode()) > 10 * 1024 * 1024:
        raise RuntimeRefused("Host trace is missing or exceeds its size limit")
    result = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if len(line.encode()) > 1024 * 1024:
            raise RuntimeRefused("Host event exceeds its size limit")
        try:
            data = json.loads(line)
        except ValueError as error:
            raise RuntimeRefused("Host trace contains invalid JSON") from error
        if not isinstance(data, dict) or not isinstance(data.get("type"), str):
            raise RuntimeRefused("Host event needs a typed object")
        result.append(data)
    return result


def completed_process(returncode, quiescent):
    if returncode != 0 or quiescent is not True:
        raise RuntimeRefused("Successful model execution needs a confirmed successful host process")
