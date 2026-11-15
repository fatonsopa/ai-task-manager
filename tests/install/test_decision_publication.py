"""Installed Writer/CLI decision flow with authored HTTP and operator-input controls."""

import json
import os
import subprocess
import time

import pytest

from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from tests.application.helpers import FEATURE_BODY, reviewed_control
from tests.existing.test_installed_protocol import installed as installed_candidate  # noqa: F401
from tests.linear.test_decision_publication import fields
from tests.session_observations.test_decision_recipient import (
    RECIPIENT,
    CrossHumanProvider,
    recipient_configuration,
)

from . import test_dp01_preparation as shared
from .test_dp01_preparation import installed  # noqa: F401

pytestmark = pytest.mark.parametrize("installed", ["api"], indirect=True)


@pytest.fixture(autouse=True)
def current_candidate(monkeypatch, installed_candidate):  # noqa: F811
    python, _, _, _ = installed_candidate
    monkeypatch.setenv("DP01_INSTALLED_PYTHON", str(python))
    monkeypatch.setattr(shared, "AdapterProvider", CrossHumanProvider)


@pytest.fixture(autouse=True)
def retained_journey(current_candidate, installed, installed_candidate):  # noqa: F811
    yield
    _, _, _, destination = installed_candidate
    (destination / "decision-journey-summary.json").write_text(
        json.dumps(
            {
                "scope": "Authored HTTP and console controls, no genuine human/provider acceptance",
                "ledger": installed.ledger.summary(),
                "commands": [
                    {
                        "code": row["code"],
                        "argv": row["command"],
                        "dispatches": len(row["receipt"]["dispatches"]) if row["receipt"] else None,
                    }
                    for row in installed.commands
                ],
                "comments": len(installed.provider.comments),
            },
            indent=2,
        )
        + "\n"
    )


