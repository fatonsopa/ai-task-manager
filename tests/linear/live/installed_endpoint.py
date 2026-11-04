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
from ai_task_manager.config.files import FileSeal, digest, read_private, strict_json, write_private
from ai_task_manager.domains.work.errors import WorkError
from ai_task_manager.observability.failures import FailureStage

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

    def _retain_readiness(self, raw, status, envelope=None, mismatches=()):
        # Child output is untrusted even when the executable is pinned. Retain
        # only closed vocabularies and locations within that package, never text.
        record = {
            "version": 1,
            "pid": self.group.process.pid,
            "status": status,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "mismatches": list(mismatches),
            "diagnostics": [],
        }
        if type(envelope) is dict:
            record["event"] = (
                envelope.get("event")
                if envelope.get("event") in ("receiver_ready", "receiver_stopped")
                else "unrecognized"
            )
            record["ok"] = envelope.get("ok") if type(envelope.get("ok")) is bool else None
            allowed_modules = set()
            runtime = Path(self.package["runtime_root"])
            for path in self.package["pins"]:
                try:
                    relative = Path(path).relative_to(runtime)
                except ValueError:
                    continue
                if relative.suffix == ".py":
                    allowed_modules.add(
                        "ai_task_manager." + ".".join(relative.with_suffix("").parts)
                    )
            diagnostics = envelope.get("diagnostics")
            for item in diagnostics[:2] if type(diagnostics) is list else ():
                if type(item) is not dict:
                    continue
                stages = {stage.value for stage in FailureStage}
                stage = item.get("stage")
                projected = {
                    "stage": stage if type(stage) is str and stage in stages else "unknown",
                    "causes": [],
                }
                causes = item.get("causes")
                for cause in causes[:4] if type(causes) is list else ():
                    if type(cause) is not dict:
                        continue
                    codes = {
                        "access_denied",
                        "validation_error",
                        "timeout",
                        "os_error",
                        "permission_error",
                        "file_not_found",
                        "runtime_error",
                        "type_error",
                        "value_error",
                        "interrupted",
                        "unclassified",
                    }
                    code = cause.get("code")
                    safe = {
                        "code": code if type(code) is str and code in codes else "unclassified",
                        "frames": [],
                    }
                    frames = cause.get("frames")
                    for frame in frames[:8] if type(frames) is list else ():
                        if type(frame) is not dict:
                            continue
                        module, line = frame.get("module"), frame.get("line")
                        if (
                            type(module) is str
                            and module in allowed_modules
                            and type(line) is int
                            and 0 < line < 10_000_000
                        ):
                            safe["frames"].append({"module": module, "line": line})
                    projected["causes"].append(safe)
                record["diagnostics"].append(projected)
        self.readiness_diagnostic = record
        path = self.ledger.path.parent / f"receiver-readiness-{self.group.process.pid}.json"
        try:
            write_private(path, record)
        except (OSError, WorkError):
            # Diagnostic failure cannot change the original refusal or grant readiness.
            record["retention_failed"] = True

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
                    self._retain_readiness(raw, "timeout")
                    raise DriverRefused("installed_receiver_readiness_timeout")
                chunk = os.read(stream.fileno(), 4096)
                if not chunk or len(raw) + len(chunk) > 16384:
                    self._retain_readiness(raw, "unavailable")
                    raise DriverRefused("installed_receiver_readiness_unavailable")
                raw.extend(chunk)
        first = bytes(raw).split(b"\n", 1)[0]
        envelope, mismatches = None, []
        try:
            envelope = strict_json(first)
            if type(envelope) is not dict:
                mismatches.append("envelope")
                raise ValueError()
            ready = envelope.get("data")
            checks = {
                "envelope_keys": set(envelope) == {"ok", "event", "data"},
                "ok": envelope.get("ok") is True,
                "event": envelope.get("event") == "receiver_ready",
                "data": type(ready) is dict,
            }
            if type(ready) is dict:
                checks.update(
                    {
                        "data_keys": set(ready)
                        == {
                            "instance_id",
                            "host",
                            "port",
                            "route",
                            "health_route",
                            "lifetime_seconds",
                        },
                        "host": ready.get("host") == "127.0.0.1",
                        "port": type(ready.get("port")) is int
                        and 1 <= ready["port"] <= 65535
                        and self.config["port"] in (0, ready["port"]),
                        "route": ready.get("route") == self.route,
                        "health_route": ready.get("health_route") == self.health_route,
                        "lifetime_seconds": ready.get("lifetime_seconds")
                        == self.config["lifetime_seconds"],
                    }
                )
            mismatches.extend(key for key, passed in checks.items() if not passed)
            if mismatches:
                raise ValueError()
            try:
                uuid(ready["instance_id"])
            except DriverRefused:
                mismatches.append("instance_id")
                raise ValueError() from None
        except (ValueError, KeyError, TypeError, WorkError):
            self._retain_readiness(first, "refused", envelope, mismatches or ("json",))
            raise DriverRefused("installed_receiver_readiness_mismatch") from None
        self._retain_readiness(first, "accepted", envelope)
        return ready

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
