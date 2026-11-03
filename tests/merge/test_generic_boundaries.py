"""Layer and actual wheel boundaries for the standalone publication component."""

import ast
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src/ai_task_manager"
LAYERS = {
    "domains": "domains/merging",
    "application": "application/merging",
    "storage": "storage/merging",
    "transport": "adapters/git",
}


def test_old_namespace_is_removed_and_layer_dependencies_have_one_owner():
    assert not (SOURCE / "adapters/projects/ai_index").exists()
    forbidden = {
        "domains": [
            "ai_task_manager.adapters",
            "ai_task_manager.application",
            "ai_task_manager.storage",
        ],
        "application": ["ai_task_manager.adapters", "ai_task_manager.storage"],
        "storage": ["ai_task_manager.adapters", "ai_task_manager.application"],
        "transport": [
            "ai_task_manager.application",
            "ai_task_manager.storage",
            "ai_task_manager.adapters.linear",
            "ai_task_manager.adapters.hosts",
        ],
    }
    for layer, directory in LAYERS.items():
        modules = list((SOURCE / directory).glob("*.py"))
        assert any(p.name != "__init__.py" for p in modules)
        for path in modules:
            text = path.read_text()
            assert "refs/heads/dev" not in text and "ai_index" not in text
            assert "merge-to-dev.sh" not in text
            assert not any(
                word in text for word in ["UPDATE tasks", "INSERT INTO tasks", "set_status"]
            )
            for node in ast.walk(ast.parse(text)):
                names = (
                    [n.name for n in node.names]
                    if isinstance(node, ast.Import)
                    else ([node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                )
                assert not any(
                    name.startswith(prefix) for name in names for prefix in forbidden[layer]
                )


def test_actual_wheel_excludes_retired_runtime_and_imports_without_checkout(tmp_path):
    owned = tmp_path / "wheel"
    owned.mkdir(mode=0o700)
    home = owned / "home"
    home.mkdir(mode=0o700)
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home),
        "TMPDIR": str(owned),
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
        "LC_ALL": "C",
        "UV_CACHE_DIR": os.environ["AITM_UV_CACHE_DIR"],
        "UV_PYTHON": sys.executable,
        "UV_PYTHON_DOWNLOADS": "never",
    }
    argv = [
        os.environ["AITM_UV_BINARY"],
        "build",
        "--offline",
        "--wheel",
        "--out-dir",
        str(owned / "dist"),
    ]
    built = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, timeout=60)
    (owned / "build.stdout").write_bytes(built.stdout)
    (owned / "build.stderr").write_bytes(built.stderr)
    assert built.returncode == 0, built.stderr.decode(errors="replace")
    wheels = list((owned / "dist").glob("*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names))
        assert not any("/ai_index/" in n or "merge-to-dev" in n for n in names)
        for d in LAYERS.values():
            assert any(n.startswith("ai_task_manager/" + d + "/") for n in names)
    modules = [
        "ai_task_manager.domains.merging.contracts",
        "ai_task_manager.storage.merging.journal",
        "ai_task_manager.application.merging.service",
        "ai_task_manager.adapters.git.profile",
        "ai_task_manager.adapters.git.publication",
    ]
    code = (
        "import importlib,importlib.util,json,sys;sys.path.insert(0,sys.argv[1]);"
        "names=json.loads(sys.argv[2]);mods=[importlib.import_module(n) for n in names];"
        "assert all(m.__file__.startswith(sys.argv[1]) for m in mods);"
        "assert importlib.util.find_spec('ai_task_manager.adapters.projects.ai_index') is None;"
        "print(json.dumps({m.__name__:m.__file__ for m in mods}))"
    )
    imported = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code, str(wheel), json.dumps(modules)],
        cwd=owned,
        env=env,
        capture_output=True,
        timeout=15,
    )
    (owned / "import.stdout").write_bytes(imported.stdout)
    (owned / "import.stderr").write_bytes(imported.stderr)
    assert imported.returncode == 0, imported.stderr.decode(errors="replace")
    (owned / "package-evidence.json").write_text(
        json.dumps(
            {
                "wheel": str(wheel),
                "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "build_argv": argv,
                "imported": json.loads(imported.stdout),
                "scope": (
                    "Actual built wheel, isolated zip imports; "
                    "no normal installation or host acceptance"
                ),
            },
            indent=2,
        )
    )
