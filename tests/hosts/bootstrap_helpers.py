"""Owned protocol, ledger and provider controls; never actual host/provider acceptance."""

import hashlib
import sys
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.hosts.bridge.admission import (
    LaunchTemplate,
    NativeSourcePins,
    NativeStartupObservations,
)
from ai_task_manager.adapters.hosts.bridge.auth_probe import claude_account_hash
from ai_task_manager.adapters.hosts.bridge.bootstrap import (
    BootstrapPermits,
    BootstrapReview,
    DiagnosticBootstrap,
)
from ai_task_manager.adapters.hosts.bridge.controller import ManagedNativeHost
from ai_task_manager.adapters.hosts.bridge.evaluation_enrollment import EvaluationGrant
from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from ai_task_manager.adapters.hosts.bridge.policy import (
    NativePolicy,
    clean_environment,
    policy_digest,
)
from ai_task_manager.adapters.hosts.bridge.records import HostBinding
from ai_task_manager.adapters.linear.models import ExpectedBinding, VerifiedBinding
from ai_task_manager.adapters.linear.receipts import ProviderJournal
from ai_task_manager.application.core import Core
from ai_task_manager.application.intake import prose_digest
from ai_task_manager.config.claim_store import enroll_claim_store, verify_claim_store
from ai_task_manager.config.evaluation_records import PURPOSES, EvaluationRecords
from ai_task_manager.domains.work.models import ApprovalPolicy, CapacityPolicy, Scope, TaskProse
from ai_task_manager.domains.work.ports import ExactRenderer
from ai_task_manager.storage.database import Database
from tests.hosts.accounting_fixtures import FixtureAccounting


def create(root, mode, *, seconds=45, cli_version="2.1.301", observed_version="2.1.301"):
    root.mkdir(mode=0o700)
    home, work = root / "home", root / "workspace"
    home.mkdir(mode=0o700)
    (home / ".claude_roni").mkdir(mode=0o700)
    work.mkdir(mode=0o700)
    source = Path(__file__).with_name("fixture_claude_bootstrap.py").read_text()
    source = source.replace(
        '"claude_code_version": "2.1.301",', f'"claude_code_version": {observed_version!r},'
    )
    executable = root / "authored-claude"
    executable.write_text(
        f"#!{sys.executable}\n" + source.replace('MODE = "normal"', f'MODE = "{mode}"')
    )
    executable.chmod(0o700)
    expected = ExpectedBinding(
        "authored-viewer", "authored-workspace", "authored-team", "TST", "authored-run-project"
    )
    scope = Scope(
        "linear", expected.workspace_id, expected.viewer_id, expected.project_id, "evaluation:case"
    )
    environment = clean_environment(home, claude_profile=home / ".claude_roni")
    sha = hashlib.sha256(executable.read_bytes()).hexdigest()
    tools = tuple(
        "mcp__aitm__" + n for n in ("read_file", "create_file", "write_file", "run_command", "acknowledge_event")
    )
    policy = NativePolicy(
        "claude",
        executable,
        sha,
        environment,
        work,
        policy_digest(
            "claude", sha, environment, work, {}, model="fixture-requested-model", effort="high"
        ),
        (),
        tools,
        "fixture-requested-model",
        "high",
        cli_version=cli_version,
    )
    binding = HostBinding(
        scope,
        "diagnostic:session",
        "diagnostic:attempt",
        "diagnostic-worker",
        "claude",
        "authored-principal-not-live",
        policy.configuration_digest,
        time.time() + seconds,
    )
    accounting = FixtureAccounting(root / "accounting", maximum_seconds=max(60, seconds))
    initialization = accounting.initialize(policy, binding)
    pins = NativeSourcePins({str(executable): sha})
    template = LaunchTemplate(policy, pins)
    review = accounting.rig.pin(
        "native_bootstrap_review",
        {
            "version": 1,
            "purpose": "isolated-native-startup-diagnostic",
            "binding": asdict(binding),
            "template": template.fingerprint,
            "batch_id": "batch",
            "expires_at": initialization.native.binding.deadline,
            "billing_basis_ref": "authored-billing-control",
            "case_id": "case",
        },
    )
    records = EvaluationRecords(
        {k: v for k, v in accounting.rig.bindings.items() if v["kind"] in PURPOSES}
    )
    authorization = BootstrapReview(records, review, initialization, template)
    journal = HostJournal(root / "host" / "journal.sqlite", root / "host" / "key")
    permits = BootstrapPermits(
        initialization,
        lambda: accounting.claim(policy, binding, "auth", maximum=15),
        lambda: accounting.claim(
            policy, binding, "neutral", parent=initialization.native.reference, maximum=20
        ),
        authorization,
        journal,
    )
    account = claude_account_hash(
        {"email": "fixture@example.invalid", "orgId": "authored-org", "userId": "authored-user"}
    )
    native = ManagedNativeHost(
        binding,
        SimpleNamespace(worktree=work),
        policy,
        root / "native",
        journal,
        None,
        None,
        permits,
        account,
    )
    observations = NativeStartupObservations(journal, template, account)
    bootstrap = DiagnosticBootstrap(native, observations)
    return SimpleNamespace(
        root=root,
        native=native,
        bootstrap=bootstrap,
        observations=observations,
        accounting=accounting,
        records=records,
        review=review,
        expected=expected,
        binding=binding,
        policy=policy,
        template=template,
        journal=journal,
    )


