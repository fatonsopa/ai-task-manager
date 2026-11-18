"""Finite trusted identity refresh; never an execution or human grant."""

import math
import threading
import time
from pathlib import Path

from ai_task_manager.domains.work.errors import AccessDenied

from .auth_probe import AUTH_OBSERVATION_MAX_SECONDS
from .identity import NativeIdentityObservations


class IdentityRefresh:
    def __init__(self, observations, root, maximum, *, on_failure=None, cancelled=None):
        if not isinstance(observations, NativeIdentityObservations):
            raise AccessDenied("A real same-native identity observer is required")
        if type(maximum) is not int or maximum < 1:
            raise AccessDenied("A finite reviewed identity invocation count is required")
        self.observations, self.root, self.maximum = observations, Path(root), maximum
        self._bound = (observations, self.root, maximum)
        self._on_failure = on_failure
        self._count, self._latency = 0, 0.0
        self._collect = threading.Lock()
        self._stop = cancelled if cancelled is not None else threading.Event()
        self._thread = None
        self._failure = None
        self._window_lock = threading.Lock()
        self._minimum_remaining = 0.0

    def ensure_current(self, *, minimum_remaining_seconds=0):
        if (
            type(minimum_remaining_seconds) not in {int, float}
            or not math.isfinite(minimum_remaining_seconds)
            or minimum_remaining_seconds < 0
        ):
            raise AccessDenied("A finite identity preparation window is required")
        with self._window_lock:
            self._minimum_remaining = max(self._minimum_remaining, minimum_remaining_seconds)
            required = self._minimum_remaining
        if (
            (self.observations, self.root, self.maximum) != self._bound
            or self._stop.is_set()
            or self._failure is not None
        ):
            raise AccessDenied("Identity refresh is stopped, failed or rebound")
        observer = self.observations
        prepared = observer.prepare(cancelled=self._stop)
        remaining = prepared.observation.expires_at - observer.journal.clock()
        # Refresh before the work observer's next full collection budget is
        # consumed, allowing the existing bounded auth operation to finish.
        threshold = max(
            15, 2 * self._latency + 2,
            required + AUTH_OBSERVATION_MAX_SECONDS + 2 if required else 0,
        )
        if prepared.observation.expires_at >= observer.execution_not_after or remaining > threshold:
            return prepared
        prepare = getattr(observer.native, "prepare_local_milestones", None)
        if callable(prepare):
            prepare()
        prepared = observer.prepare(cancelled=self._stop)
        if not self._collect.acquire(blocking=False):
            if not minimum_remaining_seconds:
                return prepared  # The old issued observation remains fresh during collection.
            while not self._collect.acquire(timeout=0.05):
                if self._stop.is_set() or observer.journal.clock() >= min(
                    prepared.observation.expires_at, observer.execution_not_after
                ):
                    raise AccessDenied("Identity preparation stopped or expired while waiting")
        try:
            if self._stop.is_set() or self._failure is not None:
                raise AccessDenied("Identity refresh stopped or failed during preparation")
            # A concurrent collector may already have supplied the needed window.
            prepared = observer.prepare(cancelled=self._stop)
            if prepared.observation.expires_at >= observer.execution_not_after or (
                prepared.observation.expires_at - observer.journal.clock() > threshold
            ):
                return prepared
            if self._count >= self.maximum:
                raise AccessDenied("The finite identity refresh invocation plan is exhausted")
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._count += 1
            started = time.monotonic()
            result = observer.refresh(self.root / str(self._count), cancelled=self._stop)
            self._latency = max(self._latency, time.monotonic() - started)
            if (
                result.observation.expires_at < observer.execution_not_after
                and result.observation.expires_at - observer.journal.clock()
                <= max(threshold, 2 * self._latency + 2)
            ):
                raise AccessDenied("Fresh identity leaves no safe next observation window")
            return result
        except BaseException as error:
            self._failure = type(error).__name__
            observer.revoke()
            raise
        finally:
            self._collect.release()

    def start(self):
        if self._thread is not None:
            raise AccessDenied("Identity refresh cannot be restarted")
        self.ensure_current()

        def run():
            while not self._stop.wait(1):
                try:
                    self.ensure_current()
                except BaseException as error:
                    if self._stop.is_set():
                        return
                    self._failure = type(error).__name__
                    self.observations.revoke()
                    if self._on_failure is not None:
                        self._on_failure(error)
                    return

        self._thread = threading.Thread(target=run, name="managed-identity-refresh", daemon=True)
        self._thread.start()

    def request_stop(self):
        self._stop.set()
        self.observations.revoke()

    def close(self):
        self.request_stop()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            remaining = max(
                0, self.observations.execution_not_after - self.observations.journal.clock()
            )
            thread.join(timeout=min(16, remaining))
        self.observations.revoke()
        return thread is None or not thread.is_alive()
