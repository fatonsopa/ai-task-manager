import re

from ai_task_manager.domains.work.models import RenderedProse

from .links import explicit_https_links, literal_blocks


class LinearMarkdownRenderer:
    """Versioned preapproval links, list encoding and terminal document LF."""

    representation = "linear-markdown-v3"
    historical_representations = frozenset({"linear-markdown-v1", "linear-markdown-v2"})

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
        body = explicit_https_links("\n".join(lines))
        # Linear drops terminal document newlines. Normalize only those LF bytes
        # before approval; internal lines, spaces and literal contents stay exact.
        return RenderedProse(prose.title, body.rstrip("\n"), self.representation)
