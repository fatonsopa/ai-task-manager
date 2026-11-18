"""Bind the fixture's C-after-A declaration after actual scoped Writer issuance."""

import argparse
import json
from pathlib import Path


def finalize(plan_file):
    from ai_task_manager.adapters.decisions.journal import AuthorityJournal
    from ai_task_manager.adapters.linear.prose import LinearMarkdownRenderer
    from ai_task_manager.adapters.writing.package import WriterPackage
    from ai_task_manager.adapters.writing.service import WritingService
    from ai_task_manager.config.files import read_private, write_private
    from ai_task_manager.domains.work.models import Scope
    from tests.install.qualification import approved_code_merge_milestone

    plan_file = Path(plan_file).resolve()
    plan = read_private(plan_file)
    assert not (plan_file.parent / "execution.json").exists()
    storage = plan["writer_storage"]
    package = read_private(plan["writer_package"])
    writer = WritingService(
        WriterPackage(Path(package["root"]), package["python"], package["pins"]),
        AuthorityJournal.open_existing(storage["journal"], storage["key"]),
        storage["sessions_root"],
        LinearMarkdownRenderer(),
    )
    selected = plan["scope"]
    scope = Scope(
        "linear",
        selected["workspace"],
        selected["owner"],
        selected["project"],
        plan["logical_session"],
    )
    refs = {
        group: read_private(plan["roles"][group + "_author"]["writing_result"])["approval_ref"]
        for group in "ac"
    }
    plan["dependency_milestones"] = [
        approved_code_merge_milestone(writer.approvals_for_tasks(scope), refs["c"], refs["a"])
    ]
    write_private(plan_file, plan, replace=True)
    return plan["dependency_milestones"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    args = parser.parse_args()
    print(json.dumps(finalize(args.plan)))
