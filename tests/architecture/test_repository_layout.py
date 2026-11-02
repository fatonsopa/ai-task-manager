"""Owned layout/consumer checks; no product, account or acceptance execution."""

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_build_control_policy_migration import normalize_relocated_readme_commands

ROOT = Path(__file__).resolve().parents[2]


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


LAYOUT = module(ROOT / "tools/repository_layout.py", "owned_layout_checker")
HANDOFF = module(ROOT / "tools/check_handoff.py", "owned_handoff_checker")
CREDENTIALS = module(ROOT / "tools/setup_test_credentials.py", "owned_credential_helper")


@pytest.fixture
def minimal(tmp_path):
    root = tmp_path / "repository"
    (root / "config").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "src/ai_task_manager").mkdir(parents=True)
    (root / "src/ai_task_manager/component.py").write_text("import json\n")
    contract = json.loads((ROOT / "config/repository-layout.json").read_text())
    contract["relocations"] = []
    contract["authorities"] = {"layout": "config/repository-layout.json"}
    (root / "config/repository-layout.json").write_text(json.dumps(contract))
    assert LAYOUT.verify(root)["passed"]
    return root


def alter_contract(root, change):
    path = root / "config/repository-layout.json"
    value = json.loads(path.read_text())
    change(value)
    path.write_text(json.dumps(value))


def test_current_repository_has_one_owner_and_no_runtime_build_dependency():
    result = LAYOUT.verify(ROOT)
    assert result["passed"], result
    assert result["product_acceptance_tests_executed"] == 0


@pytest.mark.parametrize(
    "path,body,reason",
    [
        ("unknown.txt", "unowned", "one responsibility"),
        ("docs/options.json", "{}", "machine data"),
        ("docs/prototype.html", "<script></script>", "machine data"),
        ("config/helper.py", "pass\n", "machine configuration"),
        ("config/live.sqlite", "not a database", "private operational"),
        ("config/test-credentials.env", "DUMMY=not-a-token", "private operational"),
        ("src/ai_task_manager/component.py", "import tools.build_control\n", "non-runtime"),
        ("src/ai_task_manager/component.py", "from ..tools import build_control\n", "non-runtime"),
        ("src/ai_task_manager/component.py", "from ...tools import build_control\n", "escapes"),
        ("src/ai_task_manager/component.py", "from docs import helper\n", "non-runtime"),
        ("src/ai_task_manager/component.py", "import prototypes.terminal\n", "non-runtime"),
    ],
)
def test_placement_and_actual_import_negatives(minimal, path, body, reason):
    target = minimal / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    result = LAYOUT.verify(minimal)
    assert not result["passed"]
    assert any(reason in finding for finding in result["findings"]), result


def test_supported_relative_runtime_import_is_retained(minimal):
    (minimal / "src/ai_task_manager/component.py").write_text("from . import sibling\n")
    assert LAYOUT.verify(minimal)["passed"]


@pytest.mark.parametrize("variant", ["duplicate_owner", "missing_authority", "duplicate_authority"])
def test_authority_and_owner_declarations(minimal, variant):
    def change(value):
        if variant == "duplicate_owner":
            value["roles"].append(
                {"id": "competing", "paths": ["config/"], "responsibility": "wrong"}
            )
        elif variant == "missing_authority":
            value["authorities"]["missing"] = "config/absent.json"
        else:
            value["authorities"]["duplicate"] = "config/repository-layout.json"

    alter_contract(minimal, change)
    assert not LAYOUT.verify(minimal)["passed"]


@pytest.mark.parametrize("variant", ["escape", "collision", "missing", "old_exists", "changed"])
def test_move_manifest_guards(minimal, variant):
    row = {
        "old": "docs/former.json",
        "new": "config/repository-layout.json",
        "byte_preserved": False,
    }
    if variant == "escape":
        row["new"] = "../unowned.json"
    elif variant == "missing":
        row["new"] = "config/missing.json"
    elif variant == "old_exists":
        (minimal / row["old"]).write_text("{}")
    elif variant == "changed":
        row.update(byte_preserved=True, before_sha256="0" * 64)
    alter_contract(
        minimal,
        lambda value: value.update(relocations=[row, row] if variant == "collision" else [row]),
    )
    assert not LAYOUT.verify(minimal)["passed"]