@pytest.fixture
def bootstrap_graph(tmp_path):
    owned = []

    def make(mode="normal", *, seconds=45, **versions):
        item = create(tmp_path / f"diagnostic-{len(owned)}", mode, seconds=seconds, **versions)
        owned.append(item)
        return item

    yield make
    for item in owned:
        if item.native.process is not None and not item.native.process._closed:
            stop = item.bootstrap.stop()
            assert stop.confirmed, "Owned fixture stop remained unknown"


def grant_graph(item):
    ready = item.bootstrap.start()
    core_path = item.root / "core.sqlite"
    database = Database(core_path)
    core_path.chmod(0o600)
    authority = AuthorityJournal(item.root / "authority.sqlite", item.root / "authority.key")
    enroll_claim_store(database, authority)
    claim = verify_claim_store(database, authority)
    provider = ProviderJournal(item.root / "provider.sqlite")
    core = Core(
        database,
        ApprovalPolicy("fixture-writer", "fixture-reference", "fixture-style"),
        (CapacityPolicy("claude", 1, 1),),
        principals=provider,
    )
    prose = TaskProse(
        "Verify an owned data report",
        "Summarize the owned fixture dataset and verify its output.",
        False,
        ("complete",),
    )
    observed = item.observations.verify(ready)
    body = {
        "version": 1,
        "purpose": "isolated-native-profile-evaluation",
        "case_id": "case",
        "bootstrap_ref": ready,
        "bootstrap_process": observed["payload"]["process"],
        "bootstrap_configuration_digest": item.binding.configuration_digest,
        "source_template": item.template.fingerprint,
        "scope": asdict(item.binding.scope),
        "team_id": item.expected.team_id,
        "team_key": "TST",
        "claim_store": claim.context,
        "task_prose_digest": prose_digest(prose, ExactRenderer().render(prose)),
        "actions": ["intake", "enqueue", "start", "non_code_complete"],
        "worker_batch_id": "batch",
        "expires_at": min(observed["expires_at"], time.time() + 25),
        "model": item.policy.model,
        "effort": item.policy.effort,
        "billing_basis_ref": "authored-billing-control",
    }
    ref = item.accounting.rig.pin("evaluation_grant", body)
    records = EvaluationRecords(
        {k: v for k, v in item.accounting.rig.bindings.items() if v["kind"] in PURPOSES}
    )
    grant = EvaluationGrant(
        records, ref, "policy-fixture", item.observations, core, claim, item.journal, item.expected
    )
    client = SimpleNamespace(verify_binding=lambda: VerifiedBinding(item.expected, {}))
    return SimpleNamespace(
        item=item,
        grant=grant,
        core=core,
        claim=claim,
        authority=authority,
        provider=provider,
        client=client,
        body=body,
        records=records,
        ref=ref,
    )
