"""Bounded loopback HTTP shell; an HTTP timeout never cancels adapter work."""

import hashlib
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from uuid import uuid4

from .ledger import DriverRefused


class Lease:
    def __init__(self, server):
        self.server, self.id = server, str(uuid4())
        self.handler_done, self.adapter_done, self.released = False, True, False
        self.timed_out, self.status = False, None
        self.handler = self.adapter = None
        self.ready = threading.Event()

    def finish(self, role):
        with self.server.lock:
            setattr(self, role + "_done", True)


class Server(HTTPServer):
    allow_reuse_address = False

    def __init__(self, owner, address):
        self.owner = owner
        self.slots = threading.BoundedSemaphore(owner.capacity)
        self.lock = threading.RLock()
        self.leases, self.handlers, self.adapters = [], [], []
        super().__init__(address, Handler, bind_and_activate=False)

    def process_request(self, request, client_address):
        self.reap()
        request.settimeout(self.owner.read_timeout)
        if self.owner.stopping or not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            self.owner.record("capacity_refused", status=503, active=self.owner.active)
            return
        try:
            self.owner.ledger.request("receiver_http")
        except Exception:
            self.slots.release()
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            self.owner.record("admission_refused", code="request_budget_or_store_unavailable")
            return
        lease = Lease(self)
        with self.lock:
            self.leases.append(lease)

        def handle():
            try:
                Handler(request, client_address, self, lease=lease)
            except BaseException:
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
            self.shutdown_request(request)
            lease.finish("handler")
            raise

    def reap(self):
        # A worker cannot attest its own thread exit. Release only from an observer.
        with self.lock:
            for lease in self.leases:
                threads = [lease.handler, lease.adapter]
                if (
                    lease.handler_done
                    and lease.adapter_done
                    and not lease.released
                    and not any(thread is not None and thread.is_alive() for thread in threads)
                ):
                    lease.released = True
                    self.slots.release()

    def service_actions(self):
        self.reap()

    def handle_error(self, request, client_address):
        # BaseServer's traceback can include exception arguments with private data.
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

    def respond(self, status, body=b""):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            self.server.owner.record(
                "reply_unconfirmed", request_id=self.lease.id, code="response_write_failed"
            )
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
        started = time.monotonic()
        with self.server.lock:
            lease.adapter_done = False

        def apply():
            status = 500
            try:
                response = owner.adapter.receive("POST", headers, raw)
                status = response.status
                if type(status) is not int or not 100 <= status <= 599:
                    status = 500
            except BaseException:
                status = 500
                owner.record("adapter_failed", request_id=lease.id, code="adapter_exception")
            finally:
                # No body, headers, exception arguments or traceback enter diagnostics.
                with self.server.lock:
                    lease.status = status
                    late = time.monotonic() >= started + owner.http_wait
                owner.record(
                    "adapter_finished",
                    request_id=lease.id,
                    underlying_status=status,
                    late=late,
                    seconds=time.monotonic() - started,
                )
                lease.ready.set()
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
        if lease.ready.wait(owner.http_wait):
            self.respond(lease.status)
        else:
            with self.server.lock:
                lease.timed_out = True
            owner.record(
                "http_wait_elapsed",
                request_id=lease.id,
                status=503,
                code="underlying_outcome_pending",
                active=owner.active,
            )
            self.respond(503)


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
    ):
        if host != "127.0.0.1" or type(port) is not int or not 0 <= port <= 65535:
            raise DriverRefused("loopback_binding_required")
        if type(capacity) is not int or not 1 <= capacity <= 4:
            raise DriverRefused("invalid_capacity")
        if (
            any(
                type(value) not in {int, float} or not math.isfinite(value) or value <= 0
                for value in (http_wait, read_timeout)
            )
            or http_wait > 4
            or read_timeout > 2
            or type(maximum) is not int
            or not 0 < maximum <= 1048576
        ):
            raise DriverRefused("invalid_http_limits")
        self.adapter, self.ledger = adapter, ledger
        self.capacity, self.http_wait, self.read_timeout = capacity, http_wait, read_timeout
        self.maximum = maximum
        self.route = "/hooks/linear/" + ledger.run_id
        self.health_route = "/health/" + ledger.run_id
        self.stopping, self.started = False, False
        self.diagnostic_failures = 0
        self.thread = self.shutdown_thread = None
        self.server = Server(self, (host, port))

    def record(self, kind, **fields):
        try:
            self.ledger.observe(kind, **fields)
        except Exception:
            # Diagnostics cannot strand a real worker or echo an external exception.
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
        threads = [self.thread, self.shutdown_thread, *self.server.handlers, *self.server.adapters]
        for thread in threads:
            if thread is not None and thread is not threading.current_thread():
                thread.join(max(0, end - time.monotonic()))
        if self.shutdown_thread is None or not self.shutdown_thread.is_alive():
            self.server.server_close()
        threads = [self.thread, self.shutdown_thread, *self.server.handlers, *self.server.adapters]
        live = [thread.name for thread in threads if thread is not None and thread.is_alive()]
        return {
            "stopped": not live and self.active == 0,
            "active_requests": self.active,
            "live_workers": live,
            "diagnostic_failures": self.diagnostic_failures,
        }