@pytest.mark.parametrize("variant", ["schema", "bool_schema", "missing_roles", "missing_suffixes"])
def test_malformed_layout_is_a_failed_check(minimal, variant):
    def change(value):
        if variant == "schema":
            value["schema_version"] = 999
        elif variant == "bool_schema":
            value["schema_version"] = True
        elif variant == "missing_roles":
            value.pop("roles")
        else:
            value.pop("document_suffixes")

    alter_contract(minimal, change)
    assert not LAYOUT.verify(minimal)["passed"]


def test_public_symlink_is_not_followed(minimal, tmp_path):
    sentinel = tmp_path / "foreign.txt"
    sentinel.write_bytes(b"unchanged independent sentinel")
    (minimal / "docs/link.md").symlink_to(sentinel)
    assert not LAYOUT.verify(minimal)["passed"]
    assert sentinel.read_bytes() == b"unchanged independent sentinel"


def test_all_twelve_declared_moves_and_byte_preservation():
    contract = json.loads((ROOT / "config/repository-layout.json").read_text())
    assert len(contract["relocations"]) == 12
    assert len({row["new"] for row in contract["relocations"]}) == 12
    for row in contract["relocations"]:
        assert not (ROOT / row["old"]).exists()
        assert (ROOT / row["new"]).is_file()
        if row["byte_preserved"]:
            assert (
                hashlib.sha256((ROOT / row["new"]).read_bytes()).hexdigest() == row["before_sha256"]
            )


def test_acceptance_source_relocation_preserves_the_original_complete_inventory():
    contract = json.loads((ROOT / "config/repository-layout.json").read_text())
    row = next(
        row for row in contract["relocations"] if row["new"] == "tests/acceptance/cases.json"
    )
    value = json.loads((ROOT / row["new"]).read_text())
    new = "../../config/execution-policy.json#/acceptance/required_profiles"
    old = "execution-policy.json#/acceptance/required_profiles"
    assert value["matrix"]["required_profiles_source"] == new
    assert (
        ROOT / "tests/acceptance" / new.split("#")[0]
    ).resolve() == ROOT / "config/execution-policy.json"
    value["matrix"]["required_profiles_source"] = old
    value["matrix"]["profiles"] = value["matrix"]["profiles"].replace(new, old)
    assert (
        hashlib.sha256((json.dumps(value, indent=2) + "\n").encode()).hexdigest()
        == row["before_sha256"]
    )
    assert len(value["cases"]) == 248 and len(value["workflows"]) == 14


def test_exact_command_path_normalization_keeps_original_hash_oracles():
    text = normalize_relocated_readme_commands((ROOT / "docs/readme.md").read_text())
    baseline = json.loads(
        (ROOT / "tests/fixtures/build_control/planning-before-authorization.json").read_text()
    )
    expected = baseline["documentation_invariants"]["readme.md"]["code_block_sha256"]
    assert [
        hashlib.sha256(block.encode()).hexdigest() for block in re.findall(r"```.*?```", text, re.S)
    ] == expected


@pytest.mark.parametrize("variant", ["name", "flag", "unrelated_path"])
def test_path_adapter_does_not_accept_other_command_changes(variant):
    text = (ROOT / "docs/readme.md").read_text()
    if variant == "name":
        text = text.replace("python3 tools/check_handoff.py", "python3 tools/other.py")
    elif variant == "flag":
        text = text.replace(
            "setup_test_credentials.py --secondary-only", "setup_test_credentials.py --init"
        )
    else:
        text = text.replace(
            "python3 tools/check_handoff.py", "python3 tools/check_handoff.py --root /unrelated"
        )
    with pytest.raises(AssertionError):
        normalize_relocated_readme_commands(text)


