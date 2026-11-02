"""Trusted receiver/tunnel observations, separate from provider and host proof."""

import hashlib
import os
import re
import stat
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from ai_task_manager.adapters.linear.webhooks import WebhookReceiver
from ai_task_manager.config.files import digest

from .endpoint_probe import EndpointProbe, origin_parts, private_json
from .fixture_admin import VerifiedEndpoint
from .ledger import DriverRefused, uuid
from .receiver import BoundedReceiver, receiver_route
from .run_primary import PrimaryRun


class OwnedTunnel:
    def __init__(self, group, argv, executable_sha256, origin_log):
        if (
            not argv
            or not isinstance(executable_sha256, str)
            or not re.fullmatch("[0-9a-f]{64}", executable_sha256)
        ):
            raise DriverRefused("owned_tunnel_configuration_required")
        self.group, self.argv = group, tuple(argv)
        self.executable_sha256, self.origin_log = executable_sha256, Path(origin_log)

    def observe(self, receiver):
        try:
            return self._observe(receiver)
        except (OSError, ValueError, IndexError) as error:
            raise DriverRefused("owned_tunnel_observation_unavailable") from error

    def _observe(self, receiver):
        leader, group = self.group.leader(), self.group.observe()
        if (
            not leader.known
            or leader.exited
            or not group.known
            or not any(member.pid == self.group.process.pid for member in group.live_members)
            or tuple(self.group.process.args) != self.argv
            or hashlib.sha256(Path(self.argv[0]).resolve().read_bytes()).hexdigest()
            != self.executable_sha256
        ):
            raise DriverRefused("owned_tunnel_process_required")
        target = f"http://{receiver.address[0]}:{receiver.address[1]}"
        if (
            "--url" not in self.argv
            or self.argv.index("--url") + 1 >= len(self.argv)
            or self.argv[self.argv.index("--url") + 1] != target
        ):
            raise DriverRefused("owned_tunnel_target_changed")
        before = self.origin_log.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.getuid()
            or before.st_size > 65536
        ):
            raise DriverRefused("private_owned_tunnel_log_required")
        descriptor = os.open(self.origin_log, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            opened, raw = os.fstat(stream.fileno()), stream.read(65537)
        after = self.origin_log.lstat()
        if (
            (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or len(raw) > 65536
        ):
            raise DriverRefused("owned_tunnel_log_changed")
        origins = set(re.findall(rb"https://[a-z0-9-]+\.trycloudflare\.com(?=[\s|\"']|$)", raw))
        if len(origins) != 1:
            raise DriverRefused("owned_tunnel_origin_required")
        origin = origins.pop().decode("ascii")
        origin_parts(origin)
        fields = {
            "pid": self.group.process.pid,
            "start_identity": self.group.start_identity,
            "argv_sha256": digest(list(self.argv)),
            "executable_sha256": self.executable_sha256,
            "origin": origin,
            "log_device": opened.st_dev,
            "log_inode": opened.st_ino,
        }
        return {**fields, "identity": digest(fields), "log_sha256": hashlib.sha256(raw).hexdigest()}


class EndpointAuthority:
    def __init__(self, receiver, tunnel, ledger, webhook_id, private_root, *, clock=time.time):
        if not isinstance(receiver, BoundedReceiver) or receiver.ledger is not ledger:
            raise DriverRefused("owned_endpoint_receiver_required")
        self.receiver, self.tunnel, self.ledger = receiver, tunnel, ledger
        self.webhook_id, self.clock = uuid(webhook_id), clock
        self.root = Path(private_root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.getuid()
        ):
            raise DriverRefused("private_endpoint_directory_required")
        self.probe = EndpointProbe(ledger, self.root / "probes")
        self.receipts = {}
        self.source_pins = {
            path: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                Path(__file__),
                Path(__file__).with_name("receiver.py"),
                Path(__file__).with_name("endpoint_probe.py"),
                Path(__file__).with_name("run_primary.py"),
            )
        }

    def _receiver(self):
        receiver = self.receiver
        if (
            any(
                hashlib.sha256(path.read_bytes()).hexdigest() != expected
                for path, expected in self.source_pins.items()
            )
            or not receiver.started
            or receiver.stopping
            or receiver.thread is None
            or not receiver.thread.is_alive()
            or not receiver.loop_entered.is_set()
            or receiver.server.socket.fileno() < 0
            or receiver.address[0] != "127.0.0.1"
            or receiver.route != receiver_route(self.ledger.run_id)
        ):
            raise DriverRefused("owned_endpoint_receiver_not_running")
        fields = {
            "pid": os.getpid(),
            "thread_ident": receiver.thread.ident,
            "descriptor": receiver.server.socket.fileno(),
            "address": receiver.address,
            "adapter_identity": id(receiver.adapter),
            "run_id": self.ledger.run_id,
        }
        return {**fields, "identity": digest(fields)}

    def _product(self):
        adapter, scope = self.receiver.adapter, self.ledger.scope
        owned = self.ledger.resource(scope.project)
        if owned["kind"] != "project" or owned["state"] != "active":
            raise DriverRefused("owned_endpoint_project_required")
        if not isinstance(adapter, WebhookReceiver):
            raise DriverRefused("scoped_product_receiver_required")
        run = getattr(self.receiver, "product_run", None)
        if (
            not isinstance(run, PrimaryRun)
            or run.receiver is not self.receiver
            or run.adapter is not adapter
            or run.drain.ledger is not self.ledger
            or run.drain.handle != adapter.handle
            or run.drain.thread is None
            or not run.drain.thread.is_alive()
            or run.drain.closing.is_set()
            or getattr(run.drain.inbound, "core", None) is not adapter.core
        ):
            raise DriverRefused("scoped_product_drain_required")
        _, current = adapter.core.session_binding(adapter.handle)
        expected = adapter.binding.expected
        if (current.workspace, current.principal, current.project) != (
            scope.workspace,
            scope.owner,
            scope.project,
        ) or (
            expected.workspace_id,
            expected.viewer_id,
            expected.team_id,
            expected.project_id,
            adapter.webhook_id,
        ) != (scope.workspace, scope.owner, scope.team, scope.project, self.webhook_id):
            raise DriverRefused("scoped_product_receiver_required")

    def observe_transport(self):
        before, tunnel = self._receiver(), self.tunnel.observe(self.receiver)
        observed = self.probe(self.receiver, tunnel["origin"])
        if (
            observed.get("result", {}).get("passed") is not True
            or before["identity"] != self._receiver()["identity"]
            or tunnel["identity"] != self.tunnel.observe(self.receiver)["identity"]
        ):
            raise DriverRefused("owned_endpoint_observation_changed")
        evidence = {
            "receiver": before,
            "tunnel": tunnel,
            "probe": observed,
            "observed_at": self.clock(),
            "scope": asdict(self.ledger.scope),
            "run_id": self.ledger.run_id,
            "authority": "transport_observation_only",
        }
        private_json(self.root / ("observation-" + str(uuid4()) + ".json"), evidence)
        return evidence

    def admit(self):
        self._product()
        observed = self.observe_transport()
        self._product()
        s = self.ledger.scope
        reference = "endpoint:" + str(uuid4())
        receipt = VerifiedEndpoint(
            observed["tunnel"]["origin"] + self.receiver.route,
            observed["receiver"]["identity"],
            observed["tunnel"]["identity"],
            observed["observed_at"] + 30,
            self.ledger.run_id,
            s.workspace,
            s.owner,
            s.team,
            s.project,
            self.webhook_id,
        )
        private_json(self.root / (reference.replace(":", "-") + ".json"), asdict(receipt))
        self.receipts[reference] = receipt
        return reference

    def verify(self, reference):
        receipt = self.receipts.get(reference)
        if receipt is None or not 0 < receipt.expires_at - self.clock() <= 30:
            raise DriverRefused("owned_endpoint_reference_expired")
        self._product()
        if (
            receipt.receiver_identity != self._receiver()["identity"]
            or receipt.tunnel_identity != self.tunnel.observe(self.receiver)["identity"]
        ):
            raise DriverRefused("owned_endpoint_observation_changed")
        return receipt
