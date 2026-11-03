"""Host Git observations for release must not start project-configured helpers."""

import os
import subprocess
from pathlib import Path

from ai_task_manager.supervisor.models import RuntimeRefused

_CONFIG = (
    "core.fsmonitor=false",
    "core.hooksPath=/dev/null",
    "core.untrackedCache=false",
    "gc.auto=0",
    "maintenance.auto=false",
    "submodule.recurse=false",
    "diff.ignoreSubmodules=all",
    "protocol.allow=never",
)


def _run(repository, args):
    result = subprocess.run(
        ["git", *[a for setting in _CONFIG for a in ("-c", setting)],
         "-C", str(repository), *args],
        env={
            "PATH": os.defpath,
            "HOME": str(Path.home()),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode:
        raise RuntimeRefused(f"Git resource release failed: {result.stderr.strip()}")
    return result.stdout.strip()


def release_git(repository, *args, worktree=None):
    # `config --list` reads includes and per-worktree config without executing
    # filters. Refuse them rather than changing their content comparison rules.
    # The final remove spawns status in its target, whose config may differ.
    for path in dict.fromkeys((repository, worktree or repository)):
        config = _run(path, ("config", "--null", "--list"))
        for field in config.split("\0"):
            key, _, value = field.partition("\n")
            key = key.lower()
            if key.startswith("filter.") and key.rsplit(".", 1)[-1] in {
                "clean", "smudge", "process",
            } and value:
                raise RuntimeRefused("Executable Git filters require confined resource release")
            if key == "extensions.partialclone" or (
                key.startswith("remote.") and key.endswith(".promisor")
            ):
                raise RuntimeRefused("Partial Git repositories require confined resource release")
    return _run(repository, args)
