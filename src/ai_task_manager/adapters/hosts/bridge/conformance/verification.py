"""Reconstruct fixed tool facts from owned native frames, never model verdicts."""

import hashlib

from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied

from ..admission import MANAGED_TOOLS
from .profile import profile_tools, profile_case_ids


def require(value, reason):
    if not value:
        raise AccessDenied(reason)


def frame(journal, ref, owner):
    record = journal._read(ref, "trace", fresh=False)
    p = record["payload"]
    require(
        p.get("frame_kind") == "native_frame"
        and p.get("process") == owner["process"]
        and all(
            p.get(k) == owner["binding"][k]
            for k in ("attempt_id", "worker_id", "configuration_digest")
        ),
        "Conformance needs actual same-owner native frames",
    )
    f = p.get("frame")
    require(
        isinstance(f, dict) and p.get("raw_frame_hash") == digest(f),
        "Conformance frame projection is incomplete or changed",
    )
    return record, f


def startup(journal, ref, owner, profile):
    record = journal._read(ref, "startup", fresh=False)
    p = record["payload"]
    require(
        all(p.get(k) == v for k, v in owner["binding"].items())
        and p.get("process_id") == owner["process"]["pid"]
        and p.get("process_start_identity") == owner["process"]["start_identity"]
        and p.get("model") == profile["model"]
        and p.get("requested_effort") == profile["effort"],
        "Native startup does not match conformance owner/profile",
    )
    require(profile["host"] == "claude", "Complete Codex native inventory is not yet observed")
    initial, auth = [], []
    for source in record["sources"]:
        r = journal._read(source, fresh=False)
        if r["payload"].get("auth_method"):
            auth.append(r["payload"])
        elif r["payload"].get("frame", {}).get("subtype") == "init":
            _, f = frame(journal, source, owner)
            initial.append(f)
    require(len(auth) == len(initial) == 1, "Complete actual auth and init sources required")
    a, f = auth[0], initial[0]
    require(
        a.get("auth_method") == "claude.ai"
        and a.get("account_hash") == profile["account_hash"]
        and a.get("configuration_digest") == owner["binding"]["configuration_digest"],
        "Native subscription account differs",
    )
    require(
        f.get("apiKeySource") == "none"
        and f.get("claude_code_version") == profile.get("cli_version", "2.1.301")
        and f.get("model") == profile["model"]
        and f.get("session_id") == p["session_id"]
        and set(f.get("tools", [])) == profile_tools(profile)
        and f.get("mcp_servers") == [{"name": "aitm", "source": "sdk", "status": "connected"}],
        "The complete restricted native inventory was not observed",
    )
    return p["session_id"]


def identity_at(journal, reference, owner, session, at):
    record = journal._read(reference, fresh=False)
    p = record["payload"]
    require(
        record["expires_at"] is not None and record["observed_at"] <= at < record["expires_at"],
        "Native identity was not fresh for the actual probe effect",
    )
    if record["kind"] == "startup":
        require(
            all(p.get(k) == v for k, v in owner["binding"].items())
            and p.get("process_id") == owner["process"]["pid"]
            and p.get("process_start_identity") == owner["process"]["start_identity"]
            and p.get("session_id") == session,
            "Historical identity belongs to another probe owner",
        )
    else:
        require(
            p.get("effect") == "native_identity_refreshed"
            and p.get("binding") == owner["binding"]
            and p.get("process") == owner["process"]
            and p.get("session_id") == session,
            "Only an actual same-native identity observation can cover the probe",
        )


def terminal_identity(journal, before, records, terminal_ref, owner, session, startup_ref, account):
    """Select recorded effect provenance; this does not establish current authority."""
    terminal, _ = frame(journal, terminal_ref, owner)
    at = terminal["observed_at"]
    reference = before
    for record in records:
        p = record["payload"]
        if (
            record["kind"] == "trace"
            and p.get("effect") == "native_identity_refreshed"
            and p.get("binding") == owner["binding"]
            and p.get("process") == owner["process"]
            and p.get("session_id") == session
            and p.get("original_startup_ref") == startup_ref
            and p.get("account_hash") == account
            and record["expires_at"] is not None
            and record["observed_at"] <= at < record["expires_at"]
        ):
            reference = record["ref"]
    identity_at(journal, reference, owner, session, at)
    return reference


