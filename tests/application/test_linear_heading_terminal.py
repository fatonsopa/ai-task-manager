"""HS03 / HEADINGS-P2-1: terminal container source maps preserve existing separators."""

import pytest

from ai_task_manager.adapters.linear.prose import LinearMarkdownRenderer, _heading_separators
from ai_task_manager.domains.work.errors import ValidationError
from ai_task_manager.domains.work.models import TaskProse


@pytest.mark.parametrize(
    "source",
    [
        "> # Heading\n>\n",
        "> # Heading\n>",
        "> # Heading\n> \t\n",
        "> # Heading\n>\n>\n",
        "> # Heading\r\n>\r\n",
        "> # Heading\r>\r",
        "> Heading\n> ===\n>\n",
        "> Heading\n> ---\n>\n",
        "> > # Heading\n> >\n",
        "> > Heading\n> > ---\n> >\n",
        "> > # Heading\n>\n",
        "> # Heading\n>\nOutside quote",
        "> > # Heading\n>\n> Outer paragraph",
        "1. # Heading\n\n",
        "1. # Heading\n   \n",
        "1. # Heading\n\n2. Next item",
        "* # Heading\n  \n",
        "> 1. # Heading\n>\n",
        "> 1. Heading\n>    ---\n>\n",
        "1. > # Heading\n   >\n",
        "1. > Heading\n   > ---\n   >\n",
        "1. > # Heading\n   >\n\n2. Next item",
        "> * > # Heading\n>   >\n",
        # Definitions produce no rendered block; they must not become paragraphs.
        "> # Heading\n>\n> [ref]: /target\n",
    ],
)
def test_hs03_terminal_container_separators_stay_exact(source):
    assert _heading_separators(source) == source
    renderer = LinearMarkdownRenderer()
    actual = renderer.render(TaskProse("Terminal boundary", source))
    assert actual.body == source.rstrip("\n")
    assert actual.representation == "linear-markdown-v4"
    assert renderer.render(TaskProse(actual.title, actual.body)) == actual


@pytest.mark.parametrize(
    "source",
    [
        "> # Heading\n> text\n>\n",
        "> Heading\n> ---\n> text\n>\n",
        "> # Heading\nOutside quote\n",
        "> # Heading\n> ## Next\n>\n",
        "> # Heading\n> > nested\n>\n",
        "> # Heading\n> ```text\n> code\n> ```\n>\n",
        "> # Heading\n> ---\n>\n",
        "> > # Heading\n> Outer paragraph\n>\n",
        "1. # Heading\n2. Next item\n",
        "1. # Heading\n   text\n\n",
        "> 1. # Heading\n>    text\n>\n",
        "1. > # Heading\n   > text\n   >\n",
        "> * > # Heading\n>   > text\n>   >\n",
    ],
)
def test_hs03_terminal_blank_does_not_hide_immediate_content(source):
    with pytest.raises(ValidationError, match="container headings"):
        LinearMarkdownRenderer().render(TaskProse("Terminal refusal", source))
