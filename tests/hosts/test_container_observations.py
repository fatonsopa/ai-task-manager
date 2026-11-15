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


def test_owned_watcher_boundary_denial_retains_original_sanitized_cause(tmp_path, journal):
    """Actual local watcher/process cleanup; authored transport, not Docker acceptance."""
    import subprocess
    import sys

    from ai_task_manager.adapters.hosts.bridge.containers import DockerCommands
    from ai_task_manager.adapters.process.groups import OwnedProcessGroup
    from ai_task_manager.domains.work.errors import AccessDenied
    from ai_task_manager.supervisor.models import RuntimeUncertain

    owner = "authored-observation-owner"
    output = tmp_path / "stdout"
    cleanup = tmp_path / "cleanup.json"
    program = (
        "import json,os,sys; os.umask(0o077); sys.stdin.readline(); "
        "json.dump({'watcher_pid':os.getpid(),'owner':sys.argv[2],"
        "'container':{'confirmed_stop':True}},open(sys.argv[1],'w'))"
    )
    with output.open("wb") as stream:
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", program, str(cleanup), owner],
            stdin=subprocess.PIPE,
            stdout=stream,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    group = OwnedProcessGroup(process)
    runner = object.__new__(DockerCommands)
    runner.journal = journal
    job = {
        "process": process,
        "group": group,
        "directory": tmp_path,
        "record": {"owner": owner, "name": "authored-watcher"},
    }

    def deny():
        raise AccessDenied("SECRET original authority predicate and private path")

    try:
        with pytest.raises(RuntimeUncertain, match="terminal output"):
            runner._wait(job, deny, None, {"deadline": journal.clock() + 5, "owner": owner})
        observed = journal._history("trace")[-1]["payload"]
        assert observed["trigger"] == "AccessDenied" and observed["confirmed_stop"]
        failure = observed["failure"]
        assert failure["causes"][0]["code"] == "access_denied"
        assert failure["causes"][0]["type"] == "AccessDenied"
        frames = failure["causes"][0]["frames"]
        assert any(
            frame["function"] == "_wait" and len(frame["code_sha256"]) == 64 for frame in frames
        )
        assert "SECRET" not in json.dumps(failure)
        assert "private path" not in json.dumps(failure)
        assert process.wait(timeout=1) == 0 and job["stop"].confirmed
    finally:
        if process.poll() is None:
            group.stop()
            process.wait(timeout=2)
