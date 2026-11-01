"""Inspect and stop an invocation-created POSIX group without losing its leader anchor."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Any

from .values import ControlError

TERM_GRACE_SECONDS = 2.0
KILL_CONFIRM_SECONDS = 2.0
POLL_SECONDS = 0.02


@dataclass(frozen=True)
class ProcessMember:
    pid: int
    group: int
    state: str
    start_identity: str

    @property
    def live(self) -> bool:
        return not self.state.startswith(("Z", "X"))


@dataclass(frozen=True)
class GroupObservation:
    known: bool
    members: tuple[ProcessMember, ...] = ()
    reason: str | None = None

    @property
    def live_members(self) -> tuple[ProcessMember, ...]:
        return tuple(member for member in self.members if member.live)

    def record(self) -> dict[str, Any]:
        return {
            "known": self.known,
            "reason": self.reason,
            "members": [asdict(member) for member in self.members],
            "live_pids": [member.pid for member in self.live_members] if self.known else None,
        }


@dataclass(frozen=True)
class LeaderObservation:
    known: bool
    exited: bool = False
    returncode: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class GroupStop:
    confirmed: bool
    observation: GroupObservation
    signals: tuple[str, ...] = ()
    reason: str | None = None


def _inventory() -> GroupObservation:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,pgid=,stat=,lstart="],
            capture_output=True,
            text=True,
            timeout=2,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return GroupObservation(False, reason=f"process_inventory:{type(error).__name__}")
    if result.returncode or not result.stdout.strip():
        return GroupObservation(False, reason="process_inventory_unavailable")
    members = []
    for line in result.stdout.splitlines():
        try:
            pid, group, state, start = line.split(None, 3)
            members.append(ProcessMember(int(pid), int(group), state, " ".join(start.split())))
        except (ValueError, TypeError):
            return GroupObservation(False, reason="process_inventory_incomplete")
    return GroupObservation(True, tuple(members))


def require_group_monitoring() -> None:
    required = ("waitid", "WNOWAIT", "WNOHANG", "WEXITED", "P_PID", "getpgid", "killpg")
    if not all(hasattr(os, name) for name in required):
        raise ControlError(
            "process_monitor_unavailable", "Waitable owned-group monitoring is required."
        )
    try:
        # The current process is not its own child. ECHILD confirms that these flags are supported.
        os.waitid(os.P_PID, os.getpid(), os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        pass
    except OSError as error:
        raise ControlError(
            "process_monitor_unavailable", "Non-reaping child monitoring is unavailable."
        ) from error
    if not _inventory().known:
        raise ControlError("process_monitor_unavailable", "Process-group inventory is unavailable.")


class OwnedProcessGroup:
    """The unreaped invocation leader pins ownership until all signal attempts end."""

    def __init__(self, process: subprocess.Popen[bytes]):
        self.process = process
        self.group_id = process.pid
        self.start_identity: str | None = None
        self.initial: GroupObservation | None = None

    def capture(self) -> GroupObservation:
        self.initial = self.observe()
        return self.initial

    def leader(self) -> LeaderObservation:
        if self.process.returncode is not None:
            return LeaderObservation(False, reason="leader_already_reaped")
        try:
            status = os.waitid(os.P_PID, self.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if self.group_id == os.getpgrp():
                return LeaderObservation(False, reason="leader_group_changed")
            # macOS reports ESRCH for getpgid(zombie), although waitid keeps that child
            # waitable. Its unreaped PID still anchors ownership; ps verifies its group.
            if status is None and os.getpgid(self.process.pid) != self.group_id:
                return LeaderObservation(False, reason="leader_group_changed")
        except (OSError, ChildProcessError) as error:
            return LeaderObservation(False, reason=f"leader_ownership:{type(error).__name__}")
        if status is None:
            return LeaderObservation(True)
        if status.si_pid != self.process.pid:
            return LeaderObservation(False, reason="unexpected_child_identity")
        if status.si_code == os.CLD_EXITED:
            return LeaderObservation(True, True, status.si_status)
        if status.si_code in (os.CLD_KILLED, os.CLD_DUMPED):
            return LeaderObservation(True, True, -status.si_status)
        return LeaderObservation(False, reason="unrecognized_child_exit")

    def observe(self) -> GroupObservation:
        leader = self.leader()
        if not leader.known:
            return GroupObservation(False, reason=leader.reason)
        inventory = _inventory()
        if not inventory.known:
            return inventory
        anchor = next(
            (member for member in inventory.members if member.pid == self.process.pid), None
        )
        if anchor is None or anchor.group != self.group_id:
            return GroupObservation(False, reason="leader_missing_from_inventory")
        if self.start_identity is not None and anchor.start_identity != self.start_identity:
            return GroupObservation(False, reason="leader_start_identity_changed")
        self.start_identity = anchor.start_identity
        return GroupObservation(
            True, tuple(member for member in inventory.members if member.group == self.group_id)
        )

    def _signal(self, value: signal.Signals) -> bool:
        # No signal is sent after reaping or loss of the original waitable-child anchor.
        if not self.leader().known:
            return False
        inventory = _inventory()
        if inventory.known and self.start_identity is not None:
            anchor = next(
                (member for member in inventory.members if member.pid == self.process.pid), None
            )
            if (
                anchor is None
                or anchor.group != self.group_id
                or anchor.start_identity != self.start_identity
            ):
                return False
        try:
            os.killpg(self.group_id, value)
            return True
        except (OSError, ProcessLookupError):
            return False

    def _confirm_until(self, deadline: float) -> GroupObservation:
        while True:
            observation = self.observe()
            if observation.known and not observation.live_members:
                return observation
            if time.monotonic() >= deadline:
                return observation
            time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))

    def stop(self) -> GroupStop:
        signals = []
        observation = self.observe()
        if observation.known and not observation.live_members:
            return GroupStop(True, observation)
        if self._signal(signal.SIGTERM):
            signals.append("SIGTERM")
        else:
            return GroupStop(False, observation, reason="group_ownership_unconfirmed")
        observation = self._confirm_until(time.monotonic() + TERM_GRACE_SECONDS)
        if observation.known and not observation.live_members:
            return GroupStop(True, observation, tuple(signals))
        if self._signal(signal.SIGKILL):
            signals.append("SIGKILL")
        else:
            return GroupStop(False, observation, tuple(signals), "group_ownership_unconfirmed")
        observation = self._confirm_until(time.monotonic() + KILL_CONFIRM_SECONDS)
        confirmed = observation.known and not observation.live_members
        return GroupStop(
            confirmed, observation, tuple(signals), None if confirmed else "group_stop_unconfirmed"
        )

    def reap_exited_leader(self) -> int | None:
        observation = self.leader()
        if not observation.known or not observation.exited:
            return None
        # The actual exit was read from waitid; reaping happens only after supervision/cleanup.
        self.process.wait(timeout=0.5)
        return observation.returncode

    def evidence(self, stop: GroupStop, *, orphaned: bool) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "leader_pid": self.process.pid,
            "leader_start_identity": self.start_identity,
            "orphaned": orphaned,
            "confirmed_stop": stop.confirmed,
            "signals": list(stop.signals),
            "observation": stop.observation.record(),
            "reason": stop.reason,
        }


def supervise(group: OwnedProcessGroup, *, timeout_seconds: float) -> dict[str, Any]:
    initial = group.initial or group.capture()
    if not initial.known:
        stop = group.stop()
        return {
            "outcome": "unknown",
            "reason": initial.reason,
            "returncode": group.reap_exited_leader(),
            "group": group.evidence(stop, orphaned=False),
        }
    deadline = time.monotonic() + timeout_seconds
    while True:
        leader = group.leader()
        if not leader.known:
            stop = group.stop()
            return {
                "outcome": "unknown",
                "reason": leader.reason,
                "returncode": group.reap_exited_leader(),
                "group": group.evidence(stop, orphaned=False),
            }
        if leader.exited:
            observation = group.observe()
            if not observation.known:
                stop = group.stop()
                outcome, reason, orphaned = "unknown", observation.reason, False
            elif observation.live_members:
                stop = group.stop()
                orphaned = True
                outcome = "failed" if stop.confirmed else "unknown"
                reason = f"orphaned_process_group:leader_exit={leader.returncode}"
            else:
                stop = GroupStop(True, observation)
                orphaned = False
                outcome, reason = (
                    ("succeeded", None)
                    if leader.returncode == 0
                    else ("failed", f"exit:{leader.returncode}")
                )
            return {
                "outcome": outcome,
                "reason": reason,
                "returncode": group.reap_exited_leader(),
                "group": group.evidence(stop, orphaned=orphaned),
            }
        if time.monotonic() >= deadline:
            stop = group.stop()
            return {
                "outcome": "interrupted",
                "reason": "timeout",
                "returncode": group.reap_exited_leader(),
                "group": group.evidence(stop, orphaned=False),
            }
        time.sleep(min(POLL_SECONDS, max(0, deadline - time.monotonic())))
