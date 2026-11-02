"""Literal, cell-aware terminal text with no task-controlled terminal commands."""

import re

from rich.console import Console
from rich.text import Text

CONTROL_SEQUENCE = re.compile(r"\x1b\][\s\S]*?(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")
BIDI = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def literal(value, *, multiline=False):
    text = CONTROL_SEQUENCE.sub("", str(value or "")).replace("\r\n", "\n")
    safe = "".join(
        c
        for c in text
        if c not in BIDI and (c == "\n" or c == "\t" or (ord(c) >= 32 and not 127 <= ord(c) < 160))
    ).expandtabs(4)
    return safe if multiline else safe.replace("\n", " ")


def rendered_lines(value, width):
    width = max(1, width)
    console = Console(width=width, force_terminal=False, color_system=None)
    result = []
    for line in literal(value, multiline=True).split("\n"):
        result.extend(str(piece) for piece in Text(line).wrap(console, width, overflow="fold"))
    return result or [""]
