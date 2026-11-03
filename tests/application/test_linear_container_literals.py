"""Container source boundaries, independently spelled expected provider bodies."""

from dataclasses import replace

import pytest

from ai_task_manager.adapters.linear.prose import LinearMarkdownRenderer
from ai_task_manager.domains.work.errors import ValidationError
from ai_task_manager.domains.work.models import TaskProse
from tests.application.helpers import FEATURE_BODY
from tests.application.test_linear_prose import approve, mutations

URL = "https://example.test/docs"
LINK = f"[{URL}](<{URL}>)"


@pytest.mark.parametrize(
    "source, expected",
    [
        (f"> ~~~text\n> {URL}\n> ~~~", f"> ~~~text\n> {URL}\n> ~~~"),
        (f"- ~~~text\n  {URL}\n  ~~~", f"* ~~~text\n  {URL}\n  ~~~"),
        (f"> > ~~~text\n> > {URL}\n> > ~~~", f"> > ~~~text\n> > {URL}\n> > ~~~"),
        (f"- > ~~~text\n  > {URL}\n  > ~~~", f"* > ~~~text\n  > {URL}\n  > ~~~"),
        (f"> - ~~~text\n>   {URL}\n>   ~~~", f"> - ~~~text\n>   {URL}\n>   ~~~"),
        (f"- - ~~~text\n    {URL}\n    ~~~", f"* - ~~~text\n    {URL}\n    ~~~"),
        (f"1. ~~~text\n   {URL}\n   ~~~", f"1. ~~~text\n   {URL}\n   ~~~"),
        (f"> ```text\n> {URL}\n> ```", f"> ```text\n> {URL}\n> ```"),
        (f"- ```text\n  {URL}\n  ```", f"* ```text\n  {URL}\n  ```"),
        (f"> ~~~~text\n> ~~~\n> {URL}\n> ~~~~", f"> ~~~~text\n> ~~~\n> {URL}\n> ~~~~"),
        (
            f"> ~~~text\n> ~~~ not a close\n> {URL}\n> ~~~",
            f"> ~~~text\n> ~~~ not a close\n> {URL}\n> ~~~",
        ),
        (
            f"~~~text\n~~~ not a close\n- literal {URL}\n~~~",
            f"~~~text\n~~~ not a close\n- literal {URL}\n~~~",
        ),
        (f"> ~~~text\n> {URL}\n\nRead {URL}", f"> ~~~text\n> {URL}\n\nRead {LINK}"),
        (f"- ~~~text\n  {URL}\n\nRead {URL}", f"* ~~~text\n  {URL}\n\nRead {LINK}"),
        (f">     {URL}", f">     {URL}"),
        (f"- item\n\n      {URL}", f"* item\n\n      {URL}"),
        (f"- item\n\n    Read {URL}", f"* item\n\n    Read {LINK}"),
        (f">\t~~~text\n>\t{URL}\n>\t~~~", f">\t~~~text\n>\t{URL}\n>\t~~~"),
        (f"> ~~~text\r\n> {URL}\r\n> ~~~", f"> ~~~text\r\n> {URL}\r\n> ~~~"),
        (f"~~~text\n- literal\n\n- {URL}\n~~~", f"~~~text\n- literal\n\n- {URL}\n~~~"),
        (
            f"- ~~~text\n  - literal\n\n  - {URL}\n  ~~~",
            f"* ~~~text\n  - literal\n\n  - {URL}\n  ~~~",
        ),
        (f"> paragraph\n> {URL}", f"> paragraph\n> {LINK}"),
        (f"- first {URL}\n\n- second {URL}", f"* first {LINK}\n* second {LINK}"),
    ],
)
def test_lc01_lc04_container_boundaries(source, expected):
    # The same ordinary sibling after the block prevents a blanket skip from passing.
    source += f"\n\nSibling {URL}."
    expected += f"\n\nSibling {LINK}."
    renderer = LinearMarkdownRenderer()
    prose = TaskProse("Preserve literal source", source)
    actual = renderer.render(prose)
    assert actual.body == expected
    assert renderer.render(replace(prose, body=actual.body)) == actual


def test_lc04_nesting_limit_refuses_before_approval():
    with pytest.raises(ValidationError, match="nesting"):
        LinearMarkdownRenderer().render(TaskProse("Nested source", "> " * 40 + URL))


@pytest.mark.parametrize("change_literal", [False, True])
def test_lc05_actual_writer_and_http_preserve_nested_literals(composed, change_literal):
    service, _, provider, _ = composed
    source = FEATURE_BODY + f"\n> ~~~text\n> {URL}\n> ~~~\n\nRead {URL}.\n"
    expected = FEATURE_BODY.rstrip("\n") + f"\n\n> ~~~text\n> {URL}\n> ~~~\n\nRead {LINK}."
    ref, prepared = approve(service, source)
    view = service.writer.approvals_for_tasks(service.scope)
    approved = view.prose(ref)
    assert approved.body == expected
    assert prepared["packet"]["output"] == "# " + approved.title + "\n\n" + expected

    def response(request, data):
        if change_literal and "mutation CreateIssue" in request["query"]:
            issue = data["issueCreate"]["issue"]
            issue["description"] = issue["description"].replace(f"> {URL}", f"> {LINK}")
            provider.issues[issue["id"]]["description"] = issue["description"]

    provider.hook = response
    submitted = service.call("task_submit", {"writing_ref": ref, "operation_key": "nested-code"})
    assert len(mutations(provider)) == 1
    assert mutations(provider)[0]["variables"]["input"]["description"] == expected
    if change_literal:
        assert not submitted["ok"] or submitted["outcome"] != "confirmed"
        task = service.core.list_tasks(service.handle)[0]
        assert task.body == expected and task.sync_state != "confirmed"
    else:
        assert submitted["ok"] and submitted["outcome"] == "confirmed", submitted
        task = submitted["data"]
        assert provider.issues[task["issue_id"]]["description"] == task["body"] == expected
