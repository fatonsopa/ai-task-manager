"""Real credential-free owned status processes, never the installed Codex CLI."""

from dataclasses import replace
from uuid import uuid4

import pytest

from ai_task_manager.domains.work.errors import AccessDenied
from tests.hosts.codex_configuration_helpers import policy, status
from tests.hosts.test_composition import composition as composition
from tests.hosts.test_composition import start


def test_actual_factory_preflight_precedes_every_forced_login_launch(composition):
    value = composition()
    attempt = start(value)
    native = value[4].owned[attempt.id]
    assert native.process.command.argv[-3:] == ("app-server", "--strict-config", "--stdio")
    outcomes = native.permits.outcomes
    assert outcomes[0][0] == "authored-status-control"
    assert native.startup.ready_ref is not None
    assert native.startup.configuration.inventory_complete


def test_positive_status_has_confirmed_stop_and_scoped_safe_receipt(tmp_path, binding, journal):
    p = policy(tmp_path / "owned")
    probe, permits, ref = status(p, binding, journal, tmp_path)
    data = journal._read(ref)["payload"]
    assert data["auth_method"] == "chatgpt" and data["group"]["confirmed_stop"]
    assert data["counts"] == {"stdout": 0, "stderr": 24}
    assert permits.calls[0] == "status-authorized"
    probe.consume(p, binding, ref)


def test_wrong_unknown_or_ambiguous_status_never_leaks_credentials(tmp_path, binding, journal):
    sentinel = "fixture-secret-" + uuid4().hex
    for index, output in enumerate(
        (
            "Logged in using an API key - " + sentinel,
            "prefix Logged in using ChatGPT",
            "Logged in using ChatGPT\nwarning",
            "",
        )
    ):
        p = policy(
            tmp_path / str(index), program="import sys\nsys.stderr.write(" + repr(output) + ")\n"
        )
        with pytest.raises(AccessDenied) as error:
            status(p, binding, journal, tmp_path)
        assert sentinel not in str(error.value)
    assert sentinel not in str(journal._history("trace"))
    assert not list(tmp_path.glob("*.stdout")) and not list(tmp_path.glob("*.stderr"))


def test_preflight_timeout_output_limit_or_unknown_stop_cannot_launch(
    tmp_path, binding, journal, monkeypatch
):
    from ai_task_manager.adapters.hosts.bridge import codex_auth

    programs = [
        "import sys\nsys.stderr.write('x'*20000)\n",
        "import signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\ntime.sleep(20)\n",
    ]
    child = (
        "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "print('READY',flush=True);time.sleep(20)"
    )
    programs.extend(
        [
            "import subprocess,sys,time\n"
            f"child=subprocess.Popen([sys.executable,'-u','-c',{child!r}],stdout=subprocess.PIPE)\n"
            "assert child.stdout.readline()==b'READY\\n'\n" + ending
            for ending in ("time.sleep(20)\n", "sys.stderr.write('Logged in using ChatGPT\\n')\n")
        ]
    )
    for index, program in enumerate(programs):
        p = policy(tmp_path / str(index), program=program)
        with pytest.raises(AccessDenied):
            status(p, replace(binding, deadline=journal.clock() + 0.7), journal, tmp_path)
    groups = [
        row["payload"]["group"]
        for row in journal._history("trace")
        if row["payload"].get("effect") == "codex_status_process"
    ]
    assert any(group["orphaned"] for group in groups)
    assert any("SIGKILL" in group["signals"] and group["confirmed_stop"] for group in groups)
    real = codex_auth.supervise

    def unknown(*args, **kwargs):
        result = real(*args, **kwargs)
        assert result["group"]["confirmed_stop"]
        result["group"]["confirmed_stop"] = False  # Authored loss-of-observation control.
        return result

    monkeypatch.setattr(codex_auth, "supervise", unknown)
    with pytest.raises(AccessDenied):
        status(policy(tmp_path / "unknown"), binding, journal, tmp_path)


def test_auth_receipt_is_fresh_single_launch_and_exactly_bound(tmp_path, binding, journal):
    p = policy(tmp_path)
    probe, _, ref = status(p, binding, journal, tmp_path)
    for changed in (replace(binding, worker_id="other"), replace(binding, attempt_id="other")):
        with pytest.raises(AccessDenied):
            probe.consume(p, changed, ref)
    probe.consume(p, binding, ref)
    with pytest.raises(AccessDenied):
        probe.consume(p, binding, ref)
    _, _, fresh = status(p, binding, journal, tmp_path)
    (p.codex_settings.sources.home / "auth.json").write_text("changed-fixture-cache")
    with pytest.raises(AccessDenied):
        probe.consume(p, binding, fresh)
    _, _, fresh = status(p, binding, journal, tmp_path)
    now = journal.clock()
    journal.clock = lambda: now + 61
    with pytest.raises(AccessDenied):
        probe.consume(p, binding, fresh)


def test_existing_auth_constraints_are_not_used_to_discover_a_mismatch(tmp_path):
    for index, config in enumerate(
        (
            'forced_login_method="api"',
            'forced_chatgpt_workspace_id="foreign"',
            'openai_base_url="https://fixture.invalid"',
            'cli_auth_credentials_store="keyring"',
        )
    ):
        with pytest.raises(AccessDenied):
            policy(tmp_path / str(index), config=config)
    assert not list(tmp_path.rglob("*.stdout"))
