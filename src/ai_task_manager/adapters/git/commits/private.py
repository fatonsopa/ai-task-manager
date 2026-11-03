"""Private Git inputs; hook code never receives writable shared Git metadata."""

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

from ai_task_manager.adapters.resources.paths import identity
from ai_task_manager.config.files import write_private
from ai_task_manager.domains.work.errors import AccessDenied, EvidenceRequired


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def file_state(path):
    path = Path(path)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4194304:
        raise AccessDenied("Unsupported Git index identity or size")
    return {**identity(path), "sha256": sha(path), "mode": path.stat().st_mode & 0o777}


def environment(metadata, worktree, *, index=None):
    return {
        "PATH": os.defpath,
        "HOME": str(metadata),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_DIR": str(metadata),
        "GIT_WORK_TREE": str(worktree),
        "GIT_INDEX_FILE": str(index or metadata / "index"),
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
    }


def query(record, *args, index=None):
    """Only built-in read/plumbing calls, with executable helpers disabled on host."""
    value = subprocess.run(
        [record["git"], "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", *args],
        env=environment(Path(record["metadata"]), Path(record["worktree"]), index=index),
        capture_output=True,
        timeout=10,
    )
    if value.returncode or len(value.stdout) + len(value.stderr) > 4194304:
        raise EvidenceRequired("Private Git observation failed")
    return value.stdout


def hooks_state(directory):
    result = {}
    if directory.exists():
        if directory.is_symlink():
            raise AccessDenied("Git hook directory must be physical")
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise AccessDenied("Git hook symlinks require separate admission")
            if path.is_file():
                result[str(path.relative_to(directory))] = {
                    "sha256": sha(path),
                    "mode": path.stat().st_mode & 0o777,
                }
            elif not path.is_dir():
                raise AccessDenied("Unsupported Git hook source type")
    return result


def prepare(observer, attempt_id, path, directory, intent):
    path, directory = Path(path), Path(directory)
    if not re.fullmatch(r"[0-9a-f-]{36}", attempt_id):
        raise AccessDenied("Exact owned attempt identity required")
    if directory.exists() or directory.resolve() != directory or path.resolve() != path:
        raise AccessDenied("New physical private Git metadata is required")
    if observer.source(path) != intent["git_source"]:
        raise AccessDenied("Original Git source changed")
    common = Path(
        observer.query(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        .decode()
        .strip()
    ).resolve()
    gitdir = Path(
        observer.query(path, "rev-parse", "--path-format=absolute", "--git-dir").decode().strip()
    ).resolve()
    if "original_index" not in intent:
        raise AccessDenied("Checked original index provenance is required")
    if file_state(gitdir / "index") != intent["original_index"]:
        raise AccessDenied("Original index changed since the checked snapshot")
    objects = common / "objects"
    if observer.query(path, "rev-parse", "--show-object-format").strip() != b"sha1":
        raise AccessDenied("This confined Git adapter requires the admitted SHA-1 object format")
    if any((objects / "info" / name).exists() for name in ("alternates", "http-alternates")):
        raise AccessDenied("Additional base object stores require separate admission")
    hooks = Path(
        observer.query(path, "rev-parse", "--path-format=absolute", "--git-path", "hooks")
        .decode()
        .strip()
    )
    copied = hooks_state(hooks)
    if sum((hooks / name).stat().st_size for name in copied) > 1048576:
        raise AccessDenied("Git hooks exceed their finite source budget")
    directory.mkdir(mode=0o700)
    branch = "refs/heads/managed/" + attempt_id
    for name in ("objects/info", "objects/pack", "refs/heads/managed", "hooks"):
        (directory / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / "HEAD").write_text("ref: " + branch + "\n")
    (directory / branch).write_text(intent["base"] + "\n")
    (directory / "objects/info/alternates").write_text(str(objects) + "\n")
    config = directory / "config"
    config.touch(mode=0o600)
    clean = {
        "PATH": os.defpath,
        "HOME": str(directory),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }

    def setting(*args):
        subprocess.run(
            [observer.executable, "config", "--file", str(config), *args],
            env=clean,
            check=True,
            capture_output=True,
            timeout=5,
        )

    for entry in observer.query(path, "config", "--null", "--list").split(b"\0"):
        if not entry:
            continue
        key, separator, value = entry.partition(b"\n")
        key = key.decode()
        if key.lower() == "include.path" or key.lower().startswith("includeif."):
            continue
        setting("--add", key, value.decode() if separator else "true")
    for key, value in (
        ("core.hooksPath", str(directory / "hooks")),
        ("core.worktree", str(path)),
        ("core.bare", "false"),
    ):
        setting("--replace-all", key, value)
    for name, fact in copied.items():
        destination = directory / "hooks" / name
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(hooks / name, destination)
        destination.chmod(fact["mode"])
    record = {
        "attempt_id": attempt_id,
        "base": intent["base"],
        "branch": branch,
        "worktree": str(path),
        "metadata": str(directory),
        "physical": identity(directory),
        "common": str(common),
        "gitdir": str(gitdir),
        "objects": str(objects),
        "objects_physical": identity(objects),
        "git": observer.executable,
        "git_source": intent["git_source"],
        "config_sha256": sha(config),
        "hooks": copied,
        "original_index": intent["original_index"],
        "original_head": sha(gitdir / "HEAD"),
    }
    query(record, "read-tree", intent["base"])
    if file_state(gitdir / "index") != record["original_index"]:
        raise AccessDenied("Original index changed during private metadata preparation")
    if observer.source(path) != intent["git_source"] or hooks_state(hooks) != copied:
        raise AccessDenied("Git source changed during private metadata preparation")
    write_private(directory.parent / "preparation.json", record)
    return record


def verify(record):
    metadata = Path(record["metadata"])
    if metadata.resolve() != metadata or identity(metadata) != record["physical"]:
        raise AccessDenied("Private Git metadata identity changed")
    total, count = 0, 0
    for path in metadata.rglob("*"):
        count += 1
        total += path.stat().st_size if path.is_file() else 0
        if count > 8192 or total > 33554432:
            raise AccessDenied("Private Git metadata exceeds its finite total budget")
        if path.is_symlink() or (not path.is_dir() and not path.is_file()):
            raise AccessDenied("Private Git metadata contains an unsupported entry")
        if path.is_file() and path.stat().st_size > 4194304:
            raise AccessDenied("Private Git metadata exceeded its finite file budget")
    if (
        sha(metadata / "config") != record["config_sha256"]
        or hooks_state(metadata / "hooks") != record["hooks"]
        or (metadata / "objects/info/alternates").read_text() != record["objects"] + "\n"
        or identity(Path(record["objects"])) != record["objects_physical"]
        or any((metadata / "objects/info" / name).exists() for name in ("http-alternates",))
    ):
        raise AccessDenied("Private Git configuration, hooks or object lineage changed")
