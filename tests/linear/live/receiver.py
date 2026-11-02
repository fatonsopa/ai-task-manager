"""Bounded loopback HTTP shell; closing a response never cancels adapter work."""

import hashlib
import io
import math
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from uuid import uuid4

from .ledger import DriverRefused


class HeaderLimit(ValueError):
    pass


class DeadlineReader(io.RawIOBase):
    def __init__(self, connection):
        self.connection = connection
        super().__init__()

    def readable(self):
        return True

    def readinto(self, buffer):
        connection = self.connection
        remaining = connection.work_remaining
        if remaining <= 0:
            connection.read_timed_out = True
            raise TimeoutError()
        connection.raw.settimeout(min(connection.read_timeout, remaining))
        try:
            count = connection.raw.recv_into(buffer)
        except TimeoutError:
            connection.read_timed_out = True
            raise
        if count and not connection.header_complete:
            connection.header_prefix.extend(memoryview(buffer)[:count])
            end = connection.header_prefix.find(b"\r\n\r\n")
            if end >= 0:
                if end + 4 > 65536:
                    raise HeaderLimit()
                connection.header_complete = True
                connection.header_prefix.clear()
            elif len(connection.header_prefix) > 65536:
                raise HeaderLimit()
        return count


class DeadlineConnection:
    """Supported socket/RawIO interfaces with one accept-relative deadline."""

    def __init__(self, raw, accepted_at, owner):
        self.raw, self.accepted_at = raw, accepted_at
        self.deadline = accepted_at + owner.response_budget
        self.work_deadline = self.deadline - owner.output_reserve
        self.read_timeout = owner.read_timeout
        self.header_prefix = bytearray()
        self.header_complete = self.read_timed_out = False
        self.lease = None

    def __getattr__(self, name):
        return getattr(self.raw, name)

    @property
    def work_remaining(self):
        return max(0.0, self.work_deadline - time.monotonic())

    @property
    def hard_remaining(self):
        return max(0.0, self.deadline - time.monotonic())

    def makefile(self, mode, buffering=-1):
        if mode != "rb":
            raise ValueError("unsupported_owned_stream")
        return io.BufferedReader(DeadlineReader(self), io.DEFAULT_BUFFER_SIZE)

    def sendall(self, data):
        remaining = self.hard_remaining
        if remaining <= 0:
            raise TimeoutError()
        self.raw.settimeout(remaining)
        self.raw.sendall(data)

    def interrupt(self):
        try:
            self.raw.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def close(self):
        self.header_prefix.clear()
        self.raw.close()


