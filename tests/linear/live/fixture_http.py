"""Clamp configured transport waits; a lost reply never means cancellation."""

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

    def send(self, query, variables, credentials):
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
        )
        return transport.send(query, variables, credentials)
