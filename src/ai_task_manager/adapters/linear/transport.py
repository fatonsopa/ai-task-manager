import json
import math
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import LinearError
from .models import WireResponse


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpTransport:
    ENDPOINT = "https://api.linear.app/graphql"

    def __init__(self, *, timeout=10.0, max_response_bytes=1048576, _test_endpoint=None):
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

    def send(self, query, variables, credentials):
        body = json.dumps({"query": query, "variables": variables}, ensure_ascii=False).encode()
        if len(body) > self.max_response_bytes:
            raise LinearError("request_too_large")
        deadline = time.monotonic() + self.timeout
        request = Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": credentials.authorization(),
            },
        )
        try:
            try:
                response = self.opener.open(request, timeout=self.timeout)
            except HTTPError as exc:
                response = exc
            with response:
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
                return WireResponse(
                    response.code, {k.lower(): v for k, v in response.headers.items()}, payload
                )
        except LinearError:
            raise
        except (TimeoutError, OSError, URLError) as exc:
            raise LinearError("transport_failure", uncertain=True) from exc
