"""Installed source origin and cold failure ordering, without external operations."""

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.native_installation.helpers import bundle


def test_NI06_installed_cold_manifest_refuses_before_provider_or_native(tmp_path):
    package = Path(os.environ["WORKFLOW_INSTALL_PACKAGE"])
    b = bundle(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys;sys.path.insert(0,sys.argv.pop(1));"
            "from ai_task_manager.installation.native import resolver;"
            "print(resolver.__file__,file=sys.stderr);"
            "from ai_task_manager.presentation.cli.main import main;raise SystemExit(main())",
            str(package),
            "--config",
            str(b.config),
            "--installation",
            str(b.installation),
            "--json",
            "workflow",
            "owner",
        ],
        capture_output=True,
        text=True,
        timeout=8,
        env={"PATH": os.defpath},
    )
    assert str(package) in result.stderr
    response = json.loads(result.stdout or result.stderr.splitlines()[-1])
    assert result.returncode == 4 and "not implemented" in response["error"]["message"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "codex-profile",
        "config.json",
        "home",
        "installation.json",
        "native.json",
    ]
