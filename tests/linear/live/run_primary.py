"""Local composition ports only; no credential, tunnel, account or model launch."""

import math
import threading
import time

from ai_task_manager.adapters.linear.webhooks import WebhookReceiver

from .ledger import DriverRefused
from .receiver import BoundedReceiver, receiver_route


class BudgetedTransport:
    def __init__(self, ledger, transport):
        self.ledger, self.transport = ledger, transport

    def send(self, query, variables, credentials):
        self.ledger.request("product_transport")
        return self.transport.send(query, variables, credentials)


class DrainWorker:
    def __init__(self, inbound, handle, ledger, *, maximum_per_wake=16):
        if type(maximum_per_wake) is not int or not 0 < maximum_per_wake <= 32:
            raise DriverRefused("invalid_drain_bound")
        self.inbound, self.handle, self.ledger = inbound, handle, ledger
        self.maximum = maximum_per_wake
        self.event, self.closing = threading.Event(), threading.Event()
        self.thread = None

    def wake(self):
        self.event.set()

    def start(self):
        if self.thread is not None or self.closing.is_set():
            raise DriverRefused("drain_already_started")

        def drain():
            while not self.closing.is_set():
                self.event.wait()
                self.event.clear()
                if self.closing.is_set():
                    break
                for _ in range(self.maximum):
                    if self.closing.is_set():
                        break
                    try:
                        result = self.inbound.run_once(self.handle)
                    except BaseException:
                        self.ledger.observe("drain_failed", code="drain_exception")
                        break
                    if result is None or result.get("state") in {"pending", "conflict"}:
                        break
                self.ledger.observe("drain_wake_completed", worked=True)

        self.thread = threading.Thread(target=drain, name="owned-linear-drain")
        self.thread.start()

    def stop(self, timeout=12):
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout < 0:
            raise DriverRefused("invalid_stop_bound")
        self.closing.set()
        self.event.set()
        if self.thread is not None and self.thread.ident is not None:
            self.thread.join(timeout)
        return {"stopped": self.thread is None or not self.thread.is_alive()}


class PrimaryRun:
    """The caller supplies actual scoped product objects; this grants no authority."""

    def __init__(
        self,
        core,
        handle,
        journal,
        binding,
        webhook_id,
        secret,
        inbound,
        ledger,
        *,
        clock,
        receiver=None,
        **receiver_options,
    ):
        self.drain = DrainWorker(inbound, handle, ledger)
        self.adapter = WebhookReceiver(
            core, handle, journal, binding, webhook_id, secret, clock=clock, wake=self.drain.wake
        )
        self.reusing = receiver is not None
        if self.reusing:
            if receiver_options:
                raise DriverRefused("existing_receiver_options_conflict")
            self.receiver = receiver
            self._check_receiver()
            self._check_product()
        else:
            self.receiver = BoundedReceiver(self.adapter, ledger, **receiver_options)

    def _check_receiver(self):
        receiver = self.receiver
        if (
            not isinstance(receiver, BoundedReceiver)
            or receiver.ledger is not self.drain.ledger
            or receiver.route != receiver_route(self.drain.ledger.run_id)
            or not receiver.started
            or receiver.stopping
            or receiver.thread is None
            or not receiver.thread.is_alive()
            or not receiver.loop_entered.is_set()
            or receiver.server.socket.fileno() < 0
            or receiver.address[0] != "127.0.0.1"
            or receiver.active
        ):
            raise DriverRefused("existing_receiver_not_quiescent")

    def _check_product(self):
        expected, scope = self.adapter.binding.expected, self.drain.ledger.scope
        _, current = self.adapter.core.session_binding(self.adapter.handle)
        if (expected.workspace_id, expected.viewer_id, expected.team_id, expected.project_id) != (
            scope.workspace,
            scope.owner,
            scope.team,
            scope.project,
        ) or (current.workspace, current.principal, current.project) != (
            scope.workspace,
            scope.owner,
            scope.project,
        ):
            raise DriverRefused("existing_receiver_scope_mismatch")
        owned = self.drain.ledger.resource(scope.project)
        if owned["kind"] != "project" or owned["state"] != "active":
            raise DriverRefused("existing_receiver_project_required")

    def start(self):
        if self.reusing:
            with self.receiver.lifecycle:
                if self.drain.thread is not None or self.drain.closing.is_set():
                    raise DriverRefused("existing_receiver_already_activated")
                self._check_receiver()
                self._check_product()
                placeholder = self.receiver.adapter
                try:
                    self.drain.start()
                    self._check_receiver()
                    self._check_product()
                    if self.drain.thread is None or not self.drain.thread.is_alive():
                        raise DriverRefused("existing_receiver_drain_not_running")
                    self.receiver.adapter = self.adapter
                    self.receiver.product_run = self
                    return self.receiver.address
                except BaseException:
                    self.receiver.adapter = placeholder
                    self.drain.stop()
                    raise
        self.drain.start()
        try:
            address = self.receiver.start()
            self.receiver.product_run = self
            return address
        except BaseException:
            self.drain.stop()
            raise

    def stop(self, timeout=12):
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout < 0:
            raise DriverRefused("invalid_stop_bound")
        end = time.monotonic() + timeout
        receiver = self.receiver.stop(max(0, end - time.monotonic()))
        drain = self.drain.stop(max(0, end - time.monotonic()))
        return {
            "stopped": receiver["stopped"] and drain["stopped"],
            "receiver": receiver,
            "drain": drain,
        }

    def require_quiescent(self):
        # Observe the actual owned handles. A successful HTTP response, a timed
        # wait, or a caller-made stopped flag cannot establish quiescence.
        receiver = self.receiver
        threads = [
            receiver.thread,
            receiver.shutdown_thread,
            self.drain.thread,
            *receiver.server.handlers,
            *receiver.server.adapters,
        ]
        if (
            not receiver.stopping
            or not self.drain.closing.is_set()
            or receiver.active
            or any(thread is not None and thread.is_alive() for thread in threads)
        ):
            raise DriverRefused("owned_runtime_not_quiescent")
