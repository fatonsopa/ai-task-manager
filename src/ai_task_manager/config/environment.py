"""Native environment names cannot also select provider credentials."""

from types import MappingProxyType

_COMMON = frozenset({"PATH", "HOME", "USER", "LANG"})
NATIVE_ENVIRONMENT_KEYS = MappingProxyType(
    {
        "claude": _COMMON | {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_ENTRYPOINT"},
        "codex": _COMMON | {"CODEX_HOME"},
    }
)
RESERVED_CREDENTIAL_ENVIRONMENT_NAMES = frozenset().union(*NATIVE_ENVIRONMENT_KEYS.values())
