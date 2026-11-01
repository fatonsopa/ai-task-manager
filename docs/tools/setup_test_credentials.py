#!/usr/bin/env python3
"""Prepare local Linear test credentials without displaying their values."""
import argparse
import getpass
import os
from pathlib import Path
import stat
import sys
import tempfile


TEAM_ID = "cbe80ecc-8035-407d-b531-63cfe083aafc"


def prepare_directory(directory):
    if directory.is_symlink():
        raise ValueError("The credential directory must not be a symbolic link.")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.stat().st_uid != os.getuid():
        raise ValueError("The credential directory belongs to another user.")
    directory.chmod(0o700)


def contents(first="", second=""):
    for token in (first, second):
        if any(c.isspace() for c in token) or "\x00" in token:
            raise ValueError("Tokens must not contain whitespace or NUL characters.")
    return (
        "# Local test credentials. Keep this file outside every repository.\n"
        "# Parse these assignments as data; do not execute this file.\n"
        f"LINEAR_TEST_USER_A_TOKEN={first}\n"
        f"LINEAR_TEST_USER_B_TOKEN={second}\n"
        f"LINEAR_TEST_TEAM_ID={TEAM_ID}\n"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init", action="store_true", help="Create an empty protected template; preserve an existing file.")
    parser.add_argument("--secondary-only", action="store_true", help="Save only the second user token; reuse the project's existing primary credential.")
    parser.add_argument("--directory", type=Path, default=Path.home() / ".config" / "ai-task-manager")
    args = parser.parse_args(argv)
    directory = args.directory.expanduser().absolute()
    prepare_directory(directory)
    target = directory / "test-credentials.env"
    if args.init:
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            if target.is_symlink() or not stat.S_ISREG(target.stat().st_mode):
                raise ValueError("The credential path must be a regular file.")
            if target.stat().st_uid != os.getuid() or stat.S_IMODE(target.stat().st_mode) & 0o077:
                raise ValueError("The existing credential file must belong to you and have mode 600.")
            print("Existing credential file preserved; no values read or displayed.")
            return 0
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(contents())
        print("Protected empty credential template created.")
        return 0
    if not sys.stdin.isatty():
        raise ValueError("Run this helper in your own interactive terminal so token prompts remain hidden.")
    first = ""
    if args.secondary_only and target.exists():
        if target.is_symlink() or not target.is_file() or target.stat().st_uid != os.getuid() or stat.S_IMODE(target.stat().st_mode) & 0o077:
            raise ValueError("An existing credential file must be owned by you, regular, and have mode 600.")
        for line in target.read_text(encoding="utf-8").splitlines():
            if line.startswith("LINEAR_TEST_USER_A_TOKEN="):
                first = line.partition("=")[2]
    elif not args.secondary_only:
        first = getpass.getpass("Linear token for test user A: ").strip()
    second = getpass.getpass("Linear token for test user B: ").strip()
    if not second or (not first and not args.secondary_only) or (first and first == second):
        raise ValueError("Provide distinct nonempty tokens; secondary-only mode may reuse the primary credential from the project. Live tests verify user identities.")
    data = contents(first, second)
    if target.is_symlink():
        raise ValueError("The credential path must not be a symbolic link.")
    if target.exists() and (not target.is_file() or target.stat().st_uid != os.getuid()):
        raise ValueError("An existing credential file must be a regular file owned by you.")
    fd, temporary = tempfile.mkstemp(prefix=".credentials-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print("Credential file saved with mode 600. Token values were not displayed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        raise SystemExit(1)
