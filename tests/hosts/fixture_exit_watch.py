"""Test-only kernel witnesses and exact owned-relay recovery, never product authority."""

import os
import select
import signal
import subprocess
import time


def inventory():
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,stat=,lstart="],
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
        env={"PATH": os.defpath, "LC_ALL": "C"},
    )
    rows = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) != 5:
            raise AssertionError("Fixture inventory is incomplete")
        pid, parent, group = map(int, parts[:3])
        rows[pid] = {
            "pid": pid,
            "parent": parent,
            "group": group,
            "state": parts[3],
            "start": " ".join(parts[4].split()),
        }
    return rows


class FixtureExitWatch:
    def __init__(self, process, record):
        self.process, self.record = process, record
        identity = process.verify_identity()
        self.native, self.relay = identity.pid, process.process.pid
        self.group = identity.group_id
        self.exits, self.queue, self.unknown = {}, None, None
        before = inventory()
        self.original = {pid: before[pid] for pid in (self.native, self.relay)}
        assert self.original[self.native]["start"] == identity.start_identity
        assert self.original[self.relay]["start"] == process.group.start_identity
        self.current()
        try:
            self.queue = select.kqueue()
            assert not os.get_inheritable(self.queue.fileno())
            changes = [
                select.kevent(
                    pid,
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                    fflags=select.KQ_NOTE_EXIT,
                )
                for pid in self.original
            ]
            self._accept(self.queue.control(changes, 2, 0))
            self.current()
            assert not self.read(), "Fixture generation exited during registration"
            self.record("registered", {"original": self.original, "exits": self.exits})
        except BaseException:
            self.close()
            raise

    def _accept(self, events):
        for event in events:
            if (
                event.ident not in self.original
                or event.filter != select.KQ_FILTER_PROC
                or event.flags & select.KQ_EV_ERROR
                or not event.fflags & select.KQ_NOTE_EXIT
            ):
                self.unknown = "kernel_event_unknown"
                raise AssertionError(self.unknown)
            self.exits.setdefault(
                event.ident,
                {
                    "pid": event.ident,
                    "flags": event.flags,
                    "fflags": event.fflags,
                    "data": event.data,
                },
            )

    def read(self):
        if self.unknown or self.queue is None or self.queue.closed:
            raise AssertionError("Fixture kernel witness is unavailable")
        try:
            self._accept(self.queue.control(None, 2, 0))
        except BaseException:
            self.unknown = "kernel_poll_unknown"
            raise
        return dict(self.exits)

    def current(self):
        assert self.process.identity.pid == self.native
        assert self.process.process.pid == self.relay
        assert self.process.process.returncode is None
        # waitid proves this remains our direct child without consuming its state.
        status = os.waitid(os.P_PID, self.relay, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        assert status is None or status.si_pid == self.relay
        rows = inventory()
        members = {pid: row for pid, row in rows.items() if row["group"] == self.group}
        assert set(members) <= set(self.original)
        relay = members[self.relay]
        assert relay["start"] == self.original[self.relay]["start"]
        assert relay["parent"] == os.getpid()
        if self.native in members:
            native = members[self.native]
            assert native["start"] == self.original[self.native]["start"]
            assert native["parent"] == self.relay
        else:
            assert self.native in self.read()
        return members

    def stop_parent_and_exit_native(self):
        self.current()
        assert not self.read()
        os.kill(self.relay, signal.SIGSTOP)
        until = time.monotonic() + 3
        while True:
            members = self.current()
            if members[self.relay]["state"].startswith("T"):
                break
            assert time.monotonic() < until, "Owned relay stop was not observed"
            time.sleep(0.01)
        assert not self.read()
        assert not members[self.native]["state"].startswith(("Z", "X"))
        self.record("before_native_term", {"members": members, "exits": self.exits})
        os.kill(self.native, signal.SIGTERM)
        until = time.monotonic() + 3
        while self.native not in self.read():
            assert time.monotonic() < until, "Native kernel exit was not observed"
            time.sleep(0.01)
        assert self.relay not in self.exits
        self.record("native_exit_relay_stopped", {"members": self.current(), "exits": self.exits})

    def recover(self):
        try:
            exits = self.read()
            if self.relay not in exits:
                members = self.current()
                assert self.relay not in self.read()
                if members[self.relay]["state"].startswith("T"):
                    self.record("before_exact_sigcont", {"members": members, "exits": self.exits})
                    os.kill(self.relay, signal.SIGCONT)
            until = time.monotonic() + 3
            while set(self.read()) != set(self.original):
                assert time.monotonic() < until, "Both owned kernel exits remain unconfirmed"
                time.sleep(0.01)
            rows = inventory()
            members = [row for row in rows.values() if row["group"] == self.group]
            assert not any(not row["state"].startswith(("Z", "X")) for row in members)
            # The kernel events establish actual exit; wait for the same original
            # direct child's terminal waitid state before the ordinary group reap.
            if self.process._closed:
                assert self.process.stop_evidence.confirmed
            else:
                until = time.monotonic() + 3
                while not self.process.group.leader().exited:
                    assert time.monotonic() < until, "Original terminal child anchor is unknown"
                    time.sleep(0.01)
                # Product shutdown keeps its original unknown receipt. Manual
                # fixture recovery uses the exact retained group after both
                # registered generations have exited; it does not retry or
                # rewrite the product transport's stop observation.
                prior = self.process.stop_evidence
                stop = self.process.group.stop()
                assert stop.confirmed
                self.process.group.reap_exited_leader()
                self.process.native_input.close()
                self.process.native_exit.close()
                self.process.process.stdout.close()
                if self.process._relay_ready_fd is not None:
                    os.close(self.process._relay_ready_fd)
                    self.process._relay_ready_fd = None
                assert self.process.stop_evidence is prior
            result = {
                "confirmed": True,
                "kernel_exits": self.exits,
                "members_before_original_reap": members,
                "scope": "Manual fixture recovery, not a product stop repair",
            }
            self.record("manual_recovery_confirmed", result)
            return result
        except BaseException as error:
            result = {
                "confirmed": False,
                "reason": type(error).__name__,
                "kernel_exits": self.exits,
                "original": self.original,
                "held": True,
            }
            self.record("held_unknown_no_sweep", result)
            return result

    def close(self):
        if self.queue is not None:
            self.queue.close()
