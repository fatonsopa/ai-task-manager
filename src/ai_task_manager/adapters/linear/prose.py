import re

from markdown_it import MarkdownIt

from ai_task_manager.domains.work.errors import ValidationError
from ai_task_manager.domains.work.models import RenderedProse

from .links import explicit_https_links, literal_blocks


def _heading_separators(text):
    """Insert source-line separators, never serialize Markdown or rewrite containers."""
    tokens = MarkdownIt("commonmark", {"maxNesting": 32}).parse(text)
    breaks = list(re.finditer(r"\r\n|\r|\n", text))
    starts = [0, *(match.end() for match in breaks)]
    insertions = []
    for index, token in enumerate(tokens):
        if token.type != "heading_open":
            continue
        end = token.map[1]
        if end >= len(starts) or starts[end] == len(text):
            continue
        after = starts[end + 1] if end + 1 < len(starts) else len(text)
        if not text[starts[end] : after].strip(" \t\r\n"):
            continue
        if token.level:
            # A quoted blank line contains '>'; the parser's next source map
            # recognizes that existing separator without guessing its prefixes.
            # With no later block, only non-rendering source remains: there
            # is no following content that needs a heading separator.
            following = next(
                (t for t in tokens[index + 1 :] if t.block and t.map and t.map[0] >= end),
                None,
            )
            if following is None or following.map[0] > end:
                continue
            raise ValidationError(
                "Separate container headings from following content before approval"
            )
        insertions.append((starts[end], breaks[end - 1].group()))
    result, previous = [], 0
    for offset, newline in insertions:
        result.extend((text[previous:offset], newline))
        previous = offset
    result.append(text[previous:])
    return "".join(result)


class LinearMarkdownRenderer:
    """Versioned preapproval heading separators, links, lists and terminal document LF."""

    representation = "linear-markdown-v4"
    historical_representations = frozenset(
        {"linear-markdown-v1", "linear-markdown-v2", "linear-markdown-v3"}
    )

    def render(self, prose):
        blocks, fence_starts = literal_blocks(prose.body)
        # Apply the existing list rules on the original offsets first. A list
        # marker introducing a fence belongs to its container, not its code.
        lines, offset = [], 0
        regions = iter(blocks.items())
        region = next(regions, None)
        for line in prose.body.split("\n"):
            end = offset + len(line) + 1
            while region is not None and region[1] <= offset:
                region = next(regions, None)
            literal = region is not None and region[0] < end
            if not literal or offset in fence_starts:
                line = re.sub(r"^( {0,3})- ", r"\1* ", line)
                if (
                    re.match(r"^ {0,3}\* ", line)
                    and len(lines) >= 2
                    and lines[-1] == ""
                    and re.match(r"^ {0,3}\* ", lines[-2])
                ):
                    lines.pop()
            lines.append(line)
            offset = end
        # List normalization changes offsets, so protect the final source map
        # used by the link scanner rather than reusing stale positions.
        body = _heading_separators(explicit_https_links("\n".join(lines)))
        # Linear drops terminal document newlines. Normalize only those LF bytes
        # before approval; internal lines, spaces and literal contents stay exact.
        return RenderedProse(prose.title, body.rstrip("\n"), self.representation)
