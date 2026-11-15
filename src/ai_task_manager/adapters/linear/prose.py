import re

from markdown_it import MarkdownIt

from ai_task_manager.domains.work.errors import ValidationError
from ai_task_manager.domains.work.models import RenderedProse

from .links import (
    _balanced_end,
    _code_end,
    _line_end,
    explicit_https_links,
    literal_blocks,
)


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

    @staticmethod
    def literal_spans_only(before, after):
        """Insert file spans only in prose, copying existing protected syntax exactly."""
        blocks, _ = literal_blocks(before)
        i = j = 0
        changed = False
        while i < len(before) and j < len(after):
            end = blocks.get(i)
            char = before[i]
            if end is None and char == "`":
                end = _code_end(before, i)
            elif end is None and char == "\\":
                end = min(i + 2, len(before))
            elif end is None and char == "[":
                end = _balanced_end(before, i, "[", "]")
                if before[end : end + 1] in {"(", "["}:
                    opening = before[end]
                    end = _balanced_end(before, end, opening, ")" if opening == "(" else "]")
                elif before[end : end + 1] == ":" and re.fullmatch(
                    r" {0,3}", before[before.rfind("\n", 0, i) + 1 : i]
                ):
                    after_line = _line_end(before, end)
                    if not before[end + 1 : after_line].strip():
                        after_line = _line_end(before, after_line)
                    end = after_line
            elif end is None and char == "<":
                close = before.find(">", i + 1)
                end = len(before) if close < 0 else close + 1
            if end is not None:
                if before[i:end] != after[j : j + end - i]:
                    return False
                j += end - i
                i = end
                continue
            if before[i] == after[j]:
                i += 1
                j += 1
                continue
            match = re.match(r"`([A-Za-z0-9_./-]+\.[A-Za-z0-9]+)`", after[j:])
            if not match or not before.startswith(match[1], i):
                return False
            i += len(match[1])
            j += len(match[0])
            changed = True
        return changed and i == len(before) and j == len(after)

    def render_decision(self, prose, mention_url):
        """Preserve the verified mention URL before approval; routine rendering is unchanged."""
        from dataclasses import replace

        suffix = "\n\n" + mention_url
        if not prose.body.endswith(suffix):
            raise ValidationError("Decision draft must end with its exact canonical profile URL")
        rendered = self.render(replace(prose, body=prose.body[: -len(suffix)]))
        return RenderedProse(rendered.title, rendered.body + suffix, "linear-decision-markdown-v1")

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
