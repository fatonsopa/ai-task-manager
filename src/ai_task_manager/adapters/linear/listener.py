"""Bounded loopback HTTP transport; policy remains in WebhookReceiver."""

import socket
import threading
import time
from http import HTTPStatus


class ListenerStopping(Exception):
    """The owner requested shutdown after current authority checks passed."""


class LoopbackListener:
    HEADER_LIMIT = 16384
    BODY_LIMIT = 1048576
    REQUEST_SECONDS = 3.0
    CAPACITY = 4

    def __init__(self, receive, *, route, port, health):
        self.receive, self.route, self.health = receive, route, health
        self.health_route = route + "/health"
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.reservation = None
        self.socket.settimeout(0.1)
        self.address = ("127.0.0.1", port)
        self.closing = threading.Event()
        self.slots = threading.BoundedSemaphore(self.CAPACITY)
        self.lock = threading.Lock()
        self.workers, self.connections = [], set()
        self.thread = None
        self.failure = False

    def start(self):
        try:
            # BSD permits a specific-address bind over a reusable wildcard listener.
            # Retain a non-listening wildcard reservation to exclude that overlap.
            # It never listens, accepts or sends data; HTTP remains loopback-only.
            self.reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.reservation.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.reservation.bind(("0.0.0.0", self.address[1]))
            self.address = ("127.0.0.1", self.reservation.getsockname()[1])
            # Reclaim this address after closed HTTP connections enter TIME_WAIT.
            # SO_REUSEPORT is deliberately absent: an active listener stays exclusive.
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind(self.address)
            self.socket.listen(self.CAPACITY)
            self.address = self.socket.getsockname()
            self.thread = threading.Thread(target=self._accept, name="linear-listener", daemon=True)
            self.thread.start()
        except BaseException:
            self.close(1)
            raise

    def _accept(self):
        try:
            while not self.closing.is_set():
                try:
                    connection, _ = self.socket.accept()
                except TimeoutError:
                    continue
                deadline = time.monotonic() + self.REQUEST_SECONDS
                with self.lock:
                    self.workers = [t for t in self.workers if t.is_alive()]
                if not self.slots.acquire(blocking=False):
                    self._reply(connection, 503, b"", deadline)
                    connection.close()
                    continue
                worker = threading.Thread(
                    target=self._handle,
                    args=(connection, deadline),
                    name="linear-http",
                    daemon=True,
                )
                with self.lock:
                    self.connections.add(connection)
                    self.workers.append(worker)
                try:
                    worker.start()
                except BaseException:
                    with self.lock:
                        self.connections.discard(connection)
                    connection.close()
                    self.slots.release()
                    raise
        except BaseException:
            if not self.closing.is_set():
                self.failure = True
                self.closing.set()

    @staticmethod
    def _reply(connection, status, body, deadline):
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            connection.settimeout(remaining)
            header = (
                f"HTTP/1.0 {status} {HTTPStatus(status).phrase}\r\n"
                f"Content-Length: {len(body)}\r\nContent-Type: application/json\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            connection.sendall(header + body)
        except OSError:
            pass

    def _read(self, connection, deadline):
        raw = bytearray()
        while b"\r\n\r\n" not in raw:
            if len(raw) >= self.HEADER_LIMIT:
                return 431, None
            connection.settimeout(max(0.001, deadline - time.monotonic()))
            if time.monotonic() >= deadline:
                return 408, None
            chunk = connection.recv(min(4096, self.HEADER_LIMIT - len(raw)))
            if not chunk:
                return 400, None
            raw.extend(chunk)
        header, body = bytes(raw).split(b"\r\n\r\n", 1)
        try:
            lines = header.decode("ascii").split("\r\n")
            method, route, version = lines[0].split(" ")
            if version not in {"HTTP/1.0", "HTTP/1.1"} or len(lines) > 65:
                return 400, None
            headers, names = [], set()
            for line in lines[1:]:
                key, value = line.split(":", 1)
                if (
                    not key
                    or key.lower() in names
                    or any(c.isspace() for c in key)
                    or any(ord(c) < 32 for c in value)
                ):
                    return 400, None
                names.add(key.lower())
                headers.append((key, value.strip()))
            if method == "GET" and route == self.health_route:
                return 200, self.health()
            if route != self.route:
                return 404, None
            if method != "POST":
                return 405, None
            fields = {k.lower(): v for k, v in headers}
            length = fields.get("content-length", "")
            if "transfer-encoding" in fields or not length.isascii() or not length.isdecimal():
                return 400, None
            if len(length) > 10 or int(length) > self.BODY_LIMIT:
                return 413, None
            length = int(length)
            if len(body) > length:
                return 400, None
            while len(body) < length:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return 408, None
                connection.settimeout(remaining)
                chunk = connection.recv(min(65536, length - len(body)))
                if not chunk:
                    return 400, None
                body += chunk
            return None, (headers, body)
        except (ValueError, UnicodeError):
            return 400, None

    def _handle(self, connection, deadline):
        callback = None
        try:
            status, value = self._read(connection, deadline)
            if status is not None:
                self._reply(
                    connection, status, value if isinstance(value, bytes) else b"", deadline
                )
                return
            done, result = threading.Event(), [503]

            def intake():
                try:
                    if not self.closing.is_set():
                        result[0] = self.receive("POST", *value).status
                except ListenerStopping:
                    pass  # Keep the refusal response; shutdown is not an intake failure.
                except BaseException:
                    self.failure = True
                    self.closing.set()
                finally:
                    done.set()

            callback = threading.Thread(target=intake, name="linear-intake", daemon=True)
            callback.start()
            # Reserve output time; an elapsed HTTP wait does not cancel durable intake.
            ready = done.wait(max(0, deadline - time.monotonic() - 0.1))
            self._reply(connection, result[0] if ready else 503, b"", deadline)
        except ListenerStopping:
            self._reply(connection, 503, b"", deadline)
        except (OSError, TimeoutError):
            self._reply(connection, 408, b"", deadline)
        except BaseException:
            self.failure = True
            self.closing.set()
        finally:
            connection.close()
            # Retain the capacity slot and owner while any late intake is still running.
            if callback is not None and callback.ident is not None:
                callback.join()
            with self.lock:
                self.connections.discard(connection)
            self.slots.release()

    def close(self, timeout):
        end = time.monotonic() + timeout
        self.closing.set()
        self.socket.close()
        if self.thread is not None and self.thread.ident is not None:
            self.thread.join(max(0, end - time.monotonic()))
        with self.lock:
            for connection in self.connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            threads = [self.thread, *self.workers]
        for thread in threads:
            if thread is not None and thread.ident is not None:
                thread.join(max(0, end - time.monotonic()))
        live = [t.name for t in threads if t is not None and t.is_alive()]
        stopped = not live and self.socket.fileno() == -1
        if stopped and self.reservation is not None:
            self.reservation.close()
        return {"stopped": stopped, "live_workers": live}