def test_installed_creator_choice_preserves_task_owner(installed):  # noqa: F811
    p = installed
    writing = p.approved(body=FEATURE_BODY.removeprefix("Type: Feature\n\n"))
    task = p.prepare(
        "task_submit", {"writing_ref": writing["approval_ref"], "operation_key": "decision-task"}
    )["data"]
    sentinel = "00000000-0000-4000-8000-000000000700"
    type_labels = list(p.provider.issues[task["issue_id"]].get("decision_labels", []))
    assert len(type_labels) == 1
    p.provider.issues[task["issue_id"]]["decision_labels"] = type_labels + [sentinel]

    def publish(key):
        prepared = p.prepare(
            "task_decision_prepare",
            {"task_id": task["id"], **fields(), "operation_key": "prepare-" + key},
        )["data"]
        assert prepared["mechanical_passed"], prepared["packet"]["mechanical_checks"]
        p.prepare(
            "writing_review",
            {"writing_id": prepared["writing_id"], "review": reviewed_control(prepared["packet"])},
        )
        accepted = p.prepare("writing_finalize", {"writing_id": prepared["writing_id"]})["data"]
        assert accepted["purpose"] == "decision" and accepted["kind"] == "decision"
        request = {
            "task_id": task["id"],
            "writing_ref": accepted["approval_ref"],
            "operation_key": key,
        }
        first = p.prepare("task_decision_request", request)["data"]
        assert first["state"] == "confirmed" and first["label_state"] == "confirmed"
        assert p.prepare("task_decision_request", request)["data"]["id"] == first["id"]
        return first, request

    one, request_one = publish("one")
    journal = HostJournal(p.folder / "existing-host.sqlite", p.folder / "existing-host.key")
    python = p.manifest["writer"]["python"]
    recipient_config = recipient_configuration(p.config, p.folder / "recipient")
    setup_script = (
        "import sys,json; from pathlib import Path; "
        "from ai_task_manager.adapters.linear.transport import HttpTransport; "
        "from ai_task_manager.installation.sessions import setup; "
        "from ai_task_manager.config.service import load_service; "
        "HttpTransport.ENDPOINT=sys.argv[1]; "
        "result=setup(Path(sys.argv[2]),logical_session='decision-recipient',"
        "actions=('enqueue',),deadline=float(sys.argv[3]),operation_key='recipient-enrollment'); "
        "service=load_service(Path(sys.argv[2])); "
        "assert not service.call('task_show',{'task_id':sys.argv[4]})['ok']; "
        "assert not service.call('task_decision_show',{'request_id':sys.argv[5]})['ok']; "
        "print(json.dumps(result))"
    )
    setup_result = subprocess.run(
        [
            python,
            "-I",
            "-B",
            "-c",
            setup_script,
            p.provider.endpoint,
            str(recipient_config),
            str(time.time() + 600),
            task["id"],
            one["id"],
        ],
        capture_output=True,
        text=True,
        timeout=15,
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
    )
    assert setup_result.returncode == 0, setup_result.stderr

    def choose(row, key, *, recipient=recipient_config, denied=False):
        shown = p.prepare("task_show", {"task_id": task["id"]})["data"]
        script = (
            "import sys,json,os; from pathlib import Path; "
            "from ai_task_manager.adapters.linear.transport import HttpTransport; "
            "from ai_task_manager.adapters.hosts.bridge.human import OperatorChannel; "
            "from ai_task_manager.adapters.hosts.bridge.journal import HostJournal; "
            "from ai_task_manager.config.files import digest; "
            "from ai_task_manager.installation.sessions.human import capture_instruction; "
            "HttpTransport.ENDPOINT=sys.argv[1]; "
            "OperatorChannel._exchange=lambda self,request,timeout: "
            "({'request_id':request['request_id'],'challenge':request['challenge'],'accepted':True,'instruction_hash':digest(request['instruction'])},os.getpid());"
            " "
            "print(capture_instruction(Path(sys.argv[2]),HostJournal.open_existing(sys.argv[3],sys.argv[4]),"
            "task_id=sys.argv[5],target='Backlog',expected_version=int(sys.argv[6]),decision_revision=int(sys.argv[7]),"
            "reason='Authored operator option "
            "choice',decision_request_id=sys.argv[8],decision_option_id='A',"
            "recipient_config_file=Path(sys.argv[9])))"
        )
        result = subprocess.run(
            [
                python,
                "-I",
                "-B",
                "-c",
                script,
                p.provider.endpoint,
                str(p.config),
                str(journal.path),
                str(journal.key_path),
                task["id"],
                str(shown["version"]),
                str(shown["decision_revision"]),
                row["id"],
                str(recipient),
            ],
            capture_output=True,
            text=True,
            env={
                **{k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
                "AITM_CREDENTIAL_DP01_CONTROL": "AUTHORED_SECRET_MUST_NOT_APPEAR_IN_OBSERVER",
            },
            timeout=15,
        )
        if denied:
            assert result.returncode != 0 and "authenticated" in result.stderr, result.stderr
            return
        assert result.returncode == 0, result.stderr
        data = {"request_id": row["id"], "human_ref": result.stdout.strip(), "operation_key": key}
        resolved = p.prepare("task_decision_resolve", data)["data"]
        assert resolved["decision_state"] == "resolved"
        assert p.prepare("task_decision_resolve", data)["data"]["decision_state"] == "resolved"
        return data

    choose(one, "owner-cannot-impersonate", recipient=p.config, denied=True)
    choose(one, "resolve-one")
    assert p.provider.issues[task["issue_id"]]["decision_labels"] == sorted(
        type_labels + [sentinel]
    )
    assert p.prepare("task_decision_request", request_one)["data"]["decision_state"] == "resolved"
    assert len(p.provider.comments) == 1
    assert all(
        comment["body"].endswith("/profiles/" + RECIPIENT)
        for comment in p.provider.comments.values()
    )
    assert p.provider.issues[task["issue_id"]]["assignee"]["id"] != RECIPIENT
    assert (
        p.prepare("task_show", {"task_id": task["id"]})["data"]["session_id"] == task["session_id"]
    )


def test_installed_cold_original_key_recovers_known_comment(installed):  # noqa: F811
    p = installed
    writing = p.approved()
    task = p.prepare(
        "task_submit", {"writing_ref": writing["approval_ref"], "operation_key": "recovery-task"}
    )["data"]
    prepared = p.prepare(
        "task_decision_prepare",
        {"task_id": task["id"], **fields(), "operation_key": "prepare-recovery"},
    )["data"]
    assert prepared["mechanical_passed"], prepared["packet"]["mechanical_checks"]
    p.prepare(
        "writing_review",
        {"writing_id": prepared["writing_id"], "review": reviewed_control(prepared["packet"])},
    )
    accepted = p.prepare("writing_finalize", {"writing_id": prepared["writing_id"]})["data"]
    data = {
        "task_id": task["id"],
        "writing_ref": accepted["approval_ref"],
        "operation_key": "original-readback-recovery",
    }
    p.provider.transform = lambda query, variables, result: (
        {"comment": None} if "query ReadComment" in query else result
    )
    first = p.prepare("task_decision_request", data, expected=6)
    assert first["ok"] and first["outcome"] == "uncertain", first
    assert first["data"]["comment_id"] and len(p.provider.comments) == 1
    p.provider.transform = None
    # Every prepare call launches a fresh literal installed CLI process.
    recovered = p.prepare("task_decision_request", data)
    assert recovered["ok"] and recovered["outcome"] == "confirmed", recovered
    assert recovered["data"]["id"] == first["data"]["id"]
    assert recovered["data"]["comment_id"] == first["data"]["comment_id"]
    assert recovered["data"]["label_state"] == "confirmed"
    assert len(p.provider.comments) == 1
    assert sum("mutation CreateComment" in r["query"] for r, _ in p.provider.requests) == 1
