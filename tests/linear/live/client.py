"""Read actual complete fixture responses, including early upload rejection."""

import http.client

MAXIMUM_RESPONSE_BYTES = 65536


class IncompleteResponse(OSError):
    """The fixture peer did not supply a complete bounded response."""


class _HeaderCapture:
    def __init__(self, stream):
        self.stream = stream
        self.data = bytearray()

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def readline(self, size=-1):
        remaining = MAXIMUM_RESPONSE_BYTES - len(self.data)
        limit = min(size, remaining + 1) if size >= 0 else remaining + 1
        value = self.stream.readline(limit)
        self.data.extend(value)
        if len(self.data) > MAXIMUM_RESPONSE_BYTES:
            raise IncompleteResponse("response_headers_exceed_bound")
        return value


class _Response(http.client.HTTPResponse):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.header_capture = _HeaderCapture(self.fp)
        self.fp = self.header_capture


def send_request(
    server, headers=(), body=b"", *, route=None, method="POST", length=None, omit_length=False
):
    connection = http.client.HTTPConnection(*server.address, timeout=6)
    connection.response_class = _Response
    upload_error = None
    try:
        connection.putrequest(method, route or server.route)
        for key, value in headers:
            connection.putheader(key, value)
        if not omit_length and not any(key.lower() == "content-length" for key, _ in headers):
            connection.putheader("Content-Length", str(len(body)) if length is None else length)
        try:
            connection.endheaders(body)
        except BrokenPipeError as error:
            upload_error = error
        try:
            response = connection.getresponse()
            if not response.header_capture.data.endswith(b"\r\n\r\n"):
                raise IncompleteResponse("response_headers_incomplete")
            if response.getheader("Transfer-Encoding") is not None:
                raise IncompleteResponse("unsupported_transfer_encoding")
            declared = response.getheader("Content-Length")
            if (
                declared is None
                or not declared.isdecimal()
                or int(declared) > MAXIMUM_RESPONSE_BYTES
            ):
                raise IncompleteResponse("response_length_unbounded")
            value = response.read(int(declared))
            if len(value) != int(declared):
                raise IncompleteResponse("response_body_incomplete")
            return response.status, value
        except (OSError, http.client.HTTPException, ValueError) as response_error:
            if upload_error is not None:
                raise upload_error from response_error
            raise
    finally:
        connection.close()
