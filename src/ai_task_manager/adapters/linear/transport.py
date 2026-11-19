import json
import math
import time
from contextlib import contextmanager
from threading import local
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import LinearError
from .models import WireResponse
from .rate_limits import QuotaStore


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpTransport:
    ENDPOINT = "https://api.linear.app/graphql"

    def __init__(self, *, timeout=10.0, max_response_bytes=1048576, _test_endpoint=None,
                 _quota_store=None):
        if (
            not math.isfinite(timeout)
            or not 0 < timeout <= 30
            or not 0 < max_response_bytes <= 4194304
        ):
            raise LinearError("invalid_transport_limits")
        self.endpoint = self.ENDPOINT
        if _test_endpoint is not None:
            parsed = urlsplit(_test_endpoint)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
                or parsed.username
                or parsed.password
            ):
                raise LinearError("invalid_test_endpoint")
            self.endpoint = _test_endpoint
        self.timeout, self.max_response_bytes = timeout, max_response_bytes
        self.opener = build_opener(NoRedirect())
        self.quota = _quota_store if _quota_store is not None else QuotaStore()
        self._admitted = local()

    @staticmethod
    def _key(query, variables, credentials):
        return (query, json.dumps(variables, sort_keys=True), credentials.authorization())

    @contextmanager
    def admission(self, query, variables, credentials, *, principal=None, not_after=None):
        deadline = min(time.monotonic() + self.timeout,
                       not_after if not_after is not None else float("inf"))
        while True:
            if time.monotonic() >= deadline:
                raise LinearError("quota_admission_deadline")
            try:
                reference = self.quota.reserve(query, variables, credentials, principal=principal)
                break
            except LinearError as error:
                if error.category != "quota_busy":
                    raise
                # Only initial identity discovery is serialized, before any effect fence.
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        ticket = {"reference": reference, "key": self._key(query, variables, credentials),
                  "sent": False, "response": None, "deadline": deadline}
        previous = getattr(self._admitted, "ticket", None)
        self._admitted.ticket = ticket
        try:
            yield True
        finally:
            self._admitted.ticket = previous
            try:
                if ticket["sent"]:
                    self.quota.complete(reference, ticket["response"])
                else:
                    self.quota.cancel(reference)
            except Exception:
                # A returned provider result/unknown send remains authoritative.
                # The durable reservation stays charged if accounting cannot commit.
                try:
                    self.quota.accounting_failed(reference)
                except Exception:
                    pass

    def send(self, query, variables, credentials):
        ticket = getattr(self._admitted, "ticket", None)
        if ticket is None:
            with self.admission(query, variables, credentials):
                return self.send(query, variables, credentials)
        if ticket["key"] != self._key(query, variables, credentials) or ticket["sent"]:
            raise LinearError("quota_reservation_mismatch")
        return self._send(query, variables, credentials, ticket)

    def _send(self, query, variables, credentials, ticket):
        body = json.dumps({"query": query, "variables": variables}, ensure_ascii=False).encode()
        if len(body) > self.max_response_bytes:
            raise LinearError("request_too_large")
        deadline = ticket["deadline"]
        request = Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": credentials.authorization(),
            },
        )
        if time.monotonic() >= deadline:
            raise LinearError("quota_admission_deadline")
        try:
            ticket["sent"] = True
            try:
                response = self.opener.open(
                    request, timeout=max(0.001, deadline - time.monotonic())
                )
            except HTTPError as exc:
                response = exc
            with response:
                ticket["response"] = WireResponse(
                    response.code, {k.lower(): v for k, v in response.headers.items()}, b""
                )
                chunks, size = [], 0
                while True:
                    if time.monotonic() >= deadline:
                        raise LinearError("transport_deadline", uncertain=True)
                    chunk = response.read1(min(65536, self.max_response_bytes - size + 1))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > self.max_response_bytes:
                        raise LinearError("response_too_large", uncertain=True)
                payload = b"".join(chunks)
                result = WireResponse(
                    response.code, {k.lower(): v for k, v in response.headers.items()}, payload
                )
                ticket["response"] = result
                return result
        except LinearError:
            raise
        except (TimeoutError, OSError, URLError) as exc:
            raise LinearError("transport_failure", uncertain=True) from exc
