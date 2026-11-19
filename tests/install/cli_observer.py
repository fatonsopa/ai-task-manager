"""Count pre-I/O dispatch attempts around the actual installed aitm entry point.

Production mode permits only the genuine default provider URL. Authored mode
selects one explicit loopback control; it never changes the operator manifest.
Only numeric rate headers and allowlisted error codes are retained; credentials,
request bodies, response bodies and freeform errors are never retained.
"""

import argparse
import atexit
import hashlib
import json
import os
import re
import runpy
import sqlite3
import sys
import sysconfig
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

# The source checkout supplies only reviewed test helpers; never add its src/ tree.
CHECKOUT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CHECKOUT))

from ai_task_manager.adapters.linear.transport import HttpTransport  # noqa: E402
from ai_task_manager.config.files import private_path, strict_json, write_private  # noqa: E402
from tests.linear.live.ledger import DriverRefused, Limits, RunLedger, Scope  # noqa: E402

_NUMERIC_HEADERS = frozenset(
    {
        "x-ratelimit-requests-limit",
        "x-ratelimit-requests-remaining",
        "x-ratelimit-requests-reset",
        "x-ratelimit-complexity-limit",
        "x-ratelimit-complexity-remaining",
        "x-ratelimit-complexity-reset",
        "x-complexity",
        "x-ratelimit-endpoint-requests-limit",
        "x-ratelimit-endpoint-requests-remaining",
        "x-ratelimit-endpoint-requests-reset",
    }
)
_ERROR_CODES = frozenset(
    {
        "RATELIMITED",
        "RATE_LIMITED",
        "INTERNAL_SERVER_ERROR",
        "UNAUTHENTICATED",
        "FORBIDDEN",
        "GRAPHQL_VALIDATION_FAILED",
        "BAD_USER_INPUT",
    }
)


def open_ledger(path):
    path = private_path(path)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT value FROM meta").fetchall()
    if len(rows) != 1:
        raise DriverRefused("original_ledger_binding_required")
    value = strict_json(rows[0][0])
    if set(value) != {"run", "scope", "limits"}:
        raise DriverRefused("original_ledger_binding_required")
    return RunLedger(path, value["run"], Scope(**value["scope"]), Limits(**value["limits"]))