def isolated_environment(home):
    home.mkdir(mode=0o700)
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


@pytest.mark.parametrize("root_argument", ["default", "repository", "documentation"])
def test_relocated_handoff_command_resolves_real_inputs_from_unrelated_cwd(tmp_path, root_argument):
    command = [sys.executable, "-I", str(ROOT / "tools/check_handoff.py")]
    if root_argument != "default":
        command += ["--root", str(ROOT / "docs" if root_argument == "documentation" else ROOT)]
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=isolated_environment(tmp_path / "home"),
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode() + result.stdout.decode()
    report = json.loads(result.stdout)
    assert report["passed"] and report["product_acceptance_tests_executed"] == 0
    assert report["counts"]["requirements"] == 38 and report["counts"]["cases"] == 248


@pytest.mark.parametrize("name", ["check_handoff.py", "setup_test_credentials.py"])
def test_relocated_command_help_preserves_flags(tmp_path, name):
    result = subprocess.run(
        [sys.executable, "-I", str(ROOT / "tools" / name), "--help"],
        cwd=tmp_path,
        env=isolated_environment(tmp_path / "home"),
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0
    expected = (
        [b"--root"]
        if name == "check_handoff.py"
        else [b"--init", b"--secondary-only", b"--directory"]
    )
    assert all(flag in result.stdout for flag in expected)


def test_empty_credential_template_is_owned_and_preserved(tmp_path, capsys):
    directory = tmp_path / "owned-credentials"
    assert CREDENTIALS.main(["--init", "--directory", str(directory)]) == 0
    target = directory / "test-credentials.env"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert target.stat().st_mode & 0o777 == 0o600
    before = target.read_bytes()
    assert CREDENTIALS.main(["--init", "--directory", str(directory)]) == 0
    assert target.read_bytes() == before
    assert "TOKEN=\n" in before.decode()
    assert "LINEAR_TEST_USER" not in capsys.readouterr().out


def test_credential_helper_refuses_symlink_destination(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    owned = tmp_path / "owned"
    owned.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        CREDENTIALS.main(["--init", "--directory", str(owned)])
    assert not list(outside.iterdir())


def test_layout_command_checks_actual_repository_without_product_execution(tmp_path):
    result = subprocess.run(
        [sys.executable, "-I", str(ROOT / "tools/repository_layout.py"), "--root", str(ROOT)],
        cwd=tmp_path,
        env=isolated_environment(tmp_path / "home"),
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout.decode() + result.stderr.decode()
    assert json.loads(result.stdout)["product_acceptance_tests_executed"] == 0


def test_declared_runtime_package_has_no_source_tool_or_prototype_dependency():
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/ai_task_manager"
    ]
    assert LAYOUT.verify(ROOT)["passed"]


def test_documentation_manifest_is_reproducible_from_owned_inputs():
    expected = json.loads((ROOT / "config/documentation-manifest.json").read_text())
    assert HANDOFF.build_manifest(ROOT) == expected
    assert expected["path_base"] == "repository_root"
    assert "config/documentation-manifest.json" not in {row["path"] for row in expected["files"]}


def test_only_the_reviewed_public_build_index_is_unignored(tmp_path):
    env = isolated_environment(tmp_path / "home")
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
    result = subprocess.run(
        [
            "/usr/bin/git",
            "-c",
            "core.excludesFile=/dev/null",
            "-C",
            str(ROOT),
            "check-ignore",
            "--no-index",
            "config/build/evidence-index.json",
            "config/build/unreviewed.json",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.splitlines() == [b"config/build/unreviewed.json"]


def test_public_build_metadata_is_not_hidden_by_output_exclusions(minimal):
    target = minimal / "config/build/live.sqlite"
    target.parent.mkdir()
    target.write_bytes(b"authored non-database sentinel")
    result = LAYOUT.verify(minimal)
    assert not result["passed"]
    assert any("private operational" in finding for finding in result["findings"])
