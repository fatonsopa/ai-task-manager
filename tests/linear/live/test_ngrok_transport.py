"""Authored JSON observations and local processes, never a genuine ngrok connection."""

import hashlib
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from ai_task_manager.adapters.process.groups import GroupObservation, GroupStop
from ai_task_manager.adapters.tunnels.ngrok import NgrokRefused, NgrokTunnel, credential
from tests.linear.helpers import WEBHOOK

from .endpoint_authority import EndpointAuthority
from .endpoint_probe import EndpointProbe
from .ledger import DriverRefused
from .ngrok_transport import run, source_pins
from .test_endpoint_authority import authored_probe

pytest_plugins = ["tests.linear.live.test_endpoint_authority"]
TARGET = "http://127.0.0.1:32123"
ORIGIN = "https://authored-selected.example.net"
TOKEN = "synthetic-ngrok-contract-token"


def private(path, raw):
    path.write_text(raw)
    path.chmod(0o600)
    return path


def event(**changes):
    return {
        "lvl": "info",
        "msg": "started tunnel",
        "obj": "tunnels",
        "name": "owned-receiver",
        "addr": TARGET,
        "url": ORIGIN,
        **changes,
    }


@pytest.mark.parametrize(
    "body",
    [
        f"NGROK_AUTH_TOKEN={TOKEN}\n",
        f"NGROK_AUTHTOKEN='{TOKEN}' # selected\n",
        f"NGROK_AUTH_TOKEN={TOKEN}\nNGROK_AUTHTOKEN={TOKEN}\n",
    ],
)
def test_ng01_explicit_aliases_leave_source_unchanged(tmp_path, monkeypatch, body):
    source = private(tmp_path / "selected.env", body)
    monkeypatch.setenv("NGROK_AUTHTOKEN", "different-ambient-account")
    before = source.read_bytes(), source.stat()
    selected, pin = credential(source)
    assert selected == TOKEN
    pin.check()
    assert source.read_bytes() == before[0]
    assert source.stat().st_ino == before[1].st_ino
    assert source.stat().st_mtime_ns == before[1].st_mtime_ns


@pytest.mark.parametrize(
    "body",
    [
        "OTHER=value\n",
        "NGROK_AUTHTOKEN=\n",
        "NGROK_AUTHTOKEN=a\nNGROK_AUTH_TOKEN=b\n",
        "NGROK_AUTHTOKEN=a\nNGROK_AUTHTOKEN=a\n",
        "NGROK_AUTH_TOKEN='unfinished\n",
        "NGROK_AUTH_TOKEN='two words'\n",
    ],
)
def test_ng02_missing_conflicting_or_duplicate_credentials_refuse(tmp_path, body):
    with pytest.raises(NgrokRefused, match="credential_absent_or_ambiguous"):
        credential(private(tmp_path / "selected.env", body))


@pytest.fixture
def owned(tmp_path, monkeypatch):
    source = private(tmp_path / "selected.env", f"NGROK_AUTH_TOKEN={TOKEN}\n")
    children = []

    def start(rows=None, *, target=TARGET, lifetime=8, trailer="time.sleep(8)"):
        index = len(children)
        executable = tmp_path / f"authored-agent-{index}"
        rows = [event(addr=target)] if rows is None else rows
        # This independently authored executable checks the *actual* child environment/argv.
        executable.write_text(
            f"#!{Path(sys.executable).resolve()}\n"
            "import os, sys, time\n"
            f"assert os.environ['NGROK_AUTHTOKEN'] == {TOKEN!r}\n"
            "assert 'HOME' not in os.environ\n"
            "assert set(os.environ) <= {'NGROK_AUTHTOKEN','PATH','LC_ALL','LC_CTYPE',"
            "'__CF_USER_TEXT_ENCODING'}\n"
            "assert '--inspect=false' in sys.argv\n"
            "assert '--name=owned-receiver' in sys.argv\n"
            f"assert {TOKEN!r} not in repr(sys.argv)\n"
            + "\n".join(f"print({json.dumps(row)!r}, flush=True)" for row in rows)
            + "\n"
            + trailer
            + "\n"
        )
        executable.chmod(0o700)
        tunnel = NgrokTunnel(
            executable,
            hashlib.sha256(executable.read_bytes()).hexdigest(),
            source,
            target,
            tmp_path / f"run-{index}",
            lifetime=lifetime,
        )
        children.append(tunnel)
        tunnel.start()
        return tunnel

    monkeypatch.setenv("NGROK_AUTHTOKEN", "wrong-parent-account")
    monkeypatch.setenv("NGROK_API_KEY", "must-not-propagate")
    monkeypatch.setenv("HTTPS_PROXY", "must-not-propagate")
    yield start, source
    stops = []
    for tunnel in children:
        stopped = tunnel.stop()
        stops.append(stopped)
        assert stopped["confirmed_stop"] and stopped["reaped"]
        assert not tunnel.thread.is_alive()
    private(tmp_path / "ngrok-owned-stops.json", json.dumps(stops, indent=2))


