"""Product credential namespace and native host environment names."""

from types import MappingProxyType

# This namespace is consumed only by explicit operator credential lookup.
CREDENTIAL_ENVIRONMENT_NAME_PATTERN = r"AITM_CREDENTIAL_[A-Za-z0-9_]+"

_COMMON = frozenset({"PATH", "HOME", "USER", "LANG"})
NATIVE_ENVIRONMENT_KEYS = MappingProxyType(
    {
        "claude": _COMMON | {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_ENTRYPOINT"},
        "codex": _COMMON | {"CODEX_HOME"},
    }
)
