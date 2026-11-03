"""Narrow preapproval HTTPS link spelling; not a general Markdown renderer."""

import re
from urllib.parse import urlsplit

from markdown_it import MarkdownIt

from ai_task_manager.domains.work.errors import ValidationError


def literal_blocks(text):
    """Map CommonMark code blocks to original source offsets, without serialization."""
    tokens = MarkdownIt("commonmark", {"maxNesting": 32}).parse(text)
    if any(token.level >= 31 for token in tokens):
        raise ValidationError("Markdown nesting exceeds the supported boundary")
    # CommonMark normalizes CR/CRLF internally. Keep offsets into the original
    # string so parsing never changes literal bytes or line endings.
    starts = [0, *(match.end() for match in re.finditer(r"\r\n|\r|\n", text))]
    starts.append(len(text))
    blocks, fence_starts = {}, set()
    for token in tokens:
        if token.type not in {"fence", "code_block"}:
            continue
        start, end = (starts[line] for line in token.map)
        blocks[start] = end
        if token.type == "fence":
            fence_starts.add(start)
    return blocks, fence_starts


def _refuse():
    raise ValidationError("Ambiguous HTTPS link syntax; use an explicit Markdown link or code")


def _line_end(text, start):
    end = text.find("\n", start)
    return len(text) if end < 0 else end + 1


def _code_end(text, start):
    run = re.match(r"`+", text[start:])[0]
    close = re.search(r"(?<!`)" + re.escape(run) + r"(?!`)", text[start + len(run) :])
    if close is None:
        if "https://" in text[start:]:
            _refuse()
        return len(text)
    return start + len(run) + close.end()


def _balanced_end(text, start, opening, closing):
    depth, i = 1, start + 1
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if opening == "[" and char == "`":
            i = _code_end(text, i)
            continue
        if opening == "(" and char == "<":
            end = text.find(">", i + 1)
            if end < 0:
                _refuse()
            i = end + 1
            continue
        if opening == "(" and char in "\"'" and text[i - 1].isspace():
            quote = char
            i += 1
            while i < len(text) and text[i] != quote:
                i += 2 if text[i] == "\\" else 1
            i += 1
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    _refuse()


def _url(value, *, balanced=True):
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            and not 1 <= parsed.port <= 65535
            or any(not 0x21 <= ord(c) <= 0x7E for c in value)
            or any(c in value for c in "\\[]{}*`<>\"'")
            or "~~" in value
            or re.search(r"[^A-Za-z0-9:/?#@!$&()+,;=.%_~\-]", value)
            or re.search(r"(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])", value)
            or re.search(r"%(?![0-9A-Fa-f]{2})", value)
        ):
            _refuse()
        if balanced:
            depth = 0
            for char in value:
                depth += (char == "(") - (char == ")")
                if depth < 0:
                    _refuse()
            if depth:
                _refuse()
    except ValueError:
        _refuse()
    return f"[{value}](<{value}>)"


def explicit_https_links(text, *, blocks=None):
    """Preserve protected spans and convert only the declared HTTPS prose forms."""
    if "https://" not in text:
        return text
    if blocks is None:
        blocks, _ = literal_blocks(text)
    out, i = [], 0
    while i < len(text):
        if i in blocks:
            end = blocks[i]
            out.append(text[i:end])
            i = end
            continue
        char = text[i]
        if char == "`":
            end = _code_end(text, i)
            out.append(text[i:end])
            i = end
            continue
        if char == "\\":
            if text.startswith("https://", i + 1):
                _refuse()
            out.append(text[i : i + 2])
            i += 2
            continue
        if char == "[":
            end = _balanced_end(text, i, "[", "]")
            if text[end : end + 1] in {"(", "["}:
                opening = text[end]
                end = _balanced_end(text, end, opening, ")" if opening == "(" else "]")
            elif text[end : end + 1] == ":" and re.fullmatch(
                r" {0,3}", text[text.rfind("\n", 0, i) + 1 : i]
            ):
                after = _line_end(text, end)
                # A reference destination may start on the following line.
                if not text[end + 1 : after].strip():
                    after = _line_end(text, after)
                end = after
            elif "https://" in text[i:end]:
                _refuse()
            out.append(text[i:end])
            i = end
            continue
        if char == "<":
            end = text.find(">", i + 1)
            if end < 0:
                if "https://" in text[i:]:
                    _refuse()
            elif text.startswith("https://", i + 1):
                out.append(_url(text[i + 1 : end], balanced=False))
                i = end + 1
                continue
            elif "https://" in text[i:end]:
                _refuse()
            elif end >= 0:
                out.append(text[i : end + 1])
                i = end + 1
                continue
        if text.startswith("https://", i):
            if i and (text[i - 1].isalnum() or text[i - 1] in "_\\/*~"):
                _refuse()
            end = i + len("https://")
            while end < len(text) and not text[end].isspace() and text[end] not in "<>\"'`[]{}":
                end += 1
            if end < len(text) and (
                text[end] in "`[{}"
                or (text[end] in "\"'" and end + 1 < len(text) and text[end + 1].isalnum())
            ):
                _refuse()
            value = text[i:end]
            while value and (
                value[-1] in ".,;:!?" or (value[-1] == ")" and value.count(")") > value.count("("))
            ):
                value = value[:-1]
            out.append(_url(value))
            i += len(value)
            continue
        out.append(char)
        i += 1
    return "".join(out)
