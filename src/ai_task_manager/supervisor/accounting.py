"""Launch authorization is configured by the controller, outside worker input."""

from .journal import digest
from .models import RuntimeRefused


def command_digest(command):
    return digest(
        {
            "argv": command.argv,
            "cwd": str(command.cwd),
            "environment": command.environment,
            "timeout": command.timeout_seconds,
            "host": command.host,
        }
    )


class MechanicalCommands:
    """Exact registered fixture commands only; this does not authorize provider spend."""

    def __init__(self, commands=()):
        self._allowed = frozenset(
            command_digest(command) for command in commands if command.host == "fixture"
        )

    def check(self, command):
        if command.host != "fixture" or command_digest(command) not in self._allowed:
            raise RuntimeRefused("No accounted launch authority exists for this exact command")
        if any(
            any(word in name.upper() for word in ("TOKEN", "SECRET", "API_KEY", "PASSWORD"))
            for name in command.environment
        ):
            raise RuntimeRefused("Mechanical fixture commands cannot receive credentials")
