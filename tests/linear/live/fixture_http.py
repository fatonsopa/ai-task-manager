"""Clamp configured transport waits; a lost reply never means cancellation."""

from contextlib import contextmanager
from threading import local

from ai_task_manager.adapters.linear.transport import HttpTransport

from .ledger import DriverRefused


class FixtureRequestTransport:
    def __init__(self, ledger, transport):
        if isinstance(transport, FixtureRequestTransport):
            prior = transport.ledger
            if (prior.path, prior.run_id, prior.scope, prior.limits) != (
                ledger.path,
                ledger.run_id,
                ledger.scope,
                ledger.limits,
            ):
                raise DriverRefused("fixture_transport_binding_changed")
            transport = transport.transport
        if not isinstance(transport, HttpTransport):
            raise DriverRefused("product_http_transport_required")
        self.ledger, self.transport = ledger, transport
        self._admitted = local()

    def _bounded(self):
        try:
            deadline = self.ledger.request_deadline()
        except DriverRefused as error:
            if str(error) == "deadline_expired":
                raise DriverRefused("fixture_transport_deadline") from error
            raise
        remaining = deadline - self.ledger.clock()
        if remaining <= 0:
            raise DriverRefused("fixture_transport_deadline")
        source = self.transport
        transport = HttpTransport(
            timeout=min(3, source.timeout, remaining),
            max_response_bytes=source.max_response_bytes,
            _test_endpoint=source.endpoint if source.endpoint != HttpTransport.ENDPOINT else None,
            _quota_store=source.quota,
        )
        return transport

    @contextmanager
    def admission(self, query, variables, credentials, **kwargs):
        transport = self._bounded()
        prior = getattr(self._admitted, "transport", None)
        self._admitted.transport = transport
        try:
            with transport.admission(query, variables, credentials, **kwargs) as admitted:
                yield admitted
        finally:
            self._admitted.transport = prior

    def send(self, query, variables, credentials):
        transport = getattr(self._admitted, "transport", None)
        if transport is not None:
            return transport.send(query, variables, credentials)
        with self.admission(query, variables, credentials):
            return self.send(query, variables, credentials)
