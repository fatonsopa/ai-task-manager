import json

import pytest

from ai_task_manager.adapters.hosts.bridge import container_inspection


@pytest.mark.parametrize(
    "message",
    [
        "Error: No such object: id-owned",
        "error: no such object: id-owned",
        "Error response from daemon: No such container: id-owned",
    ],
)
def test_exact_removal_receipt_accepts_observed_cli_error_case_variants(monkeypatch, message):
    calls = []
    plan = {"name": "owned", "owner": "nonce"}
    observed = {"Id": "id-owned", "Name": "/owned", "Config": {"Labels": {"aitm.owner": "nonce"}}}

    def docker(config, *arguments):
        calls.append(arguments)
        if arguments == ("inspect", "owned"):
            return {"returncode": 0, "stdout": json.dumps([observed]), "stderr": ""}
        if arguments == ("rm", "--force", "--volumes", "id-owned"):
            return {"returncode": 0, "stdout": "id-owned\n", "stderr": ""}
        return {"returncode": 1, "stdout": "[]\n", "stderr": message}

    monkeypatch.setattr(container_inspection, "docker", docker)
    assert container_inspection.cleanup(plan)["confirmed_stop"]
    assert calls[-1] == ("inspect", "id-owned")


def test_foreign_container_and_unknown_inspection_are_never_removed(monkeypatch):
    calls = []

    def docker(config, *arguments):
        calls.append(arguments)
        return {
            "returncode": 0,
            "stdout": json.dumps(
                [
                    {
                        "Id": "foreign",
                        "Name": "/owned",
                        "Config": {"Labels": {"aitm.owner": "someone-else"}},
                    }
                ]
            ),
            "stderr": "",
        }

    monkeypatch.setattr(container_inspection, "docker", docker)
    assert not container_inspection.cleanup({"name": "owned", "owner": "nonce"})["confirmed_stop"]
    assert calls == [("inspect", "owned")]
    monkeypatch.setattr(
        container_inspection,
        "docker",
        lambda *_: {"returncode": 1, "stdout": "", "stderr": "daemon unavailable"},
    )
    assert not container_inspection.cleanup({"name": "owned", "owner": "nonce"})["confirmed_stop"]
