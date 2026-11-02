"""Local composition ports only; no credential, tunnel, account or model launch."""

import math
import threading
import time

from ai_task_manager.adapters.linear.webhooks import WebhookReceiver

from .ledger import DriverRefused
from .receiver import BoundedReceiver


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
        if self.thread is not None:
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
        **receiver_options,
    ):
        self.drain = DrainWorker(inbound, handle, ledger)
        adapter = WebhookReceiver(
            core, handle, journal, binding, webhook_id, secret, clock=clock, wake=self.drain.wake
        )
        self.receiver = BoundedReceiver(adapter, ledger, **receiver_options)

    def start(self):
        self.drain.start()
        try:
            return self.receiver.start()
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
