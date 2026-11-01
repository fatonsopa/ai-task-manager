import re

from ai_task_manager.domains.work.models import RenderedProse


class LinearMarkdownRenderer:
    """Restricted observed list encoding; preserve fenced/indented code and literals."""

    def render(self, prose):
        lines, fence = [], None
        for line in prose.body.split("\n"):
            match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
            if match:
                marker = match[1][0]
                if fence is None:
                    fence = (marker, len(match[1]))
                elif marker == fence[0] and len(match[1]) >= fence[1]:
                    fence = None
                lines.append(line)
                continue
            if fence is None:
                line = re.sub(r"^( {0,3})- ", r"\1* ", line)
                if (
                    re.match(r"^ {0,3}\* ", line)
                    and len(lines) >= 2
                    and lines[-1] == ""
                    and re.match(r"^ {0,3}\* ", lines[-2])
                ):
                    lines.pop()
            lines.append(line)
        return RenderedProse(prose.title, "\n".join(lines), "linear-markdown-v1")
