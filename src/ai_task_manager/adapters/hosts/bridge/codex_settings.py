"""One trusted configuration plan and one secret-omitting native response projection."""

import json
import tomllib
from dataclasses import dataclass

from ai_task_manager.config.files import digest
from ai_task_manager.domains.work.errors import AccessDenied

from .codex_home import CodexHomeBoundary

RESTRICTIONS = {
    "model_provider": "openai",
    "agents.enabled": False,
    "features.multi_agent": False,
    "features.shell_tool": False,
    "tools.update_plan.enabled": False,
    "features.apps": False,
    "features.plugins": False,
    "features.hooks": False,
    "features.goals": False,
    "features.image_generation": False,
    "features.view_image": False,
    "features.browser_use": False,
    "features.computer_use": False,
    "features.code_mode_host": False,
    "features.sleep_tool": False,
    "web_search": "disabled",
    "approval_policy": "never",
    "approvals_reviewer": "user",
    "sandbox_mode": "read-only",
    "notify": [],
    "project_doc_max_bytes": 0,
}
CONFIG_METHODS = frozenset({"config/read", "configRequirements/read", "mcpServerStatus/list"})


def _value(data, key):
    for part in key.split("."):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    # Arbitrary compound values are never echoed into a trace.
    if key in RESTRICTIONS:
        expected = RESTRICTIONS[key]
        return data if type(data) is type(expected) and data == expected else None
    return data if type(data) in {str, bool, int} or data is None else None


def project_response(method, frame):
    """Return a labelled whitelist projection, never arbitrary config/header/env maps."""
    result = frame.get("result")
    identity = frame.get("id") if type(frame.get("id")) is int else None
    if not isinstance(result, dict) or "error" in frame:
        return {"id": identity, "error": {"classification": "native_config_error"}}
    if method == "config/read":
        config = result.get("config")
        projected = {key: _value(config, key) for key in RESTRICTIONS}
        projected.update(
            {
                "forced_login_method": _value(config, "forced_login_method")
                if _value(config, "forced_login_method") in {"chatgpt", "api"}
                else None,
                "model_hash": digest(_value(config, "model")),
                "model_reasoning_effort": _value(config, "model_reasoning_effort")
                if _value(config, "model_reasoning_effort")
                in {"none", "minimal", "low", "medium", "high", "xhigh"}
                else None,
            }
        )
        layers = result.get("layers")
        projected_layers = [] if isinstance(layers, list) else None
        if isinstance(layers, list):
            for layer in layers:
                if not isinstance(layer, dict) or not isinstance(layer.get("name"), dict):
                    projected_layers.append({"invalid": True})
                    continue
                name = layer["name"]
                projected_layers.append(
                    {
                        "type": name.get("type")
                        if name.get("type")
                        in {"user", "system", "project", "packagedDefaults", "sessionFlags"}
                        else None,
                        "file_hash": digest(name.get("file")),
                        "version_hash": digest(layer.get("version")),
                        "version_present": isinstance(layer.get("version"), str),
                    }
                )
        servers = config.get("mcp_servers") if isinstance(config, dict) else None
        return {
            "id": identity,
            "result": {
                "effective": projected,
                "layers": projected_layers,
                "origins_present": isinstance(result.get("origins"), dict),
                "servers": {
                    name: value.get("enabled")
                    if isinstance(value, dict) and type(value.get("enabled")) is bool
                    else None
                    for name, value in servers.items()
                }
                if isinstance(servers, dict)
                else None,
            },
        }
    if method == "configRequirements/read":
        req = result.get("requirements", "unknown")
        # Only a supported compatible subset is accepted; retain its exact safe fields.
        allowed = {
            "featureRequirements",
            "allowedApprovalPolicies",
            "allowedSandboxModes",
            "allowedWebSearchModes",
            "allowedApprovalsReviewers",
            "allowedLoginMethods",
            "modelProvider",
        }
        if req is None:
            projection = None
        elif isinstance(req, dict):
            projection = {"unsupported_keys": bool(set(req) - allowed), "requirements": {}}
            known_values = {
                "never",
                "on-request",
                "untrusted",
                "readOnly",
                "workspaceWrite",
                "dangerFullAccess",
                "disabled",
                "cached",
                "live",
                "user",
                "auto_review",
                "chatgpt",
                "api",
            }
            for key in allowed & req.keys():
                value = req[key]
                if key == "featureRequirements":
                    known_features = {
                        k.removeprefix("features.")
                        for k in RESTRICTIONS
                        if k.startswith("features.")
                    }
                    if (
                        not isinstance(value, dict)
                        or set(value) - known_features
                        or any(type(v) is not bool for v in value.values())
                    ):
                        projection["unsupported_keys"] = True
                    else:
                        projection["requirements"][key] = value
                elif key == "modelProvider":
                    projection["requirements"][key] = "openai" if value == "openai" else None
                elif isinstance(value, list) and all(
                    isinstance(v, str) and v in known_values for v in value
                ):
                    projection["requirements"][key] = value
                else:
                    projection["unsupported_keys"] = True
        else:
            projection = "unknown"
        return {"id": identity, "result": {"requirements": projection}}
    if method == "mcpServerStatus/list":
        data = result.get("data")
        return {
            "id": identity,
            "result": {
                "entries": len(data) if isinstance(data, list) else None,
                "nextCursor_hash": digest(result.get("nextCursor")),
                "cursor_present": "nextCursor" in result,
                "inventory_error": not isinstance(data, list)
                or any(
                    not isinstance(row, dict)
                    or row.get("toolsError") is not None
                    or row.get("runtimeStatus") is None
                    for row in data
                ),
            },
        }
    return {
        "id": frame.get("id") if type(frame.get("id")) is int else None,
        "error": {"classification": "unrequested_native_response"},
    }


