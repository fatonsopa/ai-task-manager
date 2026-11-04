"""Observe endpoint replies in a finite owned process, without credentials."""

import hashlib
import http.client
import json
import os
import stat
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from ai_task_manager.adapters.process.groups import OwnedProcessGroup, supervise

from .installed_endpoint import InstalledReceiverProcess
from .ledger import DriverRefused, Limits, RunLedger, Scope, uuid
from .receiver import BoundedReceiver


def private_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(value, output, sort_keys=True, indent=2)
        output.write("\n")


class EndpointNotReady(DriverRefused):
    """A stopped probe observed owned local health but no public route yet."""

    def __init__(self, observation):
        super().__init__("endpoint_public_not_ready")
        self.observation = observation


def public_not_ready(result):
    rows = result.get("observations", [])
    return (
        result.get("passed") is False
        and result.get("error_type") is None
        and len(rows) == 3
        and [row.get("label") for row in rows] == ["local_health", "public_health", "public_route"]
        and rows[0].get("status") == 200
        and rows[0].get("run_matches") is True
        and all(row.get("status") == 404 for row in rows[1:])
    )


def origin_parts(origin):
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.port not in {None, 443}
    ):
        raise DriverRefused("endpoint_https_origin_required")
    return parsed


def _request(origin, route, ledger, label, health_identity):
    parsed = urlsplit(origin)
    connection_type = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_type(parsed.hostname, parsed.port, timeout=2)
    # Each actual delegation is durably charged in the same existing ledger.
    ledger.request("read")
    started = time.monotonic()
    try:
        connection.request("GET", route, headers={"Connection": "close"})
        reply = connection.getresponse()
        length = reply.getheader("Content-Length")
        if (
            reply.getheader("Transfer-Encoding") is not None
            or length is None
            or not length.isdecimal()
            or int(length) > 4096
        ):
            raise DriverRefused("endpoint_response_unbounded")
        body = reply.read(int(length))
        if len(body) != int(length):
            raise DriverRefused("endpoint_response_incomplete")
        matched = False
        if label.endswith("health"):
            try:
                matched = json.loads(body) == health_identity
            except ValueError:
                pass
        return {
            "label": label,
            "status": reply.status,
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "body_empty": not body,
            "run_matches": matched,
            "started_at_monotonic": started,
            "completed_at_monotonic": time.monotonic(),
        }
    finally:
        connection.close()


def worker(job_path):
    job = json.loads(job_path.read_text())
    private_json(job_path.parent / "stage.json", {"stage": "ledger_open_begin", "pid": os.getpid()})
    ledger = RunLedger(job["ledger"], job["run_id"], Scope(**job["scope"]), Limits(**job["limits"]))
    origin_parts(job["origin"])
    observations = []
    try:
        kind = job.get("receiver_kind", "fixture")
        if kind == "fixture":
            health_identity, route_status = {"run_id": job["run_id"]}, 404
        elif kind == "installed_cli":
            health_identity = {"instance_id": uuid(job["instance_id"])}
            route_status = 405
        else:
            raise DriverRefused("endpoint_receiver_kind_unknown")
        for origin, route, label in (
            (job["local_origin"], job["health_route"], "local_health"),
            (job["origin"], job["health_route"], "public_health"),
            (job["origin"], job["route"], "public_route"),
        ):
            observations.append(_request(origin, route, ledger, label, health_identity))
        passed = (
            all(row["status"] == 200 and row["run_matches"] for row in observations[:2])
            and observations[2]["status"] == route_status
            and observations[2]["body_empty"]
        )
        result = {"passed": passed, "observations": observations, "error_type": None}
    except Exception as error:
        result = {
            "passed": False,
            "observations": observations,
            "error_type": type(error).__name__,
        }
    private_json(job_path.parent / "result.json", result)
    return 0 if result["passed"] else 1