def wait_observation(tunnel):
    until = time.monotonic() + 2
    while True:
        try:
            return tunnel.observe(tunnel.target)
        except NgrokRefused as error:
            if str(error) != "ngrok_endpoint_pending" or time.monotonic() >= until:
                raise
            time.sleep(0.01)


def test_ng03_owned_json_observation_and_environment_isolation(owned):
    start, _ = owned
    tunnel = start()
    observed = wait_observation(tunnel)
    assert observed["origin"] == ORIGIN and observed["upstream"] == TARGET
    assert observed["transport"] == "ngrok" and observed["pid"] == tunnel.group.process.pid
    assert observed["start_identity"] and observed["identity"]
    assert not any(TOKEN.encode() in p.read_bytes() for p in tunnel.root.rglob("*") if p.is_file())
    with pytest.raises(NgrokRefused, match="start_once"):
        tunnel.start()
    assert tunnel.stop()["confirmed_stop"]
    with pytest.raises(NgrokRefused):
        tunnel.observe(TARGET)


@pytest.mark.parametrize(
    "changes, code",
    [
        ({"addr": "http://127.0.0.1:32124"}, "ngrok_upstream_changed"),
        ({"url": "http://example.net"}, "ngrok_https_origin_required"),
        ({"url": "https://example.net/path"}, "ngrok_https_origin_required"),
        ({"url": "https://user:secret@example.net"}, "ngrok_https_origin_required"),
        ({"name": "foreign"}, "ngrok_endpoint_ambiguous"),
        ({"url": None}, "ngrok_https_origin_required"),
    ],
)
def test_ng04_wrong_upstream_or_malformed_endpoint_refuses(owned, changes, code):
    start, _ = owned
    tunnel = start([event(**changes)])
    with pytest.raises(NgrokRefused, match=code):
        wait_observation(tunnel)


@pytest.mark.parametrize(
    "rows, code",
    [
        ([event(), event()], "ngrok_endpoint_ambiguous"),
        ([event(), event(url="https://other.net")], "ngrok_endpoint_ambiguous"),
        ([{"lvl": "error", "msg": TOKEN}], "ngrok_agent_unhealthy"),
        (
            [event(), {"lvl": "info", "msg": "session reconnected"}],
            "ngrok_endpoint_observation_changed",
        ),
    ],
)
def test_ng05_ambiguous_or_unhealthy_output_never_exposes_raw_text(owned, rows, code):
    start, _ = owned
    tunnel = start(rows)
    until = time.monotonic() + 2
    while not tunnel.closing.is_set() and time.monotonic() < until:
        time.sleep(0.01)
    with pytest.raises(NgrokRefused, match=code) as refused:
        tunnel.observe(TARGET)
    assert TOKEN not in str(refused.value)
    assert not any(TOKEN.encode() in p.read_bytes() for p in tunnel.root.rglob("*") if p.is_file())


@pytest.mark.parametrize(
    "code, category",
    [
        ("ERR_NGROK_105", "invalid_token"),
        ("ERR_NGROK_107", "invalid_token"),
        ("ERR_NGROK_108", "account_session_limit"),
        ("ERR_NGROK_334", "endpoint_occupied"),
        ("ERR_NGROK_8012", "upstream_unavailable"),
    ],
)
def test_ng05_documented_error_codes_keep_only_safe_diagnostics(owned, code, category):
    start, _ = owned
    tunnel = start(
        [{"lvl": "error", "msg": "session failed", "err": TOKEN + " " + code}], trailer="pass"
    )
    with pytest.raises(NgrokRefused, match="agent_unhealthy"):
        wait_observation(tunnel)
    stopped = tunnel.stop()
    assert stopped["diagnostic"]["agent_code"] == code
    assert stopped["diagnostic"]["category"] == category
    assert stopped["diagnostic"]["stage"] == "starting"
    assert TOKEN not in json.dumps(stopped)


