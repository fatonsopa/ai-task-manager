"""An operator selection of an existing application user, never a credential."""

from dataclasses import asdict, dataclass

from .errors import LinearError
from .models import canonical_id


@dataclass(frozen=True)
class AgentConnection:
    connection_id: str
    workspace_id: str
    principal_id: str
    logical_session: str
    agent_id: str

    def __post_init__(self):
        for value in (self.connection_id, self.workspace_id, self.principal_id, self.agent_id):
            canonical_id(value)
        if (
            not isinstance(self.logical_session, str)
            or not self.logical_session
            or len(self.logical_session) > 256
            or self.agent_id == self.principal_id
        ):
            raise LinearError("invalid_agent_connection")

    @property
    def digest(self):
        from .receipts import digest

        return digest(asdict(self))

    def check_scope(self, scope):
        if (scope.provider, scope.workspace, scope.principal, scope.logical_session) != (
            "linear",
            self.workspace_id,
            self.principal_id,
            self.logical_session,
        ):
            raise LinearError("agent_connection_scope_mismatch")