class DispatchObserver:
    def __init__(self, ledger, endpoint, receipt, provenance):
        self.ledger, self.endpoint, self.receipt = ledger, endpoint, Path(receipt)
        if self.receipt.exists() or self.receipt.is_symlink():
            raise DriverRefused("new_observer_receipt_required")
        self.record = {
            **provenance,
            "dispatches": [],
            "http": [],
            "classification": "pre_io_dispatch_attempts",
            "observation_errors": 0,
        }
        self.saved, self.closed, self.active = False, False, {}
        self.lock = threading.RLock()
        self.save()
        self.code = HttpTransport._send.__code__
        self.tool = next((i for i in range(6) if sys.monitoring.get_tool(i) is None), None)
        if self.tool is None:
            raise DriverRefused("preparation_http_monitor_unavailable")
        sys.monitoring.use_tool_id(self.tool, "aitm-preparation-http")
        sys.monitoring.register_callback(self.tool, sys.monitoring.events.PY_RETURN, self._returned)
        sys.monitoring.set_local_events(self.tool, self.code, sys.monitoring.events.PY_RETURN)
        atexit.register(self.close)

    def save(self):
        # The first publication stays exclusive even if two callers race on a path.
        with self.lock:
            write_private(self.receipt, self.record, replace=self.saved)
            self.saved = True

    def audit(self, event, arguments):
        if self.closed or event != "urllib.Request":
            return
        # CPython's event includes headers. Deliberately never inspect or retain them.
        url, data, _, method = arguments
        if url != self.endpoint or method != "POST":
            raise DriverRefused("unexpected_product_dispatch")
        payload = strict_json(data)
        if (
            type(payload) is not dict
            or set(payload) != {"query", "variables"}
            or type(payload["query"]) is not str
            or type(payload["variables"]) is not dict
        ):
            raise DriverRefused("unexpected_product_request")
        query = payload["query"]
        operation = "mutation" if query.lstrip().startswith("mutation ") else "query"
        self.ledger.request("product_transport")
        with self.lock:
            self.record["dispatches"].append(
                {
                    "index": len(self.record["dispatches"]) + 1,
                    "operation": operation,
                    "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
                    "variables_sha256": hashlib.sha256(
                        json.dumps(
                            payload["variables"],
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ).encode()
                    ).hexdigest(),
                }
            )
            self.active[threading.get_ident()] = {
                "dispatch_index": len(self.record["dispatches"]),
                "query_sha256": self.record["dispatches"][-1]["query_sha256"],
                "variables_sha256": self.record["dispatches"][-1]["variables_sha256"],
                "thread": threading.get_ident(),
                "pid": os.getpid(),
                "started_at": time.time(),
                "started_monotonic": time.monotonic(),
            }
        self.save()

    def _returned(self, code, offset, value):
        if code is not self.code or self.closed:
            return
        try:
            with self.lock:
                row = self.active.pop(threading.get_ident(), None)
                if row is None:
                    return  # No accepted urllib audit entry means no correlated HTTP claim.
                row.update(
                    status=value.status,
                    finished_at=time.time(),
                    elapsed_seconds=time.monotonic() - row["started_monotonic"],
                    boundary="HttpTransport._send returned HTTP response",
                    provider_receipt="Application delivery not established",
                )
                headers = {name.lower(): raw for name, raw in value.headers.items()}
                row["quota_headers"] = {
                    name: raw
                    for name, raw in headers.items()
                    if name in _NUMERIC_HEADERS
                    and type(raw) is str
                    and len(raw) <= 32
                    and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw)
                }
                retry = headers.get("retry-after")
                row["retry_after"] = (
                    retry
                    if (
                        type(retry) is str
                        and len(retry) <= 64
                        and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", retry)
                    )
                    else None
                )
                row["error_codes"], row["error_code_hashes"] = [], []
                if len(value.body) <= 65536:
                    try:
                        data = json.loads(value.body)
                    except (ValueError, UnicodeError):
                        data = None
                    errors = data.get("errors") if isinstance(data, dict) else None
                    for error in errors[:8] if isinstance(errors, list) else ():
                        extension = error.get("extensions") if isinstance(error, dict) else None
                        error_code = extension.get("code") if isinstance(extension, dict) else None
                        if isinstance(error_code, str) and len(error_code) <= 128:
                            if error_code in _ERROR_CODES:
                                row["error_codes"].append(error_code)
                            else:
                                row["error_code_hashes"].append(
                                    hashlib.sha256(error_code.encode()).hexdigest()
                                )
                row["caller"] = []
                frame = sys._getframe(1)
                for _ in range(16):
                    if frame is None:
                        break
                    source = Path(frame.f_code.co_filename)
                    if "ai_task_manager" in source.parts and source.name != "transport.py":
                        row["caller"].append(
                            {
                                "source": source.name,
                                "function": frame.f_code.co_name,
                                "line": frame.f_lineno,
                            }
                        )
                        if len(row["caller"]) == 4:
                            break
                    frame = frame.f_back
                del frame
                if len(self.record["http"]) < self.ledger.limits.requests:
                    self.record["http"].append(row)
                self.save()  # Persist before LinearClient can discard the WireResponse metadata.
        except BaseException:
            # Observation cannot replace the original response or client exception.
            self.record["observation_errors"] += 1

    def close(self):
        if self.closed:
            return
        sys.monitoring.set_local_events(self.tool, self.code, 0)
        if sys.monitoring.get_events(self.tool) != 0:
            raise DriverRefused("preparation_monitor_global_events_changed")
        sys.monitoring.register_callback(self.tool, sys.monitoring.events.PY_RETURN, None)
        sys.monitoring.free_tool_id(self.tool)
        self.closed = True
        self.record["http_monitoring_closed"] = True
        self.record["unreturned_dispatches"] = len(self.record["dispatches"]) - len(
            self.record["http"]
        )
        self.save()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--authored-endpoint")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    selected_command = argparse.ArgumentParser(add_help=False)
    selected_command.add_argument("--config", required=True)
    selected_command.add_argument("--json", action="store_true")
    selected_command.add_argument(
        "group", choices=["workflow", "session", "writing", "task", "event", "sync"]
    )
    selected_command.add_argument("action")
    selected_command.add_argument("--input")
    selected = selected_command.parse_args(command)
    if (selected.group, selected.action) not in {
        ("workflow", "prepare"),
        ("session", "setup"),
        ("session", "doctor"),
        ("writing", "prepare"),
        ("writing", "review"),
        ("writing", "finalize"),
        ("task", "submit"),
        ("task", "decision_prepare"),
        ("task", "decision_request"),
        ("task", "decision_resolve"),
        ("task", "decision_show"),
        ("task", "progress"),
        ("task", "action"),
        ("task", "list"),
        ("task", "show"),
        ("event", "list"),
        ("sync", "now"),
    }:
        raise DriverRefused("installed_preparation_command_required")
    if (selected.group, selected.action) == ("task", "action"):
        if (
            not selected.input
            or selected.input == "-"
            or strict_json(private_path(selected.input).read_bytes()).get("action") != "enqueue"
        ):
            raise DriverRefused("bounded_installed_enqueue_required")
    if "PYTHONPATH" in os.environ:
        raise DriverRefused("pythonpath_must_be_absent")
    import ai_task_manager
    import ai_task_manager.presentation.cli.main as cli_module

    package = Path(ai_task_manager.__file__).resolve()
    if not package.is_relative_to(Path(sysconfig.get_path("purelib")).resolve()):
        raise DriverRefused("installed_package_required")
    python = Path(sys.executable).absolute()  # Preserve the selected venv invocation path.
    entry = python.parent / "aitm"
    endpoint = HttpTransport.ENDPOINT
    mode = "genuine_default_provider"
    if args.authored_endpoint:
        selected = urlsplit(args.authored_endpoint)
        if (
            selected.scheme != "http"
            or selected.hostname != "127.0.0.1"
            or selected.username
            or selected.password
            or selected.query
            or selected.fragment
            or selected.path not in {"", "/", "/graphql"}
        ):
            raise DriverRefused("authored_loopback_endpoint_required")
        endpoint = args.authored_endpoint
        HttpTransport.ENDPOINT = endpoint
        mode = "authored_loopback_control"
    from ai_task_manager.adapters.workflow_control.identity import source_digest

    ledger_source = sys.modules[RunLedger.__module__].__file__
    if ledger_source is None:
        raise DriverRefused("ledger_helper_source_required")
    provenance = {
        "mode": mode,
        "observer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "ledger_helper_sha256": hashlib.sha256(Path(ledger_source).read_bytes()).hexdigest(),
        "source_digest": source_digest(),
        "python_sha256": hashlib.sha256(python.read_bytes()).hexdigest(),
        "cli_module_sha256": hashlib.sha256(Path(cli_module.__file__).read_bytes()).hexdigest(),
        "python": str(python),
        "entry": str(entry),
        "entry_sha256": hashlib.sha256(entry.read_bytes()).hexdigest(),
        "package": str(package),
        "cli_module": str(Path(cli_module.__file__).resolve()),
        "cwd": str(Path.cwd().resolve()),
        "argv": [str(entry), *command],
        "pythonpath_absent": True,
        "warning": "Dispatch attempts do not prove provider receipt or successful effects.",
    }
    observer = DispatchObserver(open_ledger(args.ledger), endpoint, args.receipt, provenance)
    sys.addaudithook(observer.audit)
    sys.argv = [str(entry), *command]
    try:
        runpy.run_path(str(entry), run_name="__main__")
    except SystemExit as error:
        observer.record["exit_code"] = error.code
        raise
    finally:
        observer.close()


if __name__ == "__main__":
    main()