@pytest.mark.parametrize(
    "change", ["executable", "config", "credential", "directory", "argv", "target"]
)
def test_ng06_changed_binding_invalidates_observation(owned, change):
    start, source = owned
    tunnel = start()
    wait_observation(tunnel)
    if change in {"executable", "config", "credential"}:
        path = {
            "executable": tunnel.executable.path,
            "config": tunnel.config.path,
            "credential": source,
        }[change]
        path.write_bytes(path.read_bytes() + b"\n")
    elif change == "directory":
        tunnel.root.chmod(0o755)
    elif change == "argv":
        tunnel.group.process.args = ("changed",)
    with pytest.raises(NgrokRefused):
        tunnel.observe(TARGET + "1" if change == "target" else TARGET)


def test_ng07_unknown_liveness_refuses_without_signalling_foreign_processes(owned, monkeypatch):
    start, _ = owned
    tunnel = start()
    wait_observation(tunnel)
    with monkeypatch.context() as patch:
        patch.setattr(tunnel.group, "observe", lambda: GroupObservation(False, reason="authored"))
        with pytest.raises(NgrokRefused, match="process_unknown"):
            tunnel.observe(TARGET)


def test_ng08_deadline_stops_actual_owned_group_without_observation(owned):
    start, _ = owned
    tunnel = start(lifetime=0.25)
    tunnel.thread.join(3)
    assert not tunnel.thread.is_alive()
    assert tunnel.closed["confirmed_stop"] and tunnel.closed["reaped"]
    with pytest.raises(NgrokRefused, match="deadline"):
        tunnel.observe(TARGET)


@pytest.mark.parametrize("fault", ["link", "occupied", "pin"])
def test_ng09_prelaunch_guards_create_no_process(tmp_path, fault):
    executable = Path(sys.executable).resolve()
    root = tmp_path / "new-run"
    if fault == "link":
        linked = tmp_path / "linked"
        linked.symlink_to(executable)
        executable = linked
    if fault == "occupied":
        root.mkdir()
    expected = "0" * 64 if fault == "pin" else hashlib.sha256(executable.read_bytes()).hexdigest()
    with pytest.raises((NgrokRefused, FileExistsError)):
        NgrokTunnel(executable, expected, tmp_path / "never-read.env", TARGET, root)


def test_ng10_existing_authority_uses_ngrok_observation_and_receiver_guards(
    owned, endpoint, monkeypatch, tmp_path
):
    start, _ = owned
    _, server, _, _, ledger, _, _, _ = endpoint
    tunnel = start(target=f"http://127.0.0.1:{server.address[1]}")
    wait_observation(tunnel)
    monkeypatch.setattr(EndpointProbe, "__call__", authored_probe)
    authority = EndpointAuthority(server, tunnel, ledger, WEBHOOK, tmp_path / "authority")
    observed = authority.observe_transport()
    assert observed["authority"] == "transport_observation_only"
    assert observed["tunnel"]["origin"] == ORIGIN
    assert not authority.receipts
    reference = authority.admit()
    assert authority.verify(reference).url == ORIGIN + server.route
    assert tunnel.stop()["confirmed_stop"]
    with pytest.raises(DriverRefused, match="ngrok_observation_unavailable"):
        authority.verify(reference)
    assert server.stop(2)["stopped"]
    with pytest.raises(DriverRefused, match="receiver_not_running"):
        authority.observe_transport()


@pytest.mark.parametrize("fault", [None, "source", "bad_upstream"])
@pytest.mark.parametrize("credential_source", ["file", "environment"])
def test_ng11_transport_runner_has_no_provider_authority_and_cleans_up(
    adapter, tmp_path, monkeypatch, fault, credential_source
):
    from dataclasses import asdict

    source = private(tmp_path / "selected.env", f"NGROK_AUTH_TOKEN={TOKEN}\n")
    executable = tmp_path / "authored-runner-agent"
    executable.write_text(
        f"#!{Path(sys.executable).resolve()}\n"
        "import json, sys, time\n"
        f"row = {event()!r}\n"
        + ("row['addr'] = sys.argv[2]\n" if fault != "bad_upstream" else "")
        + "print(json.dumps(row), flush=True)\ntime.sleep(10)\n"
    )
    executable.chmod(0o700)
    packet = {
        "run_id": str(uuid4()),
        "scope": asdict(adapter[2].scope),
        "webhook_id": str(uuid4()),
        "root": str(tmp_path / "transport-run"),
        "executable": str(executable),
        "credential_file": str(source),
        "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "source_sha256": source_pins(),
    }
    if credential_source == "environment":
        packet.pop("credential_file")
        packet["credential_environment"] = "AITM_CREDENTIAL_Ngrok_Runner"
        monkeypatch.setenv(packet["credential_environment"], TOKEN)
        monkeypatch.setenv("NGROK_AUTHTOKEN", "wrong-ambient-account")
    monkeypatch.setattr(EndpointProbe, "__call__", authored_probe)
    if fault == "source":
        packet["source_sha256"][next(iter(packet["source_sha256"]))] = "0" * 64
        with pytest.raises(DriverRefused, match="packet_source_changed"):
            run(packet)
        assert not Path(packet["root"]).exists()
        return
    result = run(packet)
    assert result["passed"] is (fault is None)
    assert result["provider_mutations"] == 0
    assert result["tunnel_cleanup"]["confirmed_stop"]
    assert result["tunnel_cleanup"]["reaped"]
    assert result["receiver_cleanup"]["stopped"]
    assert result["elapsed_seconds"] < 120
    if fault is None:
        assert len(result["observation"]["probe"]["result"]["observations"]) == 3
        assert result["observation"]["authority"] == "transport_observation_only"
    else:
        assert result["failure_category"] == "upstream_mismatch"
    assert TOKEN not in (Path(packet["root"]) / "result.json").read_text()


