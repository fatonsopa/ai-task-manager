from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class IssueStatus(StrEnum):
    BACKLOG = "Backlog"
    TODO = "Todo"
    IN_PROGRESS = "In Progress"
    ON_HOLD = "On Hold"
    IN_REVIEW = "In Review"
    IN_DEV = "In Dev"
    IN_STAGING = "In Staging"
    DONE = "Done"
    CANCELED = "Canceled"
    DUPLICATE = "Duplicate"


class Cause(StrEnum):
    ENQUEUE = "enqueue"
    DEQUEUE = "dequeue"
    START = "start"
    HOLD = "hold"
    RESUME = "resume"
    REQUEST_REVIEW = "request_review"
    REVIEW_DECISION = "review_decision"
    MERGE = "merge"
    STAGE = "stage"
    RELEASE = "release"
    NON_CODE_COMPLETE = "non_code_complete"
    CANCEL = "cancel"
    DUPLICATE = "duplicate"


class AttemptState(StrEnum):
    RESERVED = "reserved"
    STARTING = "starting"
    RUNNING = "running"
    VERIFYING = "verifying"
    MERGING = "merging"
    PAUSED = "paused"
    FAILED = "failed"
    CANCELED = "canceled"
    UNKNOWN = "unknown"
    DONE = "done"


ACTIVE_ATTEMPTS = frozenset(
    {
        AttemptState.RESERVED,
        AttemptState.STARTING,
        AttemptState.RUNNING,
        AttemptState.VERIFYING,
        AttemptState.MERGING,
        AttemptState.UNKNOWN,
    }
)
DELIVERED = frozenset({IssueStatus.IN_DEV, IssueStatus.IN_STAGING, IssueStatus.DONE})


class SyncState(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    UNCERTAIN = "uncertain"
    FAILED = "failed"


class ReceiptStage(StrEnum):
    QUEUED = "queued"
    DELIVERED = "delivered"
    AI_ACKNOWLEDGED = "ai_acknowledged"
    USER_REPORTED = "user_reported"


@dataclass(frozen=True)
class Scope:
    provider: str
    workspace: str
    principal: str
    project: str
    logical_session: str


@dataclass(frozen=True)
class SessionHandle:
    token: str = field(repr=False)


@dataclass(frozen=True)
class TaskProse:
    title: str
    body: str
    code_bearing: bool = True
    required_work: tuple[str, ...] = ("complete",)


@dataclass(frozen=True)
class ApprovalPolicy:
    writer_hash: str
    reference_hash: str
    style_hash: str


@dataclass(frozen=True)
class RenderedProse:
    title: str
    body: str
    representation: str


@dataclass(frozen=True)
class Approval:
    digest: str
    policy: ApprovalPolicy
    representation: str


@dataclass(frozen=True)
class Evidence:
    kind: str
    scope: Scope
    subject: str
    payload: dict[str, Any]
    expires_at: float | None = None


@dataclass(frozen=True)
class Task:
    id: str
    session_id: str
    title: str
    body: str
    prose_digest: str
    representation: str
    approval_ref: str
    code_bearing: bool
    required_work: tuple[str, ...]
    completed_work: tuple[str, ...] = ()
    issue_id: str | None = None
    observed_status: IssueStatus | None = None
    desired_status: IssueStatus = IssueStatus.BACKLOG
    sync_state: SyncState = SyncState.PENDING
    execution_state: str = "pending_intake"
    queued_once: bool = False
    delivered_once: bool = False
    parent_task_id: str | None = None
    version: int = 1
    decision_revision: int = 0
    decision_ref: str | None = None
    human_hold: bool = False


@dataclass(frozen=True)
class Attempt:
    id: str
    task_id: str
    session_id: str
    host: str
    state: AttemptState
    decision_revision: int
    worker_id: str | None = None
    worktree_id: str | None = None
    database_id: str | None = None
    version: int = 1


@dataclass(frozen=True)
class CapacityPolicy:
    host: str
    host_limit: int
    project_limit: int


@dataclass(frozen=True)
class Event:
    id: str
    session_id: str
    sequence: int
    task_id: str
    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class OutboundOperation:
    id: str
    session_id: str
    task_id: str
    kind: str
    payload: dict[str, Any]
    state: str
    attempt_count: int
    lease_owner: str | None
    lease_until: float | None
    error: str | None
    fenced_by: str | None = None


@dataclass(frozen=True)
class TransitionFacts:
    authorized: bool = False
    queued: bool = False
    worker_started: bool = False
    quiescent: bool = False
    reason: bool = False
    review_requested: bool = False
    complete: bool = False
    merged: bool = False
    staged: bool = False
    released: bool = False
    non_code_verified: bool = False
    duplicate_link: bool = False
    code_bearing: bool = True
