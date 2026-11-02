"""Bounded facts from owned sockets; observations never create HTTP success."""

import hashlib
import json
import threading
import time


def clock_rows(server):
    rows = []
    for lease in server.server.leases:
        connection = lease.connection
        rows.append(
            {
                "accepted_at": connection.accepted_at,
                "work_deadline": connection.work_deadline,
                "hard_deadline": connection.deadline,
                "response_status": lease.response_status,
                "response_started_at": lease.response_started_at,
                "response_completed": lease.response_completed,
                "response_completed_at": lease.response_completed_at,
                "response_finished_at": lease.response_finished_at,
                "adapter_started_at": lease.adapter_started_at,
                "handler_done": lease.handler_done,
                "adapter_done": lease.adapter_done,
                "watcher_done": lease.watcher_done,
                "response_claimed": lease.response_claimed,
                "response_done": lease.response_done.is_set(),
                "socket_descriptor": connection.fileno(),
                "read_timed_out": connection.read_timed_out,
                "workers_alive": [
                    thread.name
                    for thread in [lease.handler, lease.adapter, lease.watcher]
                    if thread is not None and thread.is_alive()
                ],
            }
        )
    return rows


class ReceiverTrace:
    def __init__(self, monkeypatch, module, *, maximum=512):
        self.maximum, self.events, self.dropped = maximum, [], 0
        self.lock = threading.Lock()
        trace = self

        class Socket:
            def __init__(self, raw):
                self.raw = raw

            def __getattr__(self, name):
                return getattr(self.raw, name)

            def settimeout(self, value):
                trace.record("socket_settimeout", value=value, fd=self.raw.fileno())
                return self.raw.settimeout(value)

            def shutdown(self, how):
                trace.record("socket_shutdown_begin", how=how, fd=self.raw.fileno())
                try:
                    value = self.raw.shutdown(how)
                except OSError as error:
                    trace.record(
                        "socket_shutdown_error", error=type(error).__name__, errno=error.errno
                    )
                    raise
                trace.record("socket_shutdown_complete", how=how, fd=self.raw.fileno())
                return value

            def close(self):
                trace.record("socket_close_begin", fd=self.raw.fileno())
                try:
                    return self.raw.close()
                finally:
                    trace.record("socket_close_complete", fd=self.raw.fileno())

        original_init = module.DeadlineConnection.__init__

        def initialize(connection, raw, accepted_at, owner):
            original_init(connection, Socket(raw), accepted_at, owner)
            trace.record(
                "accepted",
                accepted_at=accepted_at,
                work_deadline=connection.work_deadline,
                hard_deadline=connection.deadline,
                fd=connection.fileno(),
            )

        monkeypatch.setattr(module.DeadlineConnection, "__init__", initialize)

        original_send = module.DeadlineConnection.sendall

        def send(connection, data):
            trace.record(
                "send_begin",
                fd=connection.fileno(),
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                hard_remaining=connection.hard_remaining,
            )
            try:
                result = original_send(connection, data)
            except OSError as error:
                trace.record(
                    "send_error",
                    error=type(error).__name__,
                    errno=error.errno,
                    fd=connection.fileno(),
                    hard_remaining=connection.hard_remaining,
                )
                raise
            trace.record(
                "send_complete", fd=connection.fileno(), hard_remaining=connection.hard_remaining
            )
            return result

        monkeypatch.setattr(module.DeadlineConnection, "sendall", send)

        original_shutdown = module.Server.shutdown_request

        def shutdown(server, connection):
            lease = connection.lease
            trace.record(
                "shutdown_request_begin",
                fd=connection.fileno(),
                claimed=lease.response_claimed,
                response_done=lease.response_done.is_set(),
                response_finished_at=lease.response_finished_at,
                hard_remaining=connection.hard_remaining,
            )
            try:
                return original_shutdown(server, connection)
            finally:
                trace.record("shutdown_request_complete", fd=connection.fileno())

        monkeypatch.setattr(module.Server, "shutdown_request", shutdown)

        original_read = module.DeadlineReader.readinto

        def read(reader, buffer):
            trace.record("read_begin", work_remaining=reader.connection.work_remaining)
            try:
                result = original_read(reader, buffer)
            except OSError as error:
                trace.record(
                    "read_error",
                    error=type(error).__name__,
                    errno=error.errno,
                    work_remaining=reader.connection.work_remaining,
                )
                raise
            trace.record(
                "read_complete", size=result, work_remaining=reader.connection.work_remaining
            )
            return result

        monkeypatch.setattr(module.DeadlineReader, "readinto", read)

    def record(self, kind, **facts):
        with self.lock:
            if len(self.events) < self.maximum:
                self.events.append(
                    {
                        "kind": kind,
                        "mono": time.monotonic(),
                        "thread": threading.current_thread().name,
                        **facts,
                    }
                )
            else:
                self.dropped += 1

    def save(self, path, **facts):
        with self.lock:
            record = {
                "events": list(self.events),
                "events_dropped": self.dropped,
                "ordering_capture_complete": self.dropped == 0,
                **facts,
            }
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
