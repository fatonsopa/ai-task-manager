"""Same product consumer in independent authored projects; no application script fixtures."""

import json
import os
from dataclasses import asdict, fields

import pytest
from f1_fixtures import Rig

from ai_task_manager.adapters.git.profile import GitProfile
from ai_task_manager.adapters.git.publication import LocalGit
from ai_task_manager.domains.merging.contracts import Publication
from ai_task_manager.domains.work.errors import AccessDenied, StaleVersion, ValidationError


@pytest.fixture
def project(tmp_path, request):
    target, mode = request.param if hasattr(request, "param") else ("trunk", "cas")
    rig = Rig(
        tmp_path / "owned",
        os.environ["F1_SELECTED_GIT"],
        target=target,
        mode=mode,
        project="project-" + target.replace("/", "-"),
    )
    yield rig
    rig.verify_closed()


@pytest.mark.parametrize(
    "project",
    [
        ("trunk", "cas"),
        ("release/next", "cas"),
        ("trunk", "fast_forward"),
        ("release/next", "fast_forward"),
    ],
    indirect=True,
)
def test_same_application_consumer_with_distinct_targets(project):
    result = project.publish_through_service()
    assert result["publication"] == "committed" and result["quiescent"]
    assert result["target_ref"] == "refs/heads/" + project.target
    assert result["repository_id"] == project.binding.repository_id
    assert result["configuration_digest"] == project.binding.configuration_digest
    assert project.dev() == project.new
    if project.request.mode == "fast_forward":
        assert (project.repo / "a.txt").read_text() == "new\n"
    record = {
        "binding": asdict(project.binding),
        "request": asdict(project.request),
        "result": result,
        "events": project.events(),
        "check_observations": project.checks.calls,
    }
    (project.root / "standalone-publication-evidence.json").write_text(json.dumps(record, indent=2))


def test_caller_cannot_supply_target_or_executable(project):
    before = project.events()
    for key, value in [("target_ref", "refs/heads/foreign"), ("executable", "/untrusted/git")]:
        with pytest.raises(TypeError):
            Publication(**dict(asdict(project.request), **{key: value}))
    assert "target_ref" not in {f.name for f in fields(Publication)}
    assert project.events() == before and project.dev() == project.old


@pytest.mark.parametrize("change", ["target_mutation", "repository_id", "configuration_digest"])
def test_configuration_cannot_redirect_existing_operation(project, change):
    before = project.events()
    if change == "target_mutation":
        project.profile.repositories[project.binding.worktree_id]["target_ref"] = "refs/heads/other"
    elif change == "repository_id":
        project.profile.repositories[project.binding.worktree_id]["repository_id"] = "foreign"
    else:
        project.profile.repositories[project.binding.worktree_id]["configuration_digest"] = "f" * 64
    with pytest.raises(AccessDenied):
        project.publish_through_service()
    assert project.events() == before and project.dev() == project.old


def test_cross_repository_profile_cannot_borrow_binding(project, tmp_path):
    other = Rig(
        tmp_path / "unrelated",
        os.environ["F1_SELECTED_GIT"],
        target="release/next",
        project="another-project",
    )
    try:
        adapter = LocalGit(project.journal, other.profile, project.operations)
        with pytest.raises(AccessDenied):
            adapter.publish(
                project.ref,
                project.journal.current(project.ref)["version"],
                "wrong-repository",
                project.request,
            )
        assert project.dev() == project.old and other.dev() == other.old
        assert not project.journal.current(project.ref)["facts"].get("local_git")
    finally:
        other.verify_closed()


@pytest.mark.parametrize(
    "target",
    [
        "dev",
        "refs/tags/v1",
        "refs/heads/../foreign",
        "refs/heads/.hidden",
        "refs/heads/main.lock/child",
    ],
)
def test_invalid_trusted_target_ref_is_refused_before_effect(project, target):
    profile = GitProfile(root=project.root, executable=project.executable, home=project.home)
    with pytest.raises(ValidationError):
        profile.enroll(project.repo, worktree_id="new", repository_id="new", target_ref=target)
    assert profile.repositories == {} and project.dev() == project.old


def test_duplicate_enrollment_cannot_replace_target(project):
    before = dict(project.profile.repositories)
    with pytest.raises(AccessDenied):
        project.profile.enroll(
            project.repo,
            worktree_id=project.binding.worktree_id,
            repository_id=project.binding.repository_id,
            target_ref="refs/heads/foreign",
        )
    assert project.profile.repositories == before


def test_committed_retry_survives_expired_checks_and_has_no_new_effect(project):
    first = project.publish_through_service()
    events = project.events()
    logfile = project.repo / ".git/logs" / ("refs/heads/" + project.target)
    history = logfile.read_bytes()
    project.checks.unavailable = True
    project.profile.repositories[project.binding.worktree_id]["target_ref"] = "refs/heads/drift"
    assert project.publish_through_service() == first
    assert project.events() == events and logfile.read_bytes() == history


def test_new_effect_requires_fresh_checks_and_current_authority(project):
    project.checks.unavailable = True
    before = project.events()
    with pytest.raises(AccessDenied):
        project.publish_through_service()
    assert project.events() == before and project.dev() == project.old
    project.checks.unavailable = False
    project.authority.active = False
    with pytest.raises(AccessDenied):
        project.publish_through_service()
    assert project.events() == before and project.dev() == project.old


def test_stale_caller_version_is_not_replaced_by_latest(project):
    before = project.events()
    with pytest.raises(StaleVersion):
        project.operations.publish(project.ref, 0, "stale", project.request, project.check_ref)
    assert project.events() == before and project.dev() == project.old


def test_foreign_historical_consumer_is_denied(project):
    project.publish_through_service()
    before = project.events()
    project.authority.project = "foreign-project"
    with pytest.raises(AccessDenied):
        project.publish_through_service()
    assert project.events() == before
