#!/usr/bin/env python3
"""Check public source ownership without executing product or prototype code."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath


def relative(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("A nonempty repository-relative path is required")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or value == "."
        or path.as_posix() != value.rstrip("/")
    ):
        raise ValueError("Escaping or ambiguous paths are unsupported")
    return value


def matching_owners(name, roles):
    return [
        role["id"]
        for role in roles
        if any(name.startswith(p) if p.endswith("/") else name == p for p in role["paths"])
    ]


def imports(path, source_root):
    tree = ast.parse(path.read_text(), filename=str(path))
    package = list(path.parent.relative_to(source_root).parts)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (name.name for name in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level > len(package) + 1:
                    raise ValueError("Relative import escapes the source root")
                prefix = package[: len(package) - node.level + 1]
                yield ".".join(prefix + (node.module or "").split("."))
            else:
                yield node.module or ""


def verify(root):
    root = Path(root).absolute()
    findings = []
    try:
        contract = json.loads((root / "config/repository-layout.json").read_text())
        if type(contract.get("schema_version")) is not int or contract["schema_version"] != 1:
            raise ValueError("Unsupported repository layout schema")
        roles = contract["roles"]
        if not isinstance(roles, list) or not roles:
            raise ValueError("Responsibility owners must be a nonempty list")
        for field in (
            "document_suffixes",
            "configuration_suffixes",
            "ignored_directories",
            "forbidden_runtime_imports",
        ):
            if not isinstance(contract[field], list) or not all(
                isinstance(value, str) and value for value in contract[field]
            ):
                raise ValueError("Invalid layout list: " + field)
        if not isinstance(contract["authorities"], dict) or not isinstance(
            contract["relocations"], list
        ):
            raise ValueError("Invalid authorities or relocations")
        if not all(
            isinstance(name, str) and name and isinstance(value, str)
            for name, value in contract["authorities"].items()
        ):
            raise ValueError("Canonical authorities need named relative paths")
        if not all(
            isinstance(role, dict)
            and isinstance(role.get("id"), str)
            and role["id"]
            and isinstance(role.get("paths"), list)
            and isinstance(role.get("responsibility"), str)
            for role in roles
        ):
            raise ValueError("Malformed responsibility owner")
        identifiers = [role["id"] for role in roles]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Duplicate responsibility owner")
        for role in roles:
            if not role["paths"] or not role["responsibility"]:
                raise ValueError("Missing owned paths or responsibility")
            for path in role["paths"]:
                relative(path)
        ignored = set(contract["ignored_directories"])
        if not {".git", ".local", ".venv"} <= ignored:
            raise ValueError("Private and host state exclusions are required")
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {"check": "repository_layout", "passed": False, "findings": [str(error)]}

    files = []
    for base, directories, names in os.walk(root, followlinks=False):
        for name in list(directories):
            path = Path(base) / name
            if (
                name in ignored and (name not in {"build", "dist", "htmlcov"} or Path(base) == root)
            ) or name.endswith(".egg-info"):
                directories.remove(name)
            elif path.is_symlink():
                findings.append(f"Symbolic public directory: {path.relative_to(root)}")
                directories.remove(name)
        for name in names:
            if name in {".git", ".DS_Store"}:
                continue
            path = Path(base) / name
            key = path.relative_to(root).as_posix()
            files.append(key)
            owners = matching_owners(key, roles)
            if len(owners) != 1:
                findings.append(f"{key}: expected one responsibility owner, found {owners}")
            if path.is_symlink():
                findings.append(f"Symbolic public file: {key}")
                continue
            if (
                key.startswith("config/")
                and path.suffix.lower() not in contract["configuration_suffixes"]
            ):
                findings.append(f"{key}: executable code does not belong in machine configuration")
            if key.startswith("docs/") and path.suffix.lower() not in contract["document_suffixes"]:
                findings.append(
                    f"{key}: executable or machine data does not belong in documentation"
                )
            if path.suffix in {".sqlite", ".sqlite3", ".env"} or name == "test-credentials.env":
                findings.append(f"{key}: private operational state cannot be public source")
            if key.startswith("src/") and path.suffix == ".py":
                try:
                    for imported in imports(path, root / "src"):
                        if imported.split(".")[0] in contract["forbidden_runtime_imports"]:
                            findings.append(f"{key}: runtime imports non-runtime owner {imported}")
                except (SyntaxError, ValueError, UnicodeError) as error:
                    findings.append(f"{key}: import boundary cannot be checked: {error}")

    destinations = []
    for row in contract.get("relocations", []):
        try:
            old, new = relative(row["old"]), relative(row["new"])
            destinations.append(new)
            if (root / old).exists() or (root / old).is_symlink():
                findings.append(f"Obsolete source path remains: {old}")
            target = root / new
            if not target.is_file() or target.is_symlink():
                findings.append(f"Missing regular relocation target: {new}")
            elif row["byte_preserved"]:
                if hashlib.sha256(target.read_bytes()).hexdigest() != row["before_sha256"]:
                    findings.append(f"Unapproved change to byte-preserved relocation: {new}")
        except (KeyError, TypeError, ValueError, OSError) as error:
            findings.append(f"Invalid relocation declaration: {error}")
    if len(destinations) != len(set(destinations)):
        findings.append("Relocation destinations collide")
    authorities = contract.get("authorities", {})
    if len(set(authorities.values())) != len(authorities):
        findings.append("Distinct authority indexes cannot share one accidental path")
    for name, path in authorities.items():
        try:
            relative(path)
            if not (root / path).is_file() or (root / path).is_symlink():
                findings.append(f"Missing canonical {name}: {path}")
        except (TypeError, ValueError) as error:
            findings.append(f"Invalid canonical {name}: {error}")
    return {
        "check": "repository_layout",
        "passed": not findings,
        "findings": findings,
        "public_files_checked": len(files),
        "product_acceptance_tests_executed": 0,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).absolute().parents[1])
    result = verify(parser.parse_args(argv).root)
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
