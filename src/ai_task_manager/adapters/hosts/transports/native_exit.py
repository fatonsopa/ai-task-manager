"""Kernel-bound exit observations; registration is separate from admission polling."""

import select
import sys
from dataclasses import dataclass
from threading import RLock

from ai_task_manager.domains.work.errors import AccessDenied
from ai_task_manager.supervisor.models import RuntimeUncertain

from ..bridge.records import ProcessIdentity


def require_exit_monitoring():
    if sys.platform != "darwin" or not all(
        hasattr(select, name)
        for name in ("kqueue", "kevent", "KQ_FILTER_PROC", "KQ_NOTE_EXIT", "KQ_EV_ERROR")
    ):
        raise RuntimeUncertain("This native host lacks a verified kernel exit monitor")


@dataclass(frozen=True)
class NativeGeneration:
    identity: ProcessIdentity
    relay_pid: int
    relay_start_identity: str


class NativeExitWatch:
    def __init__(self, generation, queue):
        self.generation, self._queue = generation, queue
        self._identities = {generation.identity.pid, generation.relay_pid}
        self._exited, self._unknown, self._closed = set(), None, False
        self._lock = RLock()

    @classmethod
    def capture(cls, process):
        """Only the trusted process owner calls this outside any Core transaction."""
        require_exit_monitoring()
        before = process.verify_identity()
        generation = NativeGeneration(before, process.process.pid, process.group.start_identity)
        if (
            before.pid <= 0
            or before.pid == generation.relay_pid
            or before.group_id != generation.relay_pid
            or not generation.relay_start_identity
        ):
            raise RuntimeUncertain("Exact native and relay generations are unavailable")
        try:
            queue = select.kqueue()
        except OSError as error:
            raise RuntimeUncertain("Kernel exit registration is unavailable") from error
        watch = cls(generation, queue)
        try:
            watch._accept(
                queue.control(
                    [
                        select.kevent(
                            pid,
                            filter=select.KQ_FILTER_PROC,
                            flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                            fflags=select.KQ_NOTE_EXIT,
                        )
                        for pid in watch._identities
                    ],
                    2,
                    0,
                )
            )
            after = process.verify_identity()
            watch.check_current(after, process.process.pid, process.group.start_identity)
            return watch
        except (OSError, ValueError) as error:
            watch.close()
            raise RuntimeUncertain("Kernel exit registration could not be confirmed") from error
        except BaseException:
            # A partial registration never grants authority. Its descriptors can
            # close without claiming that the actual process/group has stopped.
            watch.close()
            raise

    def _accept(self, events):
        for event in events:
            if (
                event.ident not in self._identities
                or event.filter != select.KQ_FILTER_PROC
                or event.flags & select.KQ_EV_ERROR
                or not event.fflags & select.KQ_NOTE_EXIT
            ):
                self._unknown = "invalid_kernel_observation"
                raise RuntimeUncertain("Native kernel exit observation is unknown")
            self._exited.add(event.ident)

    def check_current(self, identity, relay_pid, relay_start_identity):
        """Poll existing handles at timeout zero; never search or rebind a PID."""
        with self._lock:
            if (
                self._closed
                or self._unknown
                or self._queue.closed
                or NativeGeneration(identity, relay_pid, relay_start_identity) != self.generation
            ):
                self._unknown = "closed_unknown_or_changed_generation"
                raise RuntimeUncertain("Current native exit ownership is unavailable")
            if self._exited:
                raise AccessDenied("The registered native or relay process exited")
            try:
                self._accept(self._queue.control(None, 2, 0))
            except OSError as error:
                self._unknown = "kernel_poll_failed"
                raise RuntimeUncertain("Native kernel exit polling failed") from error
            if self._exited:
                raise AccessDenied("The registered native or relay process exited")

    @property
    def closed(self):
        return self._closed

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._queue.close()
