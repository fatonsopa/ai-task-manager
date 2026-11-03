"""Explicit 120-second transport-only run; never creates provider resources."""

import hashlib
import signal
import sys
import time
from pathlib import Path

from ai_task_manager.adapters.linear.models import HttpResponse
from ai_task_manager.adapters.tunnels.ngrok import NgrokRefused, NgrokTunnel
from ai_task_manager.config.files import read_private

from .endpoint_authority import EndpointAuthority
from .endpoint_probe import private_json
from .ledger import DriverRefused, Limits, RunLedger, Scope, uuid
from .receiver import BoundedReceiver

SOURCE_FILES = (
    "src/ai_task_manager/adapters/tunnels/ngrok.py",
    "src/ai_task_manager/adapters/process/groups.py",
    "src/ai_task_manager/config/files.py",
    "src/ai_task_manager/config/environment.py",
    "tests/linear/live/ngrok_transport.py",
    "tests/linear/live/endpoint_authority.py",
    "tests/linear/live/endpoint_probe.py",
    "tests/linear/live/receiver.py",
    "tests/linear/live/run_primary.py",
    "tests/linear/live/ledger.py",
)


def source_pins():
    root = Path(__file__).resolve().parents[3]
    pins = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in SOURCE_FILES}
    for name in SOURCE_FILES:
        if name.startswith("src/"):
            module = name.removeprefix("src/").removesuffix(".py").replace("/", ".")
            loaded = Path(sys.modules[module].__file__)
            if hashlib.sha256(loaded.read_bytes()).hexdigest() != pins[name]:
                raise DriverRefused("transport_runtime_source_changed")
    return pins


class RejectEvents:
    """Before provider admission there is no webhook secret or task authority."""

    def receive(self, *args, **kwargs):
        return HttpResponse(503)


def run(packet):
    required = {
        "run_id",
        "scope",
        "webhook_id",
        "root",
        "executable",
        "executable_sha256",
        "source_sha256",
    }
    if set(packet) not in (required | {"credential_file"}, required | {"credential_environment"}):
        raise DriverRefused("transport_packet_fields_required")
    if packet["source_sha256"] != source_pins():
        raise DriverRefused("transport_packet_source_changed")
    uuid(packet["run_id"])
    uuid(packet["webhook_id"])
    scope = Scope(**packet["scope"])
    scope.check()
    root = Path(packet["root"]).absolute()
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise DriverRefused("transport_directory_link_refused")
    root.mkdir(mode=0o700)
    started = time.time()
    ledger = RunLedger(
        root / "run.sqlite",
        packet["run_id"],
        scope,
        Limits(
            started + 120, requests=7, started_at=started, cleanup_requests=1, cleanup_seconds=30
        ),
    )
    receiver = tunnel = None
    result = {
        "passed": False,
        "authority": "transport_observation_only",
        "provider_mutations": 0,
        "health_probe_limit": 3,
        "prior_interruption": "Automated endpoint interruption retained; cause unknown.",
    }
    try:
        receiver = BoundedReceiver(RejectEvents(), ledger)
        receiver.start()
        if not receiver.loop_entered.wait(1):
            raise DriverRefused("transport_receiver_not_running")
        target = f"http://127.0.0.1:{receiver.address[1]}"
        tunnel = NgrokTunnel(
            packet["executable"],
            packet["executable_sha256"],
            packet.get("credential_file"),
            target,
            root / "tunnel",
            lifetime=90,
            credential_environment=packet.get("credential_environment"),
        )
        tunnel.start()
        discovery = time.monotonic() + 30
        while True:
            try:
                tunnel.observe(target)
                break
            except NgrokRefused as error:
                if str(error) != "ngrok_endpoint_pending":
                    raise
                if time.monotonic() >= discovery:
                    raise NgrokRefused("ngrok_discovery_timeout") from None
                time.sleep(0.05)
        authority = EndpointAuthority(
            receiver, tunnel, ledger, packet["webhook_id"], root / "endpoint"
        )
        if packet["source_sha256"] != source_pins():
            raise DriverRefused("transport_packet_source_changed")
        result["observation"] = authority.observe_transport()
        if packet["source_sha256"] != source_pins():
            raise DriverRefused("transport_packet_source_changed")
        result["passed"] = True
    except Exception as error:
        # External exception text may contain credentials or arbitrary response text.
        result["error_type"] = type(error).__name__
        if isinstance(error, NgrokRefused):
            result["failure_category"] = {
                "ngrok_discovery_timeout": "timeout",
                "ngrok_upstream_changed": "upstream_mismatch",
                "ngrok_credential_absent_or_ambiguous": "credential_configuration",
            }.get(str(error), "transport_refused")
    finally:
        try:
            result["tunnel_cleanup"] = tunnel.stop() if tunnel else {"confirmed_stop": True}
        except Exception:
            result["tunnel_cleanup"] = {"confirmed_stop": False, "reason": "cleanup_unknown"}
        try:
            result["receiver_cleanup"] = receiver.stop(12) if receiver else {"stopped": True}
        except Exception:
            result["receiver_cleanup"] = {"stopped": False, "reason": "cleanup_unknown"}
        result["elapsed_seconds"] = time.time() - started
        result["passed"] = bool(
            result["passed"]
            and result["tunnel_cleanup"]["confirmed_stop"]
            and result["receiver_cleanup"]["stopped"]
            and result["elapsed_seconds"] <= 120
        )
        private_json(root / "result.json", result)
    return result


def main():
    # The runner is an explicitly invoked live effect, never an import-time action.
    packet = read_private(sys.argv[1])

    def interrupt(signum, frame):
        raise DriverRefused("transport_interrupted")

    for value in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
        signal.signal(value, interrupt)
    signal.alarm(90)  # Reserve 30 seconds for owned cleanup within the 120-second packet.
    try:
        result = run(packet)
    finally:
        signal.alarm(0)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
