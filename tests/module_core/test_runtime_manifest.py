"""Isolated manifest controls; no product scenario or external service execution."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import ai_task_manager

BOOTSTRAP = """
import runpy, subprocess, sys, types
# This copied tool fixture has no Git repository. Only its recorded HEAD is authored.
original = subprocess.check_output
def recorded_head(command, *args, **kwargs):
    if command == ['git', 'rev-parse', 'HEAD']:
        return 'authored-manifest-fixture\\n'
    return original(command, *args, **kwargs)
subprocess.check_output = recorded_head
module = types.ModuleType('tests.module_core.scenario')
def run(root, database, writer):
    (root / 'scenario-entered').write_text('authored control only')
    return {'status': 'authored boundary control; product scenario not executed'}
module.run = run
sys.modules[module.__name__] = module
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""


@pytest.mark.parametrize(
    "case", ["metadata_only", "changed_code", "missing_code", "missing_asset", "similar_name"]
)
def test_only_ds_store_metadata_is_excluded_before_scenario(tmp_path, case):
    repository = Path(__file__).resolve().parents[2]
    fixture = tmp_path / "checkout"
    source = fixture / "src/ai_task_manager"
    (source / "domains").mkdir(parents=True)
    (fixture / "tools").mkdir()
    (fixture / "tests/module_core").mkdir(parents=True)
    tool = fixture / "tools/module_core_demo.py"
    shutil.copyfile(repository / "tools/module_core_demo.py", tool)
    for name in ("baseline-v1.json", "expectations-v3.json"):
        shutil.copyfile(
            repository / "tests/module_core" / name, fixture / "tests/module_core" / name
        )
    # Compare a real installed runtime file without modifying that installation.
    installed = Path(ai_task_manager.__file__).resolve().parent
    shutil.copyfile(installed / "__init__.py", source / "__init__.py")
    metadata = [source / ".DS_Store", source / "domains/.DS_Store"]
    for path in metadata:
        path.write_bytes(b"authored Finder metadata")
    unexpected = {
        "missing_code": "missing_runtime.py",
        "missing_asset": "required-runtime.json",
        "similar_name": ".DS_Store.json",
    }
    if case == "changed_code":
        with (source / "__init__.py").open("a") as stream:
            stream.write("\n# authored changed runtime\n")
    elif case in unexpected:
        assert not (installed / unexpected[case]).exists()
        (source / unexpected[case]).write_text("authored required runtime file\n")
    writer = tmp_path / "writer"
    (writer / "references").mkdir(parents=True)
    for name in ("SKILL.md", "references/writing_style.md", "references/issue.md"):
        (writer / name).write_text("Authored boundary fixture; no writing operations.\n")
    output = tmp_path / "evidence"
    command = [
        sys.executable,
        "-I",
        "-c",
        BOOTSTRAP,
        str(tool),
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
    snapshot = json.loads((output / "source.json").read_text())
    paths = {row["path"] for row in snapshot["runtime_files"]}
    assert ".DS_Store" not in paths and "domains/.DS_Store" not in paths
    assert all(path.read_bytes() == b"authored Finder metadata" for path in metadata)
    mismatches = [row for row in snapshot["runtime_files"] if row["source"] != row["installed"]]
    if case == "metadata_only":
        assert completed.returncode == 0, completed.stderr
        assert not mismatches
        assert (output / "project/scenario-entered").is_file()
        result = json.loads((output / "result.json").read_text())
        assert result["status"] == "local_demonstration_passed_connections_pending"
        assert result["database"]["same_physical_file"] is True
    else:
        assert completed.returncode != 0
        assert completed.stdout == ""
        assert (
            "Installed runtime differs from source; no demonstration admitted" in completed.stderr
        )
        assert not (output / "project").exists()
        assert not (output / "core.sqlite").exists()
        assert len(mismatches) == 1
        assert mismatches[0]["path"] == (
            "__init__.py" if case == "changed_code" else unexpected[case]
        )
