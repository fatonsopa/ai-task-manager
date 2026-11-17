"""Installed native composition is measured as authored control, never live acceptance."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


@pytest.fixture
def installed(tmp_path, request):
    root = tmp_path.resolve()
    root.chmod(0o700)
    sockets = Path(tempfile.mkdtemp(prefix="aitm-install-")).resolve()
    package = Path(os.environ["WORKFLOW_INSTALL_PACKAGE"])
    repo = Path(__file__).parents[2]
    mode = getattr(request, "param", "valid")
    command = [
        sys.executable,
        "-I",
        "-c",
        "import sys;sys.path[:0]=[sys.argv.pop(1),sys.argv.pop(1),sys.argv.pop(1)];"
        "from tests.workflow_installation.helpers import owner_main;owner_main(*sys.argv[1:])",
        str(package),
        str(repo),
        str(Path(pytest.__file__).parent.parent),
        str(root),
        str(sockets),
        mode,
    ]
    out = (root / "stdout").open("wb")
    err = (root / "stderr").open("wb")
    child = subprocess.Popen(
        command,
        stdout=out,
        stderr=err,
        start_new_session=True,
        env={"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    )
    limit = time.monotonic() + 12
    while not (root / "ready.json").exists() and child.poll() is None and time.monotonic() < limit:
        time.sleep(0.03)
    if mode in {"valid", "code", "code-restart", "recovery"}:
        assert (root / "ready.json").exists(), (root / "stderr").read_text()
    else:
        child.wait(timeout=5)
    try:
        yield root, package, child
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=10)
        out.close()
        err.close()
        assert child.returncode == 0, (root / "stderr").read_text()
        shutil.rmtree(sockets)
        record = os.environ.get("WORKFLOW_INSTALL_REPORT")
        if record:
            with Path(record).open("a") as stream:
                stream.write(
                    json.dumps({"root": str(root), "pid": child.pid, "exit": child.returncode})
                    + "\n"
                )


def cli(fixture, group, action, data=None, extra=()):
    root, package, _ = fixture
    command = [
        sys.executable,
        "-I",
        "-c",
        "import sys;sys.path.insert(0,sys.argv.pop(1));"
        "from ai_task_manager.presentation.cli.main import main;raise SystemExit(main())",
        str(package),
        "--config",
        str(root / "service.json"),
        "--json",
        group,
        action,
        *extra,
    ]
    if data is not None:
        command += ["--input", "-"]
    return subprocess.run(
        command,
        input=json.dumps(data) if data is not None else None,
        capture_output=True,
        text=True,
        timeout=100 if (root / "code-policy.json").exists() else 25,
        env={"PATH": os.defpath},
    )


def test_WI01_actual_installed_launcher_managed_native_path(installed):
    root, package, _ = installed
    ready = json.loads((root / "prepared.json").read_text())
    assert ready["module_source"].startswith(str(package))
    request = {
        "task_id": ready["task_id"],
        "plan_ref": ready["plan_ref"],
        "operation_key": "installed-run",
    }
    first = cli(installed, "workflow", "run", request)
    payload = json.loads(first.stdout or first.stderr)
    assert first.returncode == 4 and payload["data"]["state"] == "awaiting_review", payload
    second = cli(installed, "workflow", "run", request)
    assert second.returncode == 4 and json.loads(second.stdout)["data"] == payload["data"]
    with sqlite3.connect(root / "installed-state/runtime.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM runtime_attempts").fetchone()[0] == 1
        facts = [
            json.loads(r[0])
            for r in db.execute(
                "SELECT data FROM runtime_observations WHERE kind='runtime_outcome'"
            )
        ]
        assert facts and facts[-1]["quiescent"]
    files = list((root / "installed-state/resources/trees").glob("*/owned.py"))
    assert len(files) == 1 and files[0].read_text() == "answer = 42\n"


@pytest.mark.parametrize(
    "installed",
    [
        "wrong-scope",
        "wrong-prose",
        "wrong-native",
        "wrong-source",
        "missing-provider",
        "self-review",
    ],
    indirect=True,
)
def test_WI02_WI03_bound_prerequisites_refuse_before_native(installed, request):
    root, _, _ = installed
    refused = json.loads((root / "refused.json").read_text())
    expected = {
        "wrong-scope": "Installed non-code task, scope or approved prose differs",
        "wrong-prose": "Installed non-code task, scope or approved prose differs",
        "wrong-native": "Native account/model/source registration differs",
        "wrong-source": "Installed repository source changed",
        "missing-provider": "Real provider/dependency ports required before native execution",
        "self-review": "Independent genuine review capture admission is not installed",
    }
    assert refused["detail"] == expected[request.node.callspec.params["installed"]], refused
    with sqlite3.connect(root / "core.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_WI02_cold_registry_refuses_before_provider(installed):
    root, _, _ = installed
    result = cli(
        installed, "workflow", "owner", extra=["--installation", str(root / "installation.json")]
    )
    assert result.returncode == 4, json.loads(result.stdout or result.stderr)


def test_WI03_copied_installation_and_replaced_file_refuse(installed):
    from ai_task_manager.domains.work.errors import AccessDenied
    from ai_task_manager.installation.workflows.spec import Installation

    root, _, _ = installed
    installation = root / "installation.json"
    copy = root / "copied.json"
    shutil.copyfile(installation, copy)
    copy.chmod(0o600)
    with pytest.raises(AccessDenied, match="Copied installation"):
        Installation(copy, root / "service.json")
    original = Installation(installation, root / "service.json")
    os.replace(copy, installation)
    with pytest.raises(AccessDenied, match="identity changed"):
        original.check()


def test_WI04_unknown_fields_and_bool_format_refuse(installed):
    from ai_task_manager.config.files import write_private
    from ai_task_manager.domains.work.errors import ValidationError
    from ai_task_manager.installation.workflows.spec import Installation

    root, _, _ = installed
    source = json.loads((root / "installation.json").read_text())
    for name, value in [("factory", "module:callable"), ("reviewed", True), ("version", True)]:
        path = root / ("invalid-" + name + ".json")
        data = {**source, name: value, "installation_path": str(path)}
        write_private(path, data)
        with pytest.raises(ValidationError):
            Installation(path, root / "service.json")


def test_WI04_production_has_no_fixture_or_private_evaluator_import():
    import ast

    from ai_task_manager import installation

    source = Path(installation.__file__).parent
    for path in source.rglob("*.py"):
        tree = ast.parse(path.read_text())
        imports = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        imports += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        assert not any(
            n.startswith(("tests", "tools", "build_support")) or "evaluation_" in n for n in imports
        )
