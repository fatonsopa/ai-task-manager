"""Replay signed owned Codex sources and controller-correlated thread binding."""

from .codex_profile import CodexProfile, ProfileViolation


def binding_sources(rows, result, response_ref, request_ref, request_id):
    """Only a declared thread/start and its actual owned response can bind a profile."""
    if not isinstance(request_ref, str) or not isinstance(response_ref, str):
        raise ProfileViolation("native_profile_binding_source_missing")
    by_ref = {row["ref"]: (index, row) for index, row in enumerate(rows)}
    request = by_ref.get(request_ref)
    response = by_ref.get(response_ref)
    if request is None or response is None or request[0] >= response[0]:
        raise ProfileViolation("native_profile_binding_source_missing")
    declared, frame = request[1]["payload"], response[1]["payload"].get("frame")
    if (
        type(request_id) is not int
        or declared.get("effect") != "startup_request_declared"
        or declared.get("method") != "thread/start"
        or type(declared.get("request_id")) is not int
        or declared.get("request_id") != request_id
        or not isinstance(frame, dict)
        or frame.get("id") != request_id
        or type(frame.get("id")) is not int
        or "method" in frame
        or "error" in frame
        or not isinstance(frame.get("result"), dict)
        or frame["result"] != result
    ):
        raise ProfileViolation("native_profile_binding_source_mismatch")
    return result


def replay_profile(rows, requested_model, requested_effort, cwd):
    state = CodexProfile(requested_model, requested_effort, cwd)
    seen = []
    for row in rows:
        data = row["payload"]
        if data.get("effect") == "model_stability_refused":
            raise ProfileViolation("native_profile_has_durable_refusal")
        if isinstance(data.get("frame"), dict):
            state.observe(data["frame"])
        elif data.get("effect") == "codex_profile_bound":
            request_ref, response_ref = data.get("request_ref"), data.get("response_ref")
            if row["sources"] != [request_ref, response_ref]:
                raise ProfileViolation("native_profile_binding_sources_changed")
            sources = {source["ref"]: source for source in seen}
            response = sources.get(response_ref)
            if response is None:
                raise ProfileViolation("native_profile_binding_source_missing")
            result = response["payload"].get("frame", {}).get("result")
            if not isinstance(result, dict):
                raise ProfileViolation("native_profile_binding_source_malformed")
            state.bind(
                binding_sources(seen, result, response_ref, request_ref, data.get("request_id"))
            )
        seen.append(row)
    return state