@dataclass(frozen=True)
class CodexSettings:
    sources: object
    model: str
    effort: str
    instruction_sources: tuple[str, ...] = ()

    def __post_init__(self):
        if (
            not isinstance(self.model, str)
            or not self.model
            or self.effort != "high"
            or not isinstance(self.sources, CodexHomeBoundary)
            or not isinstance(self.instruction_sources, tuple)
            or any(not isinstance(path, str) for path in self.instruction_sources)
        ):
            raise AccessDenied("Codex requires an explicit canonical High profile")
        self.check()

    def check(self):
        self.sources.check()
        try:
            config = tomllib.loads(self.sources.read_config().decode())
        except (ValueError, UnicodeError):
            raise AccessDenied("Protected native configuration is invalid") from None
        if any(
            config.get(key) is not None
            for key in (
                "forced_login_method",
                "forced_chatgpt_workspace_id",
                "openai_base_url",
                "chatgpt_base_url",
                "model_providers",
            )
        ) or config.get("cli_auth_credentials_store") not in {None, "auto", "file"}:
            raise AccessDenied("Existing auth/provider constraints require separate observation")
        servers = config.get("mcp_servers", {})
        if not isinstance(servers, dict) or any(not isinstance(name, str) for name in servers):
            raise AccessDenied("Native MCP source inventory is unknown")
        return tuple(sorted(servers))

    @property
    def fingerprint(self):
        return digest(
            {
                "sources": self.sources.fingerprint,
                "model": self.model,
                "effort": self.effort,
                "restrictions": RESTRICTIONS,
                "servers": self.check(),
                "instructions": self.instruction_sources,
            }
        )

    def arguments(self, *, forced=False):
        values = {**RESTRICTIONS, "model": self.model, "model_reasoning_effort": self.effort}
        if forced:
            values["forced_login_method"] = "chatgpt"
        values.update(
            {"mcp_servers." + json.dumps(name) + ".enabled": False for name in self.check()}
        )
        return tuple(
            part for key, value in values.items() for part in ("-c", key + "=" + json.dumps(value))
        )
