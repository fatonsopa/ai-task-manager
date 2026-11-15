"""Installed Writer/CLI decision flow with authored HTTP and operator-input controls."""

import os
import subprocess

import pytest

from ai_task_manager.adapters.hosts.bridge.journal import HostJournal
from tests.application.helpers import reviewed_control
from tests.existing.test_installed_protocol import installed as installed_candidate  # noqa: F401
from tests.linear.test_decision_publication import DecisionProvider, fields

from . import test_dp01_preparation as shared
from .test_dp01_preparation import installed  # noqa: F401

pytestmark = pytest.mark.parametrize("installed", ["api"], indirect=True)


@pytest.fixture(autouse=True)
def current_candidate(monkeypatch, installed_candidate):  # noqa: F811
    python, _, _, _ = installed_candidate
    monkeypatch.setenv("DP01_INSTALLED_PYTHON", str(python))
    monkeypatch.setattr(shared, "AdapterProvider", DecisionProvider)


def test_installed_decision_request_resolve_and_routine_progress(installed):  # noqa: F811
    p = installed
    writing = p.approved()
    task = p.prepare(
        "task_submit", {"writing_ref": writing["approval_ref"], "operation_key": "decision-task"}
    )["data"]
    sentinel = "00000000-0000-4000-8000-000000000700"
    p.provider.issues[task["issue_id"]]["decision_labels"] = [sentinel]

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
    two, _ = publish("two")
    journal = HostJournal(p.folder / "existing-host.sqlite", p.folder / "existing-host.key")
    python = p.manifest["writer"]["python"]

    def choose(row, key):
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
            "choice',decision_request_id=sys.argv[8],decision_option_id='A'))"
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
            ],
            capture_output=True,
            text=True,
            env={
                **{k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
                "AITM_CREDENTIAL_DP01_CONTROL": "AUTHORED_SECRET_MUST_NOT_APPEAR_IN_OBSERVER",
            },
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        data = {"request_id": row["id"], "human_ref": result.stdout.strip(), "operation_key": key}
        resolved = p.prepare("task_decision_resolve", data)["data"]
        assert resolved["decision_state"] == "resolved"
        assert p.prepare("task_decision_resolve", data)["data"]["decision_state"] == "resolved"
        return data

    choose(one, "resolve-one")
    assert len(p.provider.issues[task["issue_id"]]["decision_labels"]) == 2
    choose(two, "resolve-two")
    assert p.provider.issues[task["issue_id"]]["decision_labels"] == [sentinel]
    assert p.prepare("task_decision_request", request_one)["data"]["decision_state"] == "resolved"
    assert len(p.provider.comments) == 2
    prepared = p.prepare(
        "writing_prepare",
        {
            "task_id": task["id"],
            "purpose": "progress",
            "kind": "issue_chore",
            "title": "Task progress update",
            "body": "Both authored choices have retained resolution evidence.",
            "code_bearing": False,
            "required_work": ["progress"],
        },
    )["data"]
    p.prepare(
        "writing_review",
        {"writing_id": prepared["writing_id"], "review": reviewed_control(prepared["packet"])},
    )
    approved = p.prepare("writing_finalize", {"writing_id": prepared["writing_id"]})["data"]
    routine = p.prepare(
        "task_progress",
        {
            "task_id": task["id"],
            "writing_ref": approved["approval_ref"],
            "operation_key": "routine",
        },
    )["data"]
    assert routine["state"] == "confirmed" and routine["decision_state"] is None
    assert p.provider.issues[task["issue_id"]]["decision_labels"] == [sentinel]
    assert len(p.provider.comments) == 3
