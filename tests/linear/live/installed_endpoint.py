"""Observe an invocation-owned installed CLI through the existing endpoint gate.

This evaluator does not launch a receiver or issue authority. The trusted caller
supplies its actual child/group and reviewed installed package bindings.
"""

import hashlib
import json
import math
import os
import selectors
import time
from dataclasses import asdict
from pathlib import Path

from ai_task_manager.adapters.decisions.observations import (
    SessionObservations,
    current_enrollment,
    intent,
)
from ai_task_manager.adapters.process.groups import OwnedProcessGroup
from ai_task_manager.application.service.facade import ApplicationService
from ai_task_manager.config.files import FileSeal, digest, read_private, strict_json
from ai_task_manager.domains.work.errors import WorkError

from .ledger import DriverRefused, uuid


def installed_route(run_id):
    return "/hooks/" + uuid(run_id)


class InstalledReceiverProcess:
    """Readiness is read from the owned child's pipe, never accepted as caller data."""

    def __init__(self, group, service, config_file, ledger, package, *, startup_seconds=30):
        if (
            not isinstance(group, OwnedProcessGroup)
            or not isinstance(service, ApplicationService)
            or type(startup_seconds) not in {int, float}
            or not math.isfinite(startup_seconds)
            or not 0 < startup_seconds <= 30
        ):
            raise DriverRefused("installed_receiver_owned_inputs_required")
        if not isinstance(package, dict) or set(package) != {"entrypoint", "pins", "runtime_root"}:
            raise DriverRefused("installed_receiver_package_required")
        self.group, self.service, self.ledger = group, service, ledger
        self.package = json.loads(json.dumps(package))
        self._package_digest = digest(package)
        self.config_file = Path(config_file).absolute()
        self.argv = (
            str(Path(package["entrypoint"]).absolute()),
            "--config",
            str(self.config_file),
            "--json",
            "sync",
            "listen",
        )
        self._bound = group, service, ledger, self.config_file
        config = read_private(self.config_file)
        self.receiver_file = Path(config["receiver_file"])
        self.config = read_private(self.receiver_file)
        self._configuration = config
        self._config_digest = digest(self.config)
        self.route = installed_route(ledger.run_id)
        self.health_route = self.route + "/health"
        if self.config.get("route") != self.route:
            raise DriverRefused("installed_receiver_route_mismatch")
        paths = [
            self.config_file,
            self.receiver_file,
            Path(config["session_file"]),
            Path(self.config["secret_file"]),
        ]
        if config["linear"].get("credential_file"):
            paths.append(Path(config["linear"]["credential_file"]))
        self.sources = [(FileSeal(path), None) for path in paths]
        self.sources = [(seal, seal.metadata()) for seal, _ in self.sources]
        self._source_identity = digest([(str(s.path), m, s.sha256) for s, m in self.sources])
        self.ready = None
        self.address = None
        self.health_identity = None
        self.check_product(self.config["webhook_id"])
        self._process()
        self.ready = self._read_ready(startup_seconds)
        self._ready_deadline = time.monotonic() + self.ready["lifetime_seconds"]
        self.address = (self.ready["host"], self.ready["port"])
        self.health_identity = {"instance_id": self.ready["instance_id"]}
        self._ready_digest = digest(self.ready)
        self.observe()

    def _package(self):
        if digest(self.package) != self._package_digest:
            raise DriverRefused("installed_receiver_package_changed")
        root = Path(self.package["runtime_root"])
        pins = self.package["pins"]
        inventory = {
            str(p)
            for p in root.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.name != ".DS_Store"
        }
        if not root.is_absolute() or not isinstance(pins, dict) or not inventory <= pins.keys():
            raise DriverRefused("installed_receiver_package_incomplete")
        required = {
            str(Path(self.package["entrypoint"]).absolute()),
            str(root / "application/linear_sync/listener.py"),
            str(root / "adapters/linear/listener.py"),
            str(root / "presentation/cli/main.py"),
        }
        if not required <= pins.keys() or not all(Path(p).is_absolute() for p in pins):
            raise DriverRefused("installed_receiver_package_incomplete")
        for path, expected in pins.items():
            if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
                raise DriverRefused("installed_receiver_source_changed")

    def _process(self):
        self._package()
        if tuple(self.group.process.args) != self.argv:
            raise DriverRefused("installed_receiver_command_changed")
        leader = self.group.leader()
        group = self.group.observe()
        if (
            not leader.known
            or leader.exited
            or not group.known
            or not any(m.pid == self.group.process.pid for m in group.live_members)
        ):
            raise DriverRefused("installed_receiver_process_not_running")

    def _read_ready(self, seconds):
        stream = self.group.process.stdout
        if stream is None:
            raise DriverRefused("installed_receiver_owned_output_required")
        deadline, raw = time.monotonic() + seconds, bytearray()
        with selectors.DefaultSelector() as selected:
            selected.register(stream, selectors.EVENT_READ)
            while b"\n" not in raw:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selected.select(remaining):
                    raise DriverRefused("installed_receiver_readiness_timeout")
                chunk = os.read(stream.fileno(), 4096)
                if not chunk or len(raw) + len(chunk) > 16384:
                    raise DriverRefused("installed_receiver_readiness_unavailable")
                raw.extend(chunk)
        try:
            envelope = strict_json(raw.split(b"\n", 1)[0])
            ready = envelope["data"]
            if (
                set(envelope) != {"ok", "event", "data"}
                or envelope["ok"] is not True
                or envelope["event"] != "receiver_ready"
                or set(ready)
                != {"instance_id", "host", "port", "route", "health_route", "lifetime_seconds"}
                or ready["host"] != "127.0.0.1"
                or type(ready["port"]) is not int
                or not 1 <= ready["port"] <= 65535
                or ready["route"] != self.route
                or ready["health_route"] != self.health_route
                or ready["lifetime_seconds"] != self.config["lifetime_seconds"]
                or self.config["port"] not in (0, ready["port"])
            ):
                raise ValueError()
            uuid(ready["instance_id"])
            return ready
        except (ValueError, KeyError, TypeError, WorkError):
            raise DriverRefused("installed_receiver_readiness_mismatch") from None

    def check_product(self, webhook_id):
        if any(
            a is not b
            for a, b in zip((self.group, self.service, self.ledger, self.config_file), self._bound)
        ):
            raise DriverRefused("installed_receiver_owner_changed")
        if self.service.seal is None or self.service.seal.path != self.config_file:
            raise DriverRefused("installed_receiver_configuration_mismatch")
        for seal, metadata in self.sources:
            if seal.metadata() != metadata:
                raise DriverRefused("installed_receiver_configuration_changed")
            seal.check()
        if (
            self.service.claim_store is None
            or self.service.inbound is None
            or self.service.inbound.core is not self.service.core
        ):
            raise DriverRefused("installed_receiver_product_binding_required")
        self.service.claim_store.check()
        self.service.seal.check()
        _, scope = self.service.core.session_binding(self.service.handle)
        expected = self.service.inbound.client.binding.expected
        selected = self.ledger.scope
        if scope != self.service.scope or (
            scope.workspace,
            scope.principal,
            scope.project,
            expected.workspace_id,
            expected.viewer_id,
            expected.team_id,
            expected.project_id,
            self.config["webhook_id"],
        ) != (
            selected.workspace,
            selected.owner,
            selected.project,
            selected.workspace,
            selected.owner,
            selected.team,
            selected.project,
            webhook_id,
        ):
            raise DriverRefused("installed_receiver_scope_mismatch")
        observations = self.service.decisions.observations if self.service.decisions else None
        if not isinstance(observations, SessionObservations) or not self.service.grant_ref:
            raise DriverRefused("installed_receiver_enrollment_required")
        observations._check()
        enrollment = current_enrollment(observations.journal, self.service.grant_ref, asdict(scope))
        if (
            observations.config_file != self.config_file
            or observations.client is not self.service.inbound.client
            or observations.pointer["reference"] != self.service.grant_ref
            or enrollment["config_path"] != str(self.config_file)
            or enrollment["config_intent"] != intent(read_private(self.config_file))
            or enrollment["provider"] != asdict(expected)
            or self._configuration["grant_ref"] != self.service.grant_ref
            or Path(self.service.claim_store.path) != Path(self._configuration["core_database"])
            or Path(self.service.core.context.db.path) != Path(self.service.claim_store.path)
            or digest(self.config) != self._config_digest
        ):
            raise DriverRefused("installed_receiver_enrollment_configuration_mismatch")

    def observe(self):
        self.check_product(self.config["webhook_id"])
        self._process()
        if (
            time.monotonic() >= self._ready_deadline
            or self.ready is None
            or digest(self.ready) != self._ready_digest
            or self.address != (self.ready["host"], self.ready["port"])
            or self.health_identity != {"instance_id": self.ready["instance_id"]}
            or self.route != installed_route(self.ledger.run_id)
            or self.health_route != self.route + "/health"
        ):
            raise DriverRefused("installed_receiver_ready_identity_changed")
        fields = {
            "kind": "installed_cli",
            "pid": self.group.process.pid,
            "start_identity": self.group.start_identity,
            "argv_sha256": digest(self.argv),
            "package_sha256": self._package_digest,
            "source_identity": self._source_identity,
            "address": self.address,
            "instance_id": self.ready["instance_id"],
            "run_id": self.ledger.run_id,
        }
        return {**fields, "identity": digest(fields)}
