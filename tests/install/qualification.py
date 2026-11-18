"""Version 1: finite fixture role and physical-disposition contracts."""

import hashlib
import inspect
import json
import sys
import threading
import time
from pathlib import Path


def required_profiles(plan):
    c = plan["consumer_counts"]
    assert set(c) == {"workers", "result_leads", "queue_leads"}
    assert all(type(n) is int and n >= 0 for n in c.values())
    assert c["workers"] == len(plan["roles"])
    return tuple(
        name
        for name, n in [
            ("worker", c["workers"]),
            ("lead", c["result_leads"]),
            ("queue_lead", c["queue_leads"]),
        ]
        if n
    )


def disposition(
    plan,
    *,
    closed,
    original_exit,
    original_launch,
    marker,
    replacement,
    command_result,
    stopped,
    members,
    active_attempts,
    leases,
    current_authority,
):
    assert current_authority and active_attempts == 0 and leases == 0
    assert set(members) == set(plan["roles"])
    for role, row in members.items():
        assert row["all_calls_stopped"] and row["scope_matches"]
        assert (
            row["task_id"] == row["installed_task_id"]
            and row["issue_id"] == row["receipt_issue_id"]
        )
        assert row["task_id"] and row["issue_id"] and row["permission_ref"]
    assert original_exit["actual_handle"] and original_exit["pid"] == original_launch["pid"]
    assert original_exit["start_identity"] == original_launch["start_identity"]
    if closed is not None:
        assert original_exit["returncode"] == 0 and original_exit["actual_handle"]
        assert closed["ok"] and closed["outcome"] == "confirmed"
        assert closed["data"]["state"] == "owner_closed"
        assert all(
            r.get("state") not in {"unknown", "uncertain"} and r.get("quiescent") is not False
            for r in closed["data"]["shutdown"]
        )
        return "graceful"
    assert original_exit["actual_handle"] and original_exit["returncode"] == 71
    assert original_exit["pid"] == marker["pid"] and original_exit["start_identity"]
    assert marker["exit_code"] == 71 and marker["physical_author_stopped"]
    assert marker["task_id"] == members["c_author"]["task_id"]
    assert marker["operation_key"] == plan["operation_keys"]["c"]
    assert replacement["returncode"] == 0 and type(replacement["signal_via_retained_Popen"]) is bool
    assert replacement["actual_child_pid"] == command_result["actual_child_pid"]
    assert command_result["actual_child_handle_retained"]
    assert stopped["ok"] and stopped["outcome"] == "confirmed"
    data = stopped["data"]
    assert data["workflow"] == marker["workflow_ref"] and data["state"] == "done"
    assert data["quiescent"] is True
    assert all(
        data["components"].get(k) is True
        for k in ["native", "commands", "merge", "harness", "collectors"]
    )
    return "crash_cold"


def verified_protection(identities, protected, recovered):
    assert isinstance(protected, list)
    expected = {"c_author": identities["c_author"]}
    expected.update({x["role"]: (x["task_id"], x["issue_id"]) for x in recovered})
    found = {}
    for row in protected:
        role = row["role"]
        actual = (row["task_id"], row["issue_id"])
        assert role in identities and actual == tuple(identities[role])
        assert role not in found
        found[role] = actual
    assert all(found.get(role) == tuple(value) for role, value in expected.items())
    return {value[1] for value in found.values()}


def capture_rejection(factory, journal):
    from ai_task_manager.adapters.hosts.bridge.conformance.models import cases
    from ai_task_manager.config.files import digest

    gateway = getattr(factory, "gateway", None)
    rows = journal._history("trace") if journal is not None else []
    submitted = [
        (i, row)
        for i, row in enumerate(rows)
        if row["payload"].get("effect") == "native_probe_submission"
    ]
    result = {"case_id": None, "native_frames": [], "gateway": [], "joined_tools": []}
    if not submitted:
        return result
    index, request = submitted[-1]
    context = request["payload"]
    result.update(
        case_id=context["case_id"],
        request_ref=request["ref"],
        invocation_ref=context["invocation_ref"],
    )
    case = next(c for c in cases(gateway.context) if c.case_id == context["case_id"])
    assert hashlib.sha256(case.prompt.encode()).hexdigest() == context["prompt_sha256"]
    result["expected_arguments"] = case.arguments

    def tools(value, ref):
        if isinstance(value, dict):
            kind = value.get("type")
            if kind == "tool_use":
                result["native_frames"].append(
                    {
                        "ref": ref,
                        "type": kind,
                        "id": value.get("id"),
                        "name": value.get("name"),
                        "input": value.get("input"),
                    }
                )
            elif kind == "tool_result":
                result["native_frames"].append(
                    {
                        "ref": ref,
                        "type": kind,
                        "tool_use_id": value.get("tool_use_id"),
                        "is_error": value.get("is_error"),
                        "content": value.get("content")
                        if len(json.dumps(value.get("content"))) <= 4096
                        else None,
                        "content_sha256": hashlib.sha256(
                            json.dumps(value.get("content"), sort_keys=True).encode()
                        ).hexdigest(),
                    }
                )
            for item in value.values():
                tools(item, ref)
        elif isinstance(value, list):
            for item in value:
                tools(item, ref)

    for row in rows[index + 1 :]:
        payload = row["payload"]
        if payload.get("effect") == "native_probe_submission":
            break
        if (
            payload.get("frame_kind") == "native_frame"
            and payload.get("process") == context["process"]
        ):
            tools(payload.get("frame", {}), row["ref"])
        if (
            payload.get("effect") == "native_probe_gateway"
            and payload.get("case_id") == case.case_id
            and payload.get("probe") == gateway.context.reference
        ):
            result["gateway"].append(
                {
                    "ref": row["ref"],
                    **{
                        k: payload.get(k)
                        for k in ["case_id", "tool", "arguments_hash", "outcome", "result_hash"]
                    },
                }
            )
    result["native_frames"] = result["native_frames"][-64:]
    result["joined_tools"] = [
        {
            "tool_use": use,
            "tool_results": [
                r
                for r in result["native_frames"]
                if r["type"] == "tool_result" and r.get("tool_use_id") == use.get("id")
            ],
            "gateway_decisions": [
                g
                for g in result["gateway"]
                if use.get("name", "").removeprefix("mcp__aitm__") == g.get("tool")
                and digest(use.get("input")) == g.get("arguments_hash")
            ],
        }
        for use in result["native_frames"]
        if use["type"] == "tool_use"
    ]
    result["gateway_crossed"] = bool(result["gateway"])
    return result


