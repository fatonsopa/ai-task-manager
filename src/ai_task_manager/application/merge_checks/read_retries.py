"""Finite backpressure for completed zero-effect reads, outside store transactions."""

import time
from dataclasses import dataclass
from math import isfinite
from typing import Protocol

from ai_task_manager.domains.work.errors import ValidationError
from ai_task_manager.storage.checks import CheckCommitBusy, CheckStoreBusy


class ParentBudget(Protocol):
    def remaining_seconds(self) -> float:
        """Return trusted remaining invocation time, not a model-selected deadline."""
        ...


@dataclass(frozen=True)
class ReadRetryPolicy:
    max_attempts: int
    max_elapsed_seconds: float
    delay_seconds: float

    def __post_init__(self):
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ValidationError("Read attempts must be a finite positive integer")
        if any(
            type(v) not in {int, float} or not isfinite(v) or v <= 0
            for v in (self.max_elapsed_seconds, self.delay_seconds)
        ):
            raise ValidationError("Read elapsed and delay limits must be finite and positive")


class ReadRetryExhausted(CheckStoreBusy):
    def __init__(self, reason, attempts, elapsed):
        super().__init__("Read backpressure exhausted: " + reason)
        self.reason, self.attempts, self.elapsed = reason, attempts, elapsed


class CheckReads:
    """Default single-attempt reads. There is no implicit retry loop."""

    def __init__(self, store):
        self.store = store

    def ensure_parent(self):
        pass

    def _invoke(self, operation, after_wait):
        return operation()

    def read(self, ref, scope, *, after_wait):
        return self._invoke(lambda: self.store.read(ref, scope), after_wait)

    def proofs(self, plan, unit, *, after_wait):
        return self._invoke(lambda: self.store.proofs(plan, unit), after_wait)


class RetryingReads(CheckReads):
    """Trusted composition configures the policy, parent budget, clock and sleeper.

    Only read/proofs are exposed. Calls have already rolled back and closed before
    a busy exception reaches this consumer. A time budget prevents subsequent
    admission after expiry; it is not a hard bound on filesystem I/O or scheduling.
    """

    def __init__(
        self,
        store,
        policy: ReadRetryPolicy,
        parent: ParentBudget,
        *,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        super().__init__(store)
        if type(policy) is not ReadRetryPolicy or not callable(
            getattr(parent, "remaining_seconds", None)
        ):
            raise ValidationError("Read retry needs trusted finite policy and parent budget")
        self.policy, self.parent, self.clock, self.sleep = policy, parent, clock, sleep

    def _parent_remaining(self):
        remaining = self.parent.remaining_seconds()
        if type(remaining) not in {int, float} or not isfinite(remaining):
            raise ValidationError("Parent time budget is unavailable or unbounded")
        return remaining

    def ensure_parent(self):
        if self._parent_remaining() <= 0:
            raise ReadRetryExhausted("parent_deadline", 0, 0)

    def _invoke(self, operation, after_wait):
        started, attempts = self.clock(), 0
        while True:
            elapsed = self.clock() - started
            if self._parent_remaining() <= 0:
                raise ReadRetryExhausted("parent_deadline", attempts, elapsed)
            if elapsed >= self.policy.max_elapsed_seconds:
                raise ReadRetryExhausted("elapsed", attempts, elapsed)
            attempts += 1
            try:
                result = operation()
            except CheckStoreBusy as error:
                if isinstance(error, CheckCommitBusy):
                    raise
                elapsed = self.clock() - started
                if attempts >= self.policy.max_attempts:
                    raise ReadRetryExhausted("attempts", attempts, elapsed) from error
                wait = min(
                    self.policy.delay_seconds,
                    self.policy.max_elapsed_seconds - elapsed,
                    self._parent_remaining(),
                )
                if wait <= 0:
                    raise ReadRetryExhausted("time_budget", attempts, elapsed) from error
                self.sleep(wait)
                self.ensure_parent()
                if self.clock() - started >= self.policy.max_elapsed_seconds:
                    raise ReadRetryExhausted("elapsed", attempts, self.clock() - started) from error
                after_wait()
            else:
                self.ensure_parent()
                if self.clock() - started >= self.policy.max_elapsed_seconds:
                    raise ReadRetryExhausted("elapsed", attempts, self.clock() - started)
                return result