def verify_case(journal, case, value, owner, context_ref, session, model):
    request = journal._read(value["request_ref"], "trace", fresh=False)
    q = request["payload"]
    terminal, f = frame(journal, value["terminal_ref"], owner)
    require(
        q.get("effect") == "native_probe_submission"
        and q.get("case_id") == case.case_id
        and q.get("prompt_sha256") == hashlib.sha256(case.prompt.encode()).hexdigest()
        and q.get("session_id") == session
        and q.get("invocation_ref") == value["invocation_ref"]
        and q.get("binding") == owner["binding"]
        and q.get("process") == owner["process"],
        "Actual scoped accounted probe submission is missing",
    )
    require(
        f.get("type") == "result"
        and f.get("subtype") == "success"
        and f.get("is_error") is False
        and f.get("session_id") == session
        and request["observed_at"] <= terminal["observed_at"],
        "A matching successful actual terminal is required",
    )
    identity_at(journal, value["identity_before"], owner, session, request["observed_at"])
    identity_at(journal, value["identity_after"], owner, session, terminal["observed_at"])
    calls = []
    for ref in value["frame_refs"]:
        r, raw = frame(journal, ref, owner)
        require(
            request["observed_at"] <= r["observed_at"] <= terminal["observed_at"],
            "Native case frame lies outside its submitted turn",
        )
        require(
            raw.get("subtype") != "model_refusal_fallback", "Native model fallback was observed"
        )
        for observed in (raw.get("model"), raw.get("message", {}).get("model")):
            require(observed is None or observed == model, "Native case model changed")
        if raw.get("type") == "assistant":
            require(raw.get("session_id") == session, "Native case session changed")
            for item in raw.get("message", {}).get("content", []):
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    require(item.get("name") in MANAGED_TOOLS, "Native tool bypass was observed")
                    calls.append((item["name"].removeprefix("mcp__aitm__"), item.get("input")))
    refs = value["gateway_refs"]
    if case.kind == "inventory":
        require(
            not calls and not refs and value["before"] == value["after"],
            "Native inventory challenge produced an unexpected effect",
        )
        return
    require(
        calls == [(case.tool, case.arguments)] and len(refs) == 1,
        "An actual exact tool call and dispatch result are required",
    )
    event = journal._read(refs[0], "trace", fresh=False)["payload"]
    require(
        event.get("effect") == "native_probe_gateway"
        and event.get("probe") == context_ref
        and event.get("case_id") == case.case_id
        and event.get("tool") == case.tool
        and event.get("arguments_hash") == digest(case.arguments)
        and event.get("before") == value["before"]
        and event.get("after") == value["after"],
        "Actual canary dispatch and artifact observations differ",
    )
    if case.kind == "deny":
        require(
            event.get("outcome") == "denied"
            and value["before"] == value["after"]
            and event.get("result_hash") == digest(None),
            "The forbidden canary effect was not denied",
        )
    else:
        nonce = digest(context_ref)
        read = {"text": nonce, "sha256": hashlib.sha256(nonce.encode()).hexdigest()}
        expected = {
            "read_file": read,
            "write_file": {"sha256": read["sha256"]},
            "run_command": {"command_ref": "canary-check", "input": read},
            "acknowledge_event": {"acknowledged": "probe"},
        }[case.tool]
        require(
            event.get("outcome") == "succeeded" and event.get("result_hash") == digest(expected),
            "The actual canary result differs from the registered operation",
        )
        expected_after = dict(value["before"])
        if case.tool == "write_file":
            expected_after["output.txt"] = read["sha256"]
        require(value["after"] == expected_after, "An undeclared canary artifact changed")


def verify_capture(journal, reference):
    """Historical capability facts are not fresh identity or work permission."""
    from pathlib import Path

    from ai_task_manager.domains.work.models import Scope

    from .models import ProbeContext, cases
    from .profile import StableProfile

    record = journal._read(reference, "trace", fresh=False)
    p = record["payload"]
    require(
        p.get("version") == 2
        and p.get("effect") == "native_conformance_capture"
        and p.get("error_type") is None,
        "A completed version2 native capture is required",
    )
    profile = StableProfile.decode(p["profile"])
    require(p["profile_digest"] == profile.fingerprint, "Historical capability profile changed")
    data = dict(p["provenance"]["context"])
    data["scope"] = Scope(**data["scope"])
    data["fixture_root"] = Path(data["fixture_root"])
    data["fixture_identity"] = tuple(data["fixture_identity"])
    data["case_ids"] = tuple(data["case_ids"])
    context = ProbeContext(**data)
    require(
        context.profile_digest == profile.fingerprint
        and context.installation_digest == p["provenance"]["manifest_digest"]
        and context.deadline == p["provenance"]["probe_deadline"]
        and (context.host, context.model, context.effort, context.account_hash)
        == (profile.host, profile.model, profile.effort, profile.account_hash),
        "The original immutable capture provenance changed",
    )
    stop = p.get("stop", {})
    require(
        p.get("confirmed_stop") is True
        and stop.get("confirmed") is True
        and stop.get("observation", {}).get("known") is True
        and all(
            row.get("state", "").startswith("Z") for row in stop["observation"].get("members", [])
        ),
        "Native probe stop remains unknown",
    )
    require(
        context.case_ids == profile_case_ids(profile)
        and [v.get("case_id") for v in p.get("cases", [])] == list(profile_case_ids(profile)),
        "The frozen capability matrix is incomplete",
    )
    session = startup(journal, p["startup_ref"], p["owner"], vars(profile))
    for case, value in zip(cases(context), p["cases"], strict=True):
        verify_case(journal, case, value, p["owner"], context.reference, session, profile.model)
    return record, profile, context
