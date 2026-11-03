"""Run the bounded M1 installed-interface demonstration; never an offline product mode."""

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="New owned evidence directory")
    parser.add_argument("--writer-root", required=True, type=Path)
    parser.add_argument(
        "--database", type=Path, help="Optional already-owned empty disposable file"
    )
    parser.add_argument(
        "--without-baseline", action="store_true", help="Run only the CLI/MCP scenario"
    )
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    output = args.output.absolute()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    import ai_task_manager

    installed = Path(ai_task_manager.__file__).resolve().parent
    source = repository / "src/ai_task_manager"
    if installed == source:
        raise RuntimeError("Use a clean installed interpreter, not the source checkout")
    files = []
    for path in sorted(source.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.name != ".DS_Store":
            relative = path.relative_to(source)
            target = installed / relative
            files.append(
                {
                    "path": str(relative),
                    "source": sha(path),
                    "installed": sha(target) if target.is_file() else None,
                }
            )
    baseline = repository / "tests/module_core/baseline-v1.json"
    expectations = repository / "tests/module_core/expectations-v3.json"
    selected = json.loads(baseline.read_text())
    metadata = {
        "head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repository, text=True
        ).strip(),
        "runtime_files": files,
        "runtime_manifest_sha256": hashlib.sha256(
            json.dumps(files, sort_keys=True).encode()
        ).hexdigest(),
        "installed": str(installed),
        "version": importlib.metadata.version("ai-task-manager"),
        "python": sys.version,
        "executable": sys.executable,
        "tool_sha256": sha(Path(__file__)),
        "baseline_sha256": sha(baseline),
        "expectations_sha256": sha(expectations),
        "arguments": sys.argv,
        "test_sources": {
            str(p.relative_to(repository)): sha(p)
            for p in sorted((repository / "tests").rglob("*.py"))
        },
        "writer_sources": {
            name: sha(args.writer_root / name)
            for name in ("SKILL.md", "references/writing_style.md", "references/issue.md")
        },
    }
    save(output / "source.json", metadata)
    save(output / "expectations.json", json.loads(expectations.read_text()))
    save(output / "baseline.json", selected)
    if not files or any(row["source"] != row["installed"] for row in files):
        raise RuntimeError("Installed runtime differs from source; no demonstration admitted")
    database = args.database.absolute() if args.database else output / "core.sqlite"
    if args.database is None:
        with database.open("xb"):
            database.chmod(0o600)
    info = database.stat()
    if database.is_symlink() or info.st_size or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("Disposable database must be an owned empty private regular file")
    project = output / "project"
    project.mkdir(mode=0o700)
    # Only test fixtures are loaded from this checkout; the product was imported
    # above from the verified installation and retains that package search path.
    sys.path.append(str(repository))
    started = time.monotonic()
    from tests.module_core.scenario import run

    result = {"status": "running", "module_accepted": False, "connections_accepted": False}
    save(output / "result.json", result)
    try:
        result["demonstration"] = run(project, database, args.writer_root)
        if not args.without_baseline:
            import pytest

            cases = list(
                dict.fromkeys(
                    case for row in selected["responsibilities"] for case in row["existing_cases"]
                )
            )
            command = [
                "-q",
                "-o",
                "pythonpath=",
                "--maxfail=1",
                "--basetemp=" + str(output / "baseline-fixtures"),
                "--junitxml=" + str(output / "baseline.xml"),
                *(str(repository / case) for case in cases),
            ]
            result["baseline_command"] = command
            result["baseline_exit_code"] = pytest.main(command)
            if result["baseline_exit_code"]:
                raise RuntimeError("A required baseline case failed; retained XML is authoritative")
    except BaseException as error:
        result["status"] = "failed"
        result["failure"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        prior_failure = result["status"] == "failed"
        identity_error = None
        observation = result["database"] = {
            "path": str(database),
            "device": None,
            "inode": None,
            "original_device": info.st_dev,
            "original_inode": info.st_ino,
            "same_physical_file": None,
        }
        try:
            after = database.lstat()
            observation.update(
                device=after.st_dev,
                inode=after.st_ino,
                same_physical_file=(after.st_dev, after.st_ino) == (info.st_dev, info.st_ino),
            )
            if not observation["same_physical_file"]:
                identity_error = RuntimeError("Final database physical identity changed")
        except OSError as error:
            observation["observation_error"] = {"type": type(error).__name__, "message": str(error)}
            identity_error = RuntimeError("Final database physical identity could not be observed")
        if identity_error is not None:
            result["status"] = "failed"
            observation["failure"] = str(identity_error)
            result.setdefault(
                "failure", {"type": type(identity_error).__name__, "message": str(identity_error)}
            )
        elif not prior_failure:
            result["status"] = "local_demonstration_passed_connections_pending"
        result["elapsed_seconds"] = time.monotonic() - started
        save(output / "result.json", result)
        if identity_error is not None and not prior_failure:
            raise identity_error
    print(
        json.dumps({"status": result["status"], "evidence": str(output), "module_accepted": False})
    )


if __name__ == "__main__":
    main()