class EndpointProbe:
    def __init__(self, ledger, private_root, *, seconds=8):
        if type(seconds) not in {int, float} or not 0 < seconds <= 8:
            raise DriverRefused("invalid_endpoint_probe_bound")
        self.ledger, self.root, self.seconds = ledger, Path(private_root), seconds
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.getuid()
        ):
            raise DriverRefused("private_endpoint_directory_required")

    def __call__(self, receiver, origin):
        origin_parts(origin)
        if isinstance(receiver, InstalledReceiverProcess):
            actual = receiver.observe()
            selected = {"receiver_kind": "installed_cli", "instance_id": actual["instance_id"]}
        elif isinstance(receiver, BoundedReceiver):
            selected = {"receiver_kind": "fixture"}
        else:
            raise DriverRefused("owned_endpoint_receiver_required")
        folder = self.root / str(uuid4())
        folder.mkdir(mode=0o700)
        (folder / "home").mkdir(mode=0o700)
        address, port = receiver.address
        if address != "127.0.0.1":
            raise DriverRefused("endpoint_loopback_receiver_required")
        private_json(
            folder / "job.json",
            {
                **selected,
                "ledger": str(self.ledger.path),
                "run_id": self.ledger.run_id,
                "scope": asdict(self.ledger.scope),
                "limits": asdict(self.ledger.limits),
                "origin": origin,
                "local_origin": f"http://127.0.0.1:{port}",
                "health_route": receiver.health_route,
                "route": receiver.route,
            },
        )
        source = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        repo = Path(__file__).resolve().parents[3]
        child = group = result = None
        error = None
        cleanup_error = None
        stop = None
        with (folder / "output.log").open("xb") as output:
            os.chmod(folder / "output.log", 0o600)
            try:
                child = subprocess.Popen(
                    [sys.executable, "-B", "-m", __name__, str(folder / "job.json")],
                    cwd=repo,
                    env={
                        "HOME": str(folder / "home"),
                        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                        "PYTHONPATH": str(repo) + os.pathsep + str(repo / "src"),
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "LC_ALL": "C",
                    },
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                group = OwnedProcessGroup(child)
                group.capture()
                result = supervise(group, timeout_seconds=self.seconds)
                stop = result["group"]
            except BaseException as caught:
                error = caught
            finally:
                if child is not None and (stop is None or not stop.get("confirmed_stop")):
                    if group is not None and child.returncode is None:
                        try:
                            observed = group.stop()
                            stop = group.evidence(observed, orphaned=False)
                            if observed.confirmed:
                                group.reap_exited_leader()
                        except BaseException as caught:
                            cleanup_error = caught
                    if stop is None:
                        stop = {"confirmed_stop": False, "reason": "ownership_unknown"}
                ownership = {
                    "pid": child.pid if child else None,
                    "returncode": child.returncode if child else None,
                    "reaped": child is not None and child.returncode is not None,
                    "group": stop,
                    "supervision": result,
                    "error_type": type(error).__name__ if error else None,
                    "cleanup_error_type": type(cleanup_error).__name__ if cleanup_error else None,
                    "source_sha256": source,
                    "source_unchanged": source
                    == hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                }
                private_json(folder / "ownership.json", ownership)
        if (
            error is not None
            or cleanup_error is not None
            or child is None
            or child.returncode not in {0, 1}
            or not ownership["reaped"]
            or not stop
            or not stop["confirmed_stop"]
            or not ownership["source_unchanged"]
        ):
            raise DriverRefused("endpoint_probe_not_confirmed")
        try:
            actual = json.loads((folder / "result.json").read_text())
        except (OSError, ValueError) as error:
            raise DriverRefused("endpoint_probe_not_confirmed") from error
        observation = {"result": actual, "ownership": ownership, "evidence": str(folder)}
        if child.returncode == 1 and public_not_ready(actual):
            raise EndpointNotReady(observation)
        if child.returncode != 0 or actual.get("passed") is not True:
            raise DriverRefused("endpoint_probe_not_confirmed")
        return observation


if __name__ == "__main__":
    raise SystemExit(worker(Path(sys.argv[1])))
