"""Failures that callers must distinguish from an empty result."""


class WorkError(Exception):
    pass


class ValidationError(WorkError):
    pass


class AccessDenied(WorkError):
    pass


class NotFound(AccessDenied):
    pass


class InvalidTransition(WorkError):
    pass


class StaleVersion(WorkError):
    pass


class StaleDecision(StaleVersion):
    pass


class CapacityExceeded(WorkError):
    pass


class IdempotencyConflict(WorkError):
    pass


class ApprovalRequired(WorkError):
    pass


class EvidenceRequired(WorkError):
    pass


class SchemaError(WorkError):
    pass