class Lease:
    def __init__(self, server, connection):
        self.server, self.connection, self.id = server, connection, str(uuid4())
        self.handler_done, self.adapter_done, self.watcher_done = False, True, False
        self.released = self.timed_out = self.timeout_recorded = False
        self.status = None
        self.handler = self.adapter = self.watcher = None
        self.ready, self.response_done = threading.Event(), threading.Event()
        self.response_lock = threading.Lock()
        self.response_claimed = self.response_completed = False
        self.response_status = self.response_started_at = self.response_completed_at = None
        self.response_finished_at = self.adapter_started_at = None

    def finish(self, role):
        with self.server.lock:
            setattr(self, role + "_done", True)

    def respond(self, status, body=b""):
        connection = self.connection
        if not self.response_lock.acquire(timeout=connection.hard_remaining):
            connection.interrupt()
            return False
        if self.response_claimed:
            self.response_lock.release()
            return False
        self.response_claimed = True
        failed = False
        try:
            if connection.work_remaining <= 0:
                status, body = 503, b""
                self.timed_out = True
            if connection.hard_remaining <= 0:
                connection.interrupt()
                failed = True
            else:
                status = int(status)
                try:
                    phrase = HTTPStatus(status).phrase
                except ValueError:
                    phrase = "Response"
                header = (
                    f"HTTP/1.0 {status} {phrase}\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                ).encode("ascii")
                self.response_status, self.response_started_at = status, time.monotonic()
                try:
                    connection.sendall(header + body)
                    completed = time.monotonic()
                    if completed <= connection.deadline:
                        self.response_completed = True
                        self.response_completed_at = completed
                    else:
                        failed = True
                        connection.interrupt()
                except OSError:
                    failed = True
                    connection.interrupt()
        finally:
            self.response_finished_at = time.monotonic()
            self.response_done.set()
            self.response_lock.release()
        # Persistence is outside the response lock and after transport completion.
        if failed:
            self.server.owner.record(
                "reply_unconfirmed", request_id=self.id, code="response_write_failed"
            )
        return self.response_completed

    def note_timeout(self):
        with self.server.lock:
            if self.timeout_recorded:
                return
            self.timeout_recorded = True
            pending = not self.adapter_done
        fields = {"request_id": self.id, "active": self.server.owner.active}
        if self.response_completed and self.response_status == 503:
            fields["status"] = 503
        if pending:
            fields["code"] = "underlying_outcome_pending"
        self.server.owner.record("http_wait_elapsed", **fields)

    def watch(self):
        try:
            if not self.response_done.wait(self.connection.work_remaining):
                self.timed_out = True
                self.respond(503)
                if not self.response_done.wait(self.connection.hard_remaining):
                    self.connection.interrupt()
                # Wake an input reader; this never interrupts the adapter thread.
                self.connection.interrupt()
                self.note_timeout()
        finally:
            self.finish("watcher")


class Server(HTTPServer):
    allow_reuse_address = False

    def __init__(self, owner, address):
        self.owner = owner
        self.slots = threading.BoundedSemaphore(owner.capacity)
        self.lock = threading.RLock()
        self.leases, self.handlers, self.adapters, self.watchers = [], [], [], []
        super().__init__(address, Handler, bind_and_activate=False)

    def get_request(self):
        raw, address = super().get_request()
        accepted_at = time.monotonic()  # Immediately after the actual socket.accept.
        connection = DeadlineConnection(raw, accepted_at, self.owner)
        self.reap()
        if self.owner.stopping or not self.slots.acquire(blocking=False):
            try:
                connection.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            connection.close()
            self.owner.record("capacity_refused", status=503, active=self.owner.active)
            raise OSError("owned_capacity_refused")
        lease = Lease(self, connection)
        connection.lease = lease
        watcher = threading.Thread(target=lease.watch, name="owned-deadline-" + lease.id)
        lease.watcher = watcher
        with self.lock:
            self.leases.append(lease)
            self.watchers.append(watcher)
        try:
            watcher.start()
        except BaseException:
            lease.response_done.set()
            lease.finish("handler")
            lease.finish("watcher")
            connection.close()
            self.owner.record("server_failed", code="server_exception")
            raise OSError("owned_watcher_not_started") from None
        return connection, address

    def process_request(self, request, client_address):
        lease = request.lease
        if request.work_remaining <= 0:
            lease.respond(503)
            self.shutdown_request(request)
            lease.finish("handler")
            return

        def handle():
            try:
                try:
                    self.owner.ledger.request("receiver_http")
                except Exception:
                    lease.respond(503)
                    self.owner.record(
                        "admission_refused", code="request_budget_or_store_unavailable"
                    )
                    return
                if request.work_remaining <= 0 or lease.response_claimed or self.owner.stopping:
                    lease.respond(503)
                    return
                self.RequestHandlerClass(request, client_address, self, lease=lease)
            except BaseException:
                lease.respond(500)
                self.owner.record("handler_failed", request_id=lease.id, code="handler_exception")
            finally:
                self.shutdown_request(request)
                lease.finish("handler")

        thread = threading.Thread(target=handle, name="owned-http-" + lease.id)
        lease.handler = thread
        with self.lock:
            self.handlers.append(thread)
        try:
            thread.start()
        except BaseException:
            lease.respond(500)
            self.shutdown_request(request)
            lease.finish("handler")
            self.owner.record("handler_failed", request_id=lease.id, code="handler_exception")

    def reap(self):
        # A worker cannot attest its own thread exit. Release only from an observer.
        with self.lock:
            for lease in self.leases:
                threads = [lease.handler, lease.adapter, lease.watcher]
                if (
                    lease.handler_done
                    and lease.adapter_done
                    and lease.watcher_done
                    and not lease.released
                    and not any(thread is not None and thread.is_alive() for thread in threads)
                ):
                    lease.released = True
                    self.slots.release()

    def service_actions(self):
        self.reap()

    def handle_error(self, request, client_address):
        self.owner.record("server_failed", code="server_exception")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def __init__(self, *args, lease):
        self.lease = lease
        super().__init__(*args)

    def log_message(self, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self.respond(code)

    def parse_request(self):
        if not super().parse_request():
            return False
        if not self.connection.header_complete:
            self.respond(400)
            return False
        return True

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except HeaderLimit:
            self.respond(431)
        finally:
            if self.connection.read_timed_out and not self.lease.response_claimed:
                self.respond(408)

    def respond(self, status, body=b""):
        try:
            return self.lease.respond(status, body)
        finally:
            self.close_connection = True

    def do_GET(self):
        owner = self.server.owner
        if self.path != owner.health_route:
            self.respond(404)
        else:
            self.respond(200, ('{"run_id":"' + owner.ledger.run_id + '"}').encode())

    def do_PUT(self):
        self.respond(405)

    do_DELETE = do_PUT
    do_PATCH = do_PUT
    do_HEAD = do_PUT

    def do_POST(self):
        owner, lease = self.server.owner, self.lease
        if self.path != owner.route:
            self.respond(404)
            return
        headers = list(self.headers.raw_items())
        lengths = [value for key, value in headers if key.lower() == "content-length"]
        if (
            len(lengths) != 1
            or not lengths[0].isascii()
            or not lengths[0].isdecimal()
            or any(key.lower() == "transfer-encoding" for key, _ in headers)
        ):
            self.respond(400)
            return
        if len(lengths[0]) > 10:
            self.respond(413)
            return
        length = int(lengths[0])
        if length > owner.maximum:
            self.respond(413)
            return
        try:
            raw = self.rfile.read(length)
        except (TimeoutError, OSError):
            self.respond(408)
            return
        if len(raw) != length:
            self.respond(400)
            return
        owner.record(
            "request_received",
            request_id=lease.id,
            size=len(raw),
            raw_sha256=hashlib.sha256(raw).hexdigest(),
        )
        if self.connection.work_remaining <= 0 or lease.response_claimed or owner.stopping:
            self.respond(503)
            return
        started = time.monotonic()
        wait_deadline = min(self.connection.work_deadline, started + owner.http_wait)
        with self.server.lock:
            lease.adapter_done = False

        def apply():
            status = 500
            try:
                if self.connection.work_remaining <= 0 or lease.response_claimed or owner.stopping:
                    status = 503
                else:
                    lease.adapter_started_at = time.monotonic()
                    response = owner.adapter.receive("POST", headers, raw)
                    status = response.status
                    if type(status) is not int or not 100 <= status <= 599:
                        status = 500
            except BaseException:
                status = 500
                owner.record("adapter_failed", request_id=lease.id, code="adapter_exception")
            finally:
                with self.server.lock:
                    lease.status = status
                    late = time.monotonic() >= wait_deadline
                # A returned adapter result and a stopped adapter thread are
                # separate facts. Diagnostics may still be blocked afterward.
                lease.ready.set()
                owner.record(
                    "adapter_finished",
                    request_id=lease.id,
                    underlying_status=status,
                    late=late,
                    seconds=time.monotonic() - started,
                )
                lease.finish("adapter")

        worker = threading.Thread(target=apply, name="owned-adapter-" + lease.id)
        lease.adapter = worker
        with self.server.lock:
            self.server.adapters.append(worker)
        try:
            worker.start()
        except BaseException:
            lease.finish("adapter")
            raise
        if lease.ready.wait(max(0.0, wait_deadline - time.monotonic())):
            self.respond(lease.status)
        else:
            lease.timed_out = True
            self.respond(503)
            lease.note_timeout()


class BoundedReceiver:
    def __init__(
        self,
        adapter,
        ledger,
        *,
        host="127.0.0.1",
        port=0,
        capacity=4,
        http_wait=4.0,
        read_timeout=2.0,
        maximum=1048576,
        response_budget=4.5,
        output_reserve=0.25,
    ):
        if host != "127.0.0.1" or type(port) is not int or not 0 <= port <= 65535:
            raise DriverRefused("loopback_binding_required")
        if type(capacity) is not int or not 1 <= capacity <= 4:
            raise DriverRefused("invalid_capacity")
        if (
            any(
                type(value) not in {int, float} or not math.isfinite(value) or value <= 0
                for value in (http_wait, read_timeout, response_budget, output_reserve)
            )
            or http_wait > 4
            or read_timeout > 2
            or response_budget > 4.5
            or output_reserve >= response_budget
            or type(maximum) is not int
            or not 0 < maximum <= 1048576
        ):
            raise DriverRefused("invalid_http_limits")
        self.adapter, self.ledger = adapter, ledger
        self.capacity, self.http_wait, self.read_timeout = capacity, http_wait, read_timeout
        self.response_budget, self.output_reserve, self.maximum = (
            response_budget,
            output_reserve,
            maximum,
        )
        self.route, self.health_route = "/hooks/linear/" + ledger.run_id, "/health/" + ledger.run_id
        self.stopping, self.started = False, False
        self.diagnostic_failures = 0
        self.thread = self.shutdown_thread = None
        self.server = Server(self, (host, port))

    def record(self, kind, **fields):
        try:
            self.ledger.observe(kind, **fields)
        except Exception:
            with self.server.lock:
                self.diagnostic_failures += 1

    @property
    def active(self):
        self.server.reap()
        with self.server.lock:
            return sum(not lease.released for lease in self.server.leases)

    @property
    def address(self):
        if not self.started:
            raise DriverRefused("receiver_not_started")
        return self.server.server_address

    def start(self):
        if self.started or self.stopping:
            raise DriverRefused("receiver_not_startable")
        self.server.server_bind()
        self.server.server_activate()
        self.started = True
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.02), name="owned-http-accept"
        )
        self.thread.start()
        return self.address

    def stop(self, timeout=12.0):
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout < 0:
            raise DriverRefused("invalid_stop_bound")
        end = time.monotonic() + timeout
        self.stopping = True
        if self.started and self.shutdown_thread is None:
            self.shutdown_thread = threading.Thread(
                target=self.server.shutdown, name="owned-http-shutdown"
            )
            self.shutdown_thread.start()
        threads = [
            self.thread,
            self.shutdown_thread,
            *self.server.handlers,
            *self.server.adapters,
            *self.server.watchers,
        ]
        for thread in threads:
            if (
                thread is not None
                and thread is not threading.current_thread()
                and thread.ident is not None
            ):
                thread.join(max(0, end - time.monotonic()))
        if self.shutdown_thread is None or not self.shutdown_thread.is_alive():
            self.server.server_close()
        threads = [
            self.thread,
            self.shutdown_thread,
            *self.server.handlers,
            *self.server.adapters,
            *self.server.watchers,
        ]
        live = [thread.name for thread in threads if thread is not None and thread.is_alive()]
        return {
            "stopped": not live and self.active == 0,
            "active_requests": self.active,
            "live_workers": live,
            "diagnostic_failures": self.diagnostic_failures,
        }
