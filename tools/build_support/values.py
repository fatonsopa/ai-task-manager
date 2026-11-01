"""Typed build input validation and exact money/time values."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

MAX_SECONDS = 48 * 60 * 60
MAX_BUDGET_MICRO = 100000000
ACTIVE_WORKER_STATES = ("reserved", "running", "uncertain")


class ControlError(Exception):
    """A refused operation, with a stable category for callers."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise ControlError(code, message)


def _text(value: str, name: str) -> str:
    _require(
        isinstance(value, str) and bool(value.strip()), "invalid_input", f"{name} is required."
    )
    return value


def _identifier(value: str, name: str) -> str:
    _require(
        isinstance(value, str) and bool(IDENTIFIER.fullmatch(value)),
        "invalid_input",
        f"{name} must be a bounded identifier.",
    )
    return value


def _utc(value: datetime | str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    except ValueError as error:
        raise ControlError(
            "invalid_input", "Use an ISO timestamp with an explicit timezone."
        ) from error
    _require(
        isinstance(parsed, datetime)
        and parsed.tzinfo is not None
        and (parsed.utcoffset() is not None),
        "invalid_input",
        "Timestamps need a timezone.",
    )
    return parsed.astimezone(UTC)


def _money(value: str | int | float | Decimal) -> int:
    _require(
        not isinstance(value, bool), "invalid_input", "Money must be a finite nonnegative amount."
    )
    try:
        amount = Decimal(str(value))
        scaled = amount * 1000000
        valid = amount.is_finite() and amount >= 0 and (scaled == scaled.to_integral_value())
    except (InvalidOperation, ValueError) as error:
        raise ControlError("invalid_input", "Money needs at most six decimal places.") from error
    _require(
        valid and scaled <= 9223372036854775807,
        "invalid_input",
        "Money must be finite, nonnegative, and exact to six places within storage limits.",
    )
    return int(scaled)


def _usd(micro: int) -> str:
    return f"{Decimal(micro) / 1000000:.6f}"


def validate_worker_cap(cap: int) -> None:
    _require(
        type(cap) is int and 0 < cap <= 1000000,
        "invalid_input",
        "Worker cap must be a positive finite integer.",
    )
