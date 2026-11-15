"""Trusted setup API lineage with actual installed CLI and authored provider HTTP."""

import json
import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from ai_task_manager.adapters.decisions.journal import AuthorityJournal
from ai_task_manager.adapters.decisions.observations import bind_record
from ai_task_manager.config.files import digest, read_private, write_private
from tests.linear.live.ledger import DriverRefused

from .test_dp01_preparation import installed, snapshot  # noqa: F401, F811 -- shared installed fixture


def submitted(p):
    writing = p.approved()
    task = p.prepare(
        "task_submit", {"writing_ref": writing["approval_ref"], "operation_key": "api-submit"}
    )["data"]
    return writing, task, p.commands[-1]["receipt_path"]


def observe(p, writing, task, receipt):
    return p.admin.observe_product_issue(
        p.config, "api-submit", task["id"], writing["approval_ref"], api_submit_observation=receipt
    )


@pytest.mark.parametrize("installed", ["api"], indirect=True)
def test_API01_actual_default_setup_and_installed_submit_are_readonly_observed(installed):  # noqa: F811
    p = installed
    writing, task, receipt = submitted(p)
    before = snapshot(p.config.parent)
    # API provenance must be explicitly selected; never synthesize operator records.
    with pytest.raises(DriverRefused):
        p.admin.observe_product_issue(p.config, "api-submit", task["id"], writing["approval_ref"])
    owned = observe(p, writing, task, receipt)
    assert owned["id"] == task["issue_id"]
    assert observe(p, writing, task, receipt) == owned
    assert snapshot(p.config.parent) == before
    assert not (p.config.parent / "operator-intent.json").exists()
    assert not (p.config.parent / "operator-result.json").exists()
    proof = p.ledger.summary()["product_observations"][0]
    assert proof["origin"] == "confirmed_product_post_effect"
    provenance = json.loads(proof["provenance"])
    assert provenance["installation"]["mode"] == "setup_api"
    assert provenance["installation"]["provider_mode"] == "authored_loopback_control"
    assert not [row for row in p.ledger.summary()["operations"] if row["kind"] == "issue"]
    assert len(p.ledger.summary()["requests"]) == len(p.provider.requests)


@pytest.mark.parametrize("installed", ["api"], indirect=True)
@pytest.mark.parametrize(
    "change",
    [
        "foreign_command",
        "missing_setup_result",
        "revoked",
        "authority_mac",
        "source",
        "capability_scope",
        "prose",
        "claim_store",
        "during_readback",
    ],
)
def test_API02_missing_foreign_revoked_and_changed_lineage_refuses(installed, change):  # noqa: F811
    p = installed
    writing, task, receipt = submitted(p)
    config = read_private(p.config)
    observed = read_private(receipt)
    if change == "foreign_command":
        observed["argv"][2] = str(p.folder / "foreign-service.json")
        write_private(receipt, observed, replace=True)
    elif change == "missing_setup_result":
        with sqlite3.connect(config["authority_database"]) as conn:
            conn.execute(
                "DELETE FROM records WHERE ref=?",
                ("session-setup-result:" + digest(config["grant_ref"]),),
            )
    elif change == "revoked":
        # The supported revocation producer's exact authenticated journal record.
        journal = AuthorityJournal(config["authority_database"], config["authority_key"])
        bind_record(
            journal,
            "session-revoked:" + digest(config["grant_ref"]),
            {"enrollment": config["grant_ref"]},
        )
    elif change == "authority_mac":
        with sqlite3.connect(config["authority_database"]) as conn:
            conn.execute("UPDATE records SET mac=? WHERE ref=?", ("0" * 64, config["grant_ref"]))
    elif change == "source":
        observed["source_digest"] = "0" * 64
        write_private(receipt, observed, replace=True)
    elif change == "capability_scope":
        capability = read_private(config["session_file"])
        capability["scope"]["principal"] = str(uuid4())
        write_private(config["session_file"], capability, replace=True)
    elif change == "prose":
        # Keep the actual publication SQL guard active; alter the approved output instead.
        Path(writing["handoff"]["destination"]).write_bytes(b"AUTHORED_CHANGED_APPROVED_OUTPUT")
    elif change == "claim_store":
        source = Path(config["core_database"])
        replacement = source.with_name("foreign-copy.sqlite")
        shutil.copyfile(source, replacement)
        replacement.chmod(0o600)
        replacement.replace(source)
    else:

        def transform(query, variables, result):
            if "query FixtureIssueMarker" in query:
                observed["entry_sha256"] = "0" * 64
                write_private(receipt, observed, replace=True)
            return result

        p.provider.transform = transform
    before = snapshot(p.config.parent)
    creates = len(p.provider.issues)
    with pytest.raises(DriverRefused):
        observe(p, writing, task, receipt)
    assert snapshot(p.config.parent) == before
    assert len(p.provider.issues) == creates == 1
    assert "product_observations" not in p.ledger.summary()


@pytest.mark.parametrize("installed", ["api_workflow"], indirect=True)
def test_API03_workflow_prepare_origin_is_readonly_observed(installed):  # noqa: F811
    p = installed
    writing, task, receipt = submitted(p)
    command = read_private(receipt)["argv"]
    assert command[3:-1] == ["workflow", "prepare", "--input"]
    assert read_private(command[-1]) == {
        "operation": "task_submit",
        "data": {"writing_ref": writing["approval_ref"], "operation_key": "api-submit"},
    }
    before = snapshot(p.config.parent)
    owned = observe(p, writing, task, receipt)
    assert owned["id"] == task["issue_id"] and observe(p, writing, task, receipt) == owned
    assert snapshot(p.config.parent) == before
    assert not (p.config.parent / "operator-intent.json").exists()
    assert not (p.config.parent / "operator-result.json").exists()
    assert len(p.provider.issues) == 1


@pytest.mark.parametrize("installed", ["api_workflow"], indirect=True)
@pytest.mark.parametrize("change", ["wrong_operation", "input_tamper"])
def test_API04_wrong_workflow_operation_or_tampered_input_refuses(installed, change):  # noqa: F811
    p = installed
    writing, task, receipt = submitted(p)
    request = Path(read_private(receipt)["argv"][-1])
    value = read_private(request)
    if change == "wrong_operation":
        value["operation"] = "task_action"
    else:
        value["data"]["operation_key"] = "another-original-key"
    write_private(request, value, replace=True)
    before = snapshot(p.config.parent)
    with pytest.raises(DriverRefused):
        observe(p, writing, task, receipt)
    assert snapshot(p.config.parent) == before and len(p.provider.issues) == 1
    assert "product_observations" not in p.ledger.summary()
