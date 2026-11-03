"""Authored final-boundary faults; no product scenario or provider execution."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

BOOTSTRAP = """
import runpy, sys, types
from pathlib import Path
fault = sys.argv.pop(1)
module = types.ModuleType('tests.module_core.scenario')
def run(root, database, writer):
    original = database.stat()
    (root / 'admitted.json').write_text(__import__('json').dumps({
        'device': original.st_dev, 'inode': original.st_ino,
    }))
    if fault in ('replace', 'replace_and_fail'):
        replacement = database.with_suffix('.replacement')
        replacement.write_bytes(b'authored replacement')
        replacement.chmod(0o600)
        replacement.replace(database)
    elif fault == 'missing':
        database.unlink()
    elif fault == 'unobservable':
        original_lstat = Path.lstat
        def denied(path, *args, **kwargs):
            if path == database:
                raise PermissionError('authored identity observation refusal')
            return original_lstat(path, *args, **kwargs)
        Path.lstat = denied
    elif fault == 'symlink':
        retained = database.with_suffix('.retained')
        database.rename(retained)
        database.symlink_to(retained)
    if fault in ('fail', 'replace_and_fail'):
        raise ValueError('authored scenario failure')
    return {'status': 'authored boundary control; product scenario not executed'}
module.run = run
sys.modules[module.__name__] = module
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""


@pytest.mark.parametrize(
    "fault",
    ["unchanged", "replace", "missing", "unobservable", "symlink", "fail", "replace_and_fail"],
)
def test_final_identity_controls_success_and_retains_failure(tmp_path, fault):
    # The child must use an installed interpreter matching this checkout, just as
    # the demonstration does. Only its scenario is replaced by the declared fault.
    repository = Path(__file__).resolve().parents[2]
    writer = tmp_path / "writer"
    (writer / "references").mkdir(parents=True)
    for relative in ("SKILL.md", "references/writing_style.md", "references/issue.md"):
        (writer / relative).write_text("Authored boundary fixture; no writing operations.\n")
    output = tmp_path / "evidence"
    command = [
        sys.executable,
        "-I",
        "-c",
        BOOTSTRAP,
        fault,
        str(repository / "tools/module_core_demo.py"),
        "--output",
        str(output),
        "--writer-root",
        str(writer),
        "--without-baseline",
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=20)
    (tmp_path / "command.json").write_text(
        json.dumps(
            {
                "command": command,
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            },
            indent=2,
        )
    )
    result = json.loads((output / "result.json").read_text())
    admitted = json.loads((output / "project/admitted.json").read_text())
    observed = result["database"]
    assert observed["original_device"] == admitted["device"]
    assert observed["original_inode"] == admitted["inode"]
    assert result["module_accepted"] is result["connections_accepted"] is False
    assert result["elapsed_seconds"] >= 0
    if fault == "unchanged":
        assert completed.returncode == 0, completed.stderr
        assert result["status"] == "local_demonstration_passed_connections_pending"
        assert observed["same_physical_file"] is True
        assert json.loads(completed.stdout)["status"] == result["status"]
        assert "failure" not in result
    else:
        assert completed.returncode != 0
        assert result["status"] == "failed"
        assert completed.stdout == ""
        if fault in ("fail", "replace_and_fail"):
            assert result["failure"] == {
                "type": "ValueError",
                "message": "authored scenario failure",
            }
        else:
            assert result["failure"]["type"] == "RuntimeError"
            assert "Final database physical identity" in result["failure"]["message"]
        if fault in ("missing", "unobservable"):
            assert observed["same_physical_file"] is None
            assert observed["inode"] is observed["device"] is None
            assert observed["observation_error"]["type"] == (
                "FileNotFoundError" if fault == "missing" else "PermissionError"
            )
        elif fault == "fail":
            assert observed["same_physical_file"] is True
        else:
            assert observed["same_physical_file"] is False
            assert observed["failure"] == "Final database physical identity changed"
