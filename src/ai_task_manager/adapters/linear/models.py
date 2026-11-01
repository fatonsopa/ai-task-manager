import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from .errors import LinearError


def canonical_id(value):
    try:
        result = str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise LinearError("invalid_canonical_identity") from exc
    if value != result:
        raise LinearError("invalid_canonical_identity")
    return result


def revision(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("timezone missing")
        return stamp.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, TypeError, AttributeError) as exc:
        raise LinearError("invalid_provider_revision") from exc


@dataclass(frozen=True)
class Credentials:
    value: str = field(repr=False)
    kind: str = "api_key"

    def authorization(self):
        if (
            not self.value
            or any(c in self.value for c in "\r\n")
            or self.kind not in {"api_key", "oauth"}
        ):
            raise LinearError("invalid_credentials")
        return f"Bearer {self.value}" if self.kind == "oauth" else self.value


@dataclass(frozen=True)
class ExpectedBinding:
    viewer_id: str
    workspace_id: str
    team_id: str
    team_key: str
    project_id: str


@dataclass(frozen=True)
class VerifiedBinding:
    expected: ExpectedBinding
    states: dict[str, str]


@dataclass(frozen=True)
class IssueSnapshot:
    id: str
    identifier: str
    team_id: str
    project_id: str
    assignee_id: str | None
    title: str
    body: str
    state_id: str
    status: str
    revision: str


@dataclass(frozen=True)
class WireResponse:
    status: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes = b""


@dataclass(frozen=True)
class IssueCreateCapability:
    schema_digest: str

    @classmethod
    def from_introspection(cls, result):
        try:
            fields = {item["name"]: item["type"] for item in result["inputFields"]}
            if result["name"] != "IssueCreateInput" or fields["id"] != {
                "kind": "SCALAR",
                "name": "String",
                "ofType": None,
            }:
                raise ValueError()
            if (
                not {"title", "description", "teamId", "projectId", "assigneeId", "stateId"}
                <= fields.keys()
            ):
                raise ValueError()
        except (KeyError, TypeError, ValueError) as exc:
            raise LinearError("unsupported_creation_uuid_capability") from exc
        return cls(hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest())


@dataclass(frozen=True)
class CommentSnapshot:
    id: str
    body: str
    issue: IssueSnapshot
    revision: str
    author_id: str | None = None


@dataclass(frozen=True)
class CommentCreateCapability:
    schema_digest: str

    @classmethod
    def from_introspection(cls, result):
        try:
            fields = {item["name"]: item["type"] for item in result["inputFields"]}
            if (
                result["name"] != "CommentCreateInput"
                or fields["id"] != {"kind": "SCALAR", "name": "String", "ofType": None}
                or not {"issueId", "body"} <= fields.keys()
            ):
                raise ValueError()
        except (KeyError, TypeError, ValueError) as exc:
            raise LinearError("unsupported_comment_uuid_capability") from exc
        return cls(hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest())
