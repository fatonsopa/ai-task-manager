"""Credential-free authored controls; never actual Codex account/conformance evidence."""

import hashlib
import sys
from dataclasses import replace
from uuid import uuid4

from ai_task_manager.adapters.hosts.bridge.codex_auth import CodexAuthProbe
from ai_task_manager.adapters.hosts.bridge.codex_configuration import CodexConfigurationObserver
from ai_task_manager.adapters.hosts.bridge.codex_home import CodexHomeBoundary
from ai_task_manager.adapters.hosts.bridge.codex_settings import RESTRICTIONS, CodexSettings
from ai_task_manager.adapters.hosts.bridge.policy import (
    NativePolicy,
    clean_environment,
    policy_digest,
)
from ai_task_manager.adapters.hosts.bridge.startup import StartupObserver
from ai_task_manager.config.files import digest
from tests.hosts.accounting_fixtures import FixtureAccounting
from tests.hosts.fixture_cli import ACCOUNT


def files(root, config="", *, mode=0o755):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    home = root / "coherent-home"
    home.mkdir(mode=mode)
    for name, value in (("auth.json", "fixture-not-a-credential"), ("config.toml", config)):
        (home / name).write_text(value)
        (home / name).chmod(0o600)
    return home


class FixturePermits:
    def __init__(self):
        self.calls = []
        self.deny_conformance = False

    def authorize_auth(self, policy, binding):
        assert policy.executable.name == "fixture-cli"
        self.calls.append("status-authorized")
        if not hasattr(self, "accounting"):
            self.accounting = FixtureAccounting(
                policy.cwd / "fixture-accounting" / ("ledger-" + uuid4().hex)
            )
        return self.accounting.authorize_auth(policy, binding)

    def record_auth(self, binding, completion):
        self.calls.append(completion)
        self.accounting.record_auth(binding, completion)

    def require_native_conformance(self, policy, binding):
        from ai_task_manager.domains.work.errors import AccessDenied

        if self.deny_conformance:
            raise AccessDenied("Authored fixture conformance refusal, not real host evidence")
        return "authored-fixture-not-native-conformance"


def policy(root, *, program=None, config=""):
    home = files(root, config)
    (root / "fixture-accounting").mkdir(mode=0o700)
    fixture_home = root / "fixture-home"
    fixture_home.mkdir(mode=0o700)
    executable = root / "fixture-cli"
    body = (
        program
        if program is not None
        else (
            "import sys\nassert sys.argv[-2:]==['login','status']\n"
            "assert not any('forced_login_method' in x for x in sys.argv)\n"
            "sys.stderr.write('Logged in using ChatGPT\\n')\n"
        )
    )
    executable.write_text(f"#!{sys.executable}\n" + body)
    executable.chmod(0o700)
    settings = CodexSettings(CodexHomeBoundary(home), "synthetic-no-model", "high")
    env = clean_environment(fixture_home, codex_home=home)
    sha = hashlib.sha256(executable.read_bytes()).hexdigest()
    fingerprint = policy_digest(
        "codex",
        sha,
        env,
        root,
        {"codex_settings": settings.fingerprint},
        model=settings.model,
        effort=settings.effort,
    )
    return NativePolicy(
        "codex",
        executable,
        sha,
        env,
        root,
        fingerprint,
        (),
        model=settings.model,
        effort=settings.effort,
        codex_settings=settings,
    )


def effective(p):
    data = {"mcp_servers": {name: {"enabled": False} for name in p.codex_settings.check()}}
    for key, value in {
        **RESTRICTIONS,
        "forced_login_method": "chatgpt",
        "model": p.model,
        "model_reasoning_effort": p.effort,
    }.items():
        cursor = data
        parts = key.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    return {
        "config": data,
        "origins": {},
        "layers": [
            {
                "name": {
                    "type": "user",
                    "file": str(p.codex_settings.sources.home / "config.toml"),
                },
                "version": "authored-fixture-version",
                "config": data,
            }
        ],
    }


def observer(native, binding, journal, root):
    p = policy(root)
    process, transcript, emit = native()
    owned_binding = replace(binding, host="codex", configuration_digest=p.configuration_digest)
    transcript.binding = owned_binding
    permits = FixturePermits()
    cfg = CodexConfigurationObserver(p.codex_settings, journal, owned_binding, p, permits)
    auth = journal._append("trace", {"source": "authored fixture only"})
    startup = StartupObserver(
        transcript, p, expected_account=digest(ACCOUNT), configuration=cfg, codex_auth_ref=auth
    )
    counter = 0

    def reply(method, result, params=None):
        nonlocal counter
        counter += 1
        startup.expect(counter, method, params)
        emit({"id": counter, "result": result})

    def initial():
        reply("initialize", {"codexHome": p.environment["CODEX_HOME"]})
        reply("account/read", {"account": ACCOUNT})
        reply("config/read", effective(p), {"includeLayers": True, "cwd": str(p.cwd)})
        reply("configRequirements/read", {})  # Caller explicitly replaces unknown requirements.

    def ready_config():
        reply("initialize", {"codexHome": p.environment["CODEX_HOME"]})
        reply("account/read", {"account": ACCOUNT})
        reply("config/read", effective(p), {"includeLayers": True, "cwd": str(p.cwd)})
        reply("configRequirements/read", {"requirements": None})
        cfg.pre_thread()

    def thread():
        reply(
            "thread/start",
            {
                "thread": {"id": "configured-fixture-thread"},
                "model": p.model,
                "reasoningEffort": p.effort,
                "modelProvider": "openai",
                "approvalPolicy": "never",
                "approvalsReviewer": "user",
                "sandbox": {"type": "readOnly", "networkAccess": False},
                "cwd": str(p.cwd),
                "instructionSources": [],
            },
        )

    return p, process, transcript, emit, startup, cfg, permits, reply, ready_config, thread


def status(p, binding, journal, directory):
    permits = FixturePermits()
    probe = CodexAuthProbe(journal, permits)
    return probe, permits, probe.observe(p, binding, directory)