class ReceiptObserver:
    def __init__(self, code, path):
        self.code, self.path = code, Path(path)
        lines, start = inspect.getsourcelines(code)
        self.rejection_line = next(
            start + i
            for i, text in enumerate(lines)
            if 'raise AccessDenied("Dependency receipt was superseded")' in text
        )
        self.rows = []
        self.closed = False
        self.lock = threading.RLock()
        self.tool = next(i for i in (5, 4, 3) if sys.monitoring.get_tool(i) is None)
        sys.monitoring.use_tool_id(self.tool, "dp06-exact-dependency-refusal")
        assert sys.monitoring.get_events(self.tool) == 0
        sys.monitoring.register_callback(self.tool, sys.monitoring.events.LINE, self.record)
        sys.monitoring.set_local_events(self.tool, code, sys.monitoring.events.LINE)

    def record(self, code, line):
        try:
            if code is not self.code or line != self.rejection_line:
                return
            with self.lock:
                if len(self.rows) >= 16:
                    return
                frame = sys._getframe(1)
                if frame.f_code is not self.code:
                    return
                local = frame.f_locals
                snapshot = local.get("snapshot")
                head = local.get("row")
                callers = []
                caller = frame.f_back
                for _ in range(4):
                    if caller is None:
                        break
                    callers.append(
                        {
                            "source": caller.f_code.co_filename,
                            "function": caller.f_code.co_name,
                            "line": caller.f_lineno,
                        }
                    )
                    caller = caller.f_back
                row = {
                    "at": time.time(),
                    "thread": threading.get_ident(),
                    "rejected_reference": local.get("reference"),
                    "source_revision": getattr(snapshot, "source_revision", None),
                    "current_generation": head[0] if head else None,
                    "task_id": getattr(snapshot, "task_id", None),
                    "callers": callers,
                    "last_boundary": (
                        "Signed receipt/context validated; "
                        "current generation rejected before snapshot return"
                    ),
                }
                del frame, local, snapshot, head, caller
                self.rows.append(row)
                self.save()
        except BaseException:
            pass

    def save(self):
        self.path.write_text(
            json.dumps(
                {
                    "scope": (
                        "Exact installed DependencyReceipts.verify_snapshot rejection LINE only; "
                        "no retained frame or authority key"
                    ),
                    "events": self.rows,
                    "closed": self.closed,
                    "global_events": 0,
                },
                indent=2,
            )
            + "\n"
        )
        self.path.chmod(0o600)

    def close(self):
        if self.closed:
            return
        sys.monitoring.set_local_events(self.tool, self.code, 0)
        assert sys.monitoring.get_events(self.tool) == 0
        sys.monitoring.register_callback(self.tool, sys.monitoring.events.LINE, None)
        sys.monitoring.free_tool_id(self.tool)
        self.closed = True
        self.save()


def candidate_names(plan, group):
    names = tuple(plan["candidate_slots"][group])
    assert names and len(names) <= 4 and len(set(names)) == len(names)
    assert names[0] == "code_candidate"
    assert all(n == "code_candidate" or n == "code_candidate_standby" for n in names)
    return names


def group_names(plan, group):
    return ("author", "code_worker", *candidate_names(plan, group))


def preparation_roles(plan):
    names = ["a_author", "c_author", "b_author"]
    names.extend(g + "_" + n for g in "abc" for n in group_names(plan, g) if n != "author")
    assert len(names) == len(set(names)) and set(names) == set(plan["roles"])
    return (*names, "coordination")


def review_stage(name):
    assert name in {"code_worker", "code_candidate", "code_candidate_standby"}
    return "code_worker" if name == "code_worker" else "code_candidate"


def review_bindings(reviews, names):
    assert set(reviews) == {"code_worker", *names}
    pointers = [reviews[n] for n in names]
    assert len({p["path"] for p in pointers}) == len(pointers)
    return {
        "code_worker": reviews["code_worker"],
        "code_candidate": pointers if len(pointers) > 1 else pointers[0],
    }


def target_only_resume(
    event,
    *,
    original_key_matches,
    source_unchanged,
    target_changed,
    publication_steps,
    graph_ready,
    current_authority,
    standby_ready,
    elapsed,
    wait_seconds,
    lineage_count,
    already_resumed,
):
    assert event.get("state") == "blocked"
    assert event.get("detail") == "Source or target changed during immutable preparation"
    assert original_key_matches and source_unchanged and target_changed
    assert not publication_steps and graph_ready and current_authority and standby_ready
    assert elapsed < 600 and wait_seconds < 120 and lineage_count == 0
    assert not already_resumed
    return True