@pytest.mark.parametrize("fault", ["unknown", "exception"])
def test_ng12_unknown_cleanup_cannot_claim_a_healthy_or_stopped_transport(
    owned, monkeypatch, tmp_path, fault
):
    start, _ = owned
    tunnel = start()
    wait_observation(tunnel)
    with monkeypatch.context() as patch:

        def failed_stop():
            if fault == "exception":
                raise OSError("authored failure with " + TOKEN)
            return GroupStop(
                False, GroupObservation(False, reason="authored_unknown"), reason="authored_unknown"
            )

        patch.setattr(tunnel.group, "stop", failed_stop)
        stopped = tunnel.stop()
        assert not stopped["confirmed_stop"] and not stopped["reaped"]
        assert TOKEN not in json.dumps(stopped)
        private(tmp_path / "unknown-cleanup.json", json.dumps(stopped))
        with pytest.raises(NgrokRefused):
            tunnel.observe(TARGET)
    # End only this authored fault; the real owner then inspects/stops its original child.
    tunnel.closed = None
    assert tunnel.stop()["confirmed_stop"]


@pytest.mark.parametrize(
    "trailer, code",
    [
        ("print('not json', flush=True); time.sleep(8)", "ngrok_output_invalid"),
        ("print('x' * 70000, flush=True); time.sleep(8)", "ngrok_output_bound"),
        ("os.close(sys.stdout.fileno()); time.sleep(8)", "ngrok_output_closed"),
    ],
)
def test_ng12_malformed_bounded_or_closed_output_refuses(owned, trailer, code):
    start, _ = owned
    tunnel = start([], trailer=trailer)
    with pytest.raises(NgrokRefused, match=code):
        wait_observation(tunnel)


@pytest.mark.parametrize("fault", ["link", "permissions", "replacement"])
def test_ng12_credential_source_guards(tmp_path, fault):
    source = private(tmp_path / "source.env", f"NGROK_AUTH_TOKEN={TOKEN}\n")
    if fault == "link":
        link = tmp_path / "alias.env"
        link.symlink_to(source)
        with pytest.raises(NgrokRefused, match="symbolic_link"):
            credential(link)
    elif fault == "permissions":
        from ai_task_manager.domains.work.errors import AccessDenied

        source.chmod(0o644)
        with pytest.raises(AccessDenied):
            credential(source)
    else:
        _, pin = credential(source)
        source.rename(tmp_path / "old.env")
        private(source, f"NGROK_AUTH_TOKEN={TOKEN}\n")
        with pytest.raises(NgrokRefused, match="file_changed"):
            pin.check()


@pytest.mark.parametrize("upstream", ["127.0.0.1:32123", "127.0.0.1:32124"])
def test_ng05_connection_records_preserve_only_the_matching_upstream(owned, upstream):
    start, _ = owned
    tunnel = start(
        [
            event(),
            {
                "lvl": "info",
                "msg": "join connections",
                "obj": "join",
                "l": upstream,
                "r": "authored-client-private-data",
            },
        ]
    )
    if upstream.endswith("32124"):
        with pytest.raises(NgrokRefused, match="upstream_changed"):
            wait_observation(tunnel)
    else:
        assert wait_observation(tunnel)["origin"] == ORIGIN
        assert "authored-client-private-data" not in json.dumps(tunnel.stop())
