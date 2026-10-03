"""Markdown → VK ``format_data`` renderer.

A VK community message can carry, alongside its plain ``message`` text, a
``format_data`` payload::

    {"version": 1, "items": [{"type": "bold", "offset": 0, "length": 4},
                             {"type": "url", "offset": 5, "length": 9, "url": "https://…"}]}

with ``type`` in ``bold`` / ``italic`` / ``underline`` / ``strike`` / ``url`` /
``mention``.  Everything VK cannot render is left as plain text (VK has no
monospace and no headings), so the agent's markdown degrades gracefully instead
of leaking asterisks.

Offsets are counted in **UTF-16 code units** — the unit the ``markdown-to-vk``
pipeline used by ``openclaw-vk`` emits and the one VK's own web client indexes
with.  For Cyrillic/Latin text that is identical to ``len()``; only astral
characters (emoji) differ, which is exactly why the helper below exists.

The renderer runs in two stages so text and spans can never drift apart:
``parse_markdown`` produces a line/segment model with no offsets, then
``render_chunks`` splits it at the VK message limit and computes offsets *per
chunk* afterwards.  A naive "render then truncate" order would slice spans.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

# (text, styles, url) — styles is a frozenset of format item types.
Segment = Tuple[str, frozenset, Optional[str]]

DEFAULT_LIMIT = 4000  # VK community messages are capped at 4096 characters.

# Item types VK's community ``messages.send`` actually honours. This is an allowlist on purpose:
# VK does NOT error on an unsupported item type, it silently drops the WHOLE format_data object —
# a single ``strike`` item turned a correctly-marked-up reply into unformatted plain text
# (verified live against a real community: bold+italic survived, strike-only stored ``null``).
VK_SAFE_ITEM_TYPES = frozenset({"bold", "italic", "url"})


def u16_len(text: str) -> int:
    """Length of ``text`` in UTF-16 code units (VK's offset unit)."""
    return len(text.encode("utf-16-le")) // 2


def _closes(text: str, index: int, marker: str, *, intraword_ok: bool) -> bool:
    """Whether ``marker`` at ``index`` is a valid closer for a span of the same kind."""
    after = text[index + len(marker): index + len(marker) + 1]
    if not intraword_ok and marker.startswith("_"):
        # CommonMark: ``_`` emphasis cannot start or end inside a word.
        if text[index - 1: index].isalnum() and after and after.isalnum():
            return False
    return True


def _opens(text: str, index: int, marker: str, *, intraword_ok: bool) -> bool:
    if not intraword_ok and marker.startswith("_"):
        if text[index - 1: index].isalnum():
            return False
    return True


def _find_closer(text: str, start: int, marker: str, *, intraword_ok: bool) -> int:
    """Index of the closing ``marker`` at/after ``start``, or -1.

    A closing RUN longer than the marker is consumed from its end (CommonMark behaviour):
    ``**жирный с *курсивом***`` closes the italic with the first ``*`` and the bold with the last
    two. Without this the inner emphasis leaked its markers into the delivered text.
    """
    char = marker[0]
    pos = text.find(marker, start)
    while pos != -1:
        if _closes(text, pos, marker, intraword_ok=intraword_ok):
            run_start, run_end = pos, pos
            while run_start > start and text[run_start - 1] == char:
                run_start -= 1
            while run_end < len(text) and text[run_end] == char:
                run_end += 1
            if run_end - run_start > len(marker):
                return run_end - len(marker)
            return pos
        pos = text.find(marker, pos + 1)
    return -1


def _escaped(text: str, index: int) -> bool:
    """True when ``text[index]`` is backslash-escaped."""
    backslashes = 0
    i = index - 1
    while i >= 0 and text[i] == "\\":
        backslashes += 1
        i -= 1
    return backslashes % 2 == 1


def _scan_inline(text: str, styles: frozenset) -> List[Segment]:
    """Split one line of markdown into styled segments."""
    out: List[Segment] = []
    buf: List[str] = []

    def flush(extra_styles: frozenset = frozenset(), url: Optional[str] = None) -> None:
        if not buf:
            return
        out.append(("".join(buf), styles | extra_styles, url))
        buf.clear()

    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            buf.append(text[i + 1])
            i += 2
            continue
        # [text](url) and ![alt](url)
        if ch in "[!" and (ch == "[" or text[i: i + 2] == "!["):
            open_at = i + 1 if ch == "[" else i + 1
            close = text.find("]", open_at)
            if close != -1 and text[close + 1: close + 2] == "(":
                end = text.find(")", close + 2)
                if end != -1:
                    label = text[open_at + (1 if ch == "!" else 0): close] or text[close + 2:end]
                    url = text[close + 2:end].strip()
                    flush()
                    out.append((label, styles | {"url"}, url or None))
                    i = end + 1
                    continue
        if ch == "<" and text[i + 1: i + 5] in ("http",):
            end = text.find(">", i)
            if end != -1:
                flush()
                out.append((text[i + 1: end], styles | {"url"}, text[i + 1: end]))
                i = end + 1
                continue
        if ch == "`":
            fence = "```" if text[i: i + 3] == "```" else "`"
            close = text.find(fence, i + len(fence))
            if close != -1:
                flush()
                out.append((text[i + len(fence): close], styles, None))
                i = close + len(fence)
                continue
            buf.append(ch)
            i += 1
            continue
        matched = False
        # ``~~text~~`` markers are consumed but carry NO style: VK silently voids a whole
        # format_data object that contains a ``strike`` item (see VK_SAFE_ITEM_TYPES).
        for marker, style, intraword in (
            ("***", "bold,italic", True), ("**", "bold", True), ("__", "bold", False),
            ("~~", "", True), ("*", "italic", True), ("_", "italic", False),
        ):
            if not text.startswith(marker, i) or not _opens(text, i, marker, intraword_ok=intraword):
                continue
            body_start = i + len(marker)
            close = _find_closer(text, body_start, marker, intraword_ok=intraword)
            if close == -1 or close == body_start:
                continue
            inner = text[body_start: close]
            if not inner.strip():
                continue
            flush()
            added = frozenset(s for s in style.split(",") if s)
            out.extend(_scan_inline(inner, styles | added))
            i = close + len(marker)
            matched = True
            break
        if matched:
            continue
        buf.append(ch)
        i += 1
    flush()
    return out


def _parse_line(raw: str) -> List[Segment]:
    """One markdown line → styled segments (block-level markup normalised)."""
    line = raw.rstrip()
    stripped = line.lstrip()
    indent = line[: len(line) - len(stripped)]
    if stripped.startswith("```"):
        return []
    if set(stripped) and stripped.strip("-*_ ") == "":  # horizontal rule
        return [(indent + "─" * 24, frozenset(), None)]
    if stripped == ">" or stripped.startswith("> "):
        # VK has no quote styling: render the quoted text as italic so it still reads as a quote
        # instead of arriving with a literal ">" prefix.
        return _scan_inline(stripped[2:] if stripped.startswith("> ") else "", frozenset({"italic"}))
    heading = 0
    while heading < len(stripped) and stripped[heading] == "#":
        heading += 1
    if heading and stripped[heading: heading + 1] == " ":
        return _scan_inline(stripped[heading + 1:], frozenset({"bold"}))
    for bullet, replacement in (("- ", "• "), ("* ", "• "), ("+ ", "• ")):
        if stripped.startswith(bullet):
            return [(indent + replacement, frozenset(), None), *_scan_inline(stripped[2:], frozenset())]
    return [(indent, frozenset(), None)][: 0] + _scan_inline(line, frozenset())


def parse_markdown(markdown: str) -> List[List[Segment]]:
    """Markdown → lines of styled segments (blank lines preserved as empty lists)."""
    out: List[List[Segment]] = []
    in_fence = False
    for raw in (markdown or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            out.append([(raw, frozenset(), None)] if raw else [])
            continue
        out.append(_parse_line(raw))
    while out and not out[-1]:
        out.pop()
    return out


def _line_len(line: Sequence[Segment]) -> int:
    return sum(u16_len(seg[0]) for seg in line)


def _items_for(segments: Sequence[Segment]) -> Tuple[str, Optional[Dict]]:
    """Segments → (text, format_data) with UTF-16 offsets."""
    text_parts: List[str] = []
    items: List[Dict] = []
    offset = 0
    for seg_text, styles, url in segments:
        length = u16_len(seg_text)
        if length and styles:
            for style in ("bold", "italic", "url"):
                if style not in styles or style not in VK_SAFE_ITEM_TYPES:
                    continue
                if style == "url":
                    if url:
                        items.append({"type": "url", "offset": offset, "length": length, "url": url})
                else:
                    items.append({"type": style, "offset": offset, "length": length})
        text_parts.append(seg_text)
        offset += length
    text = "".join(text_parts)
    return text, ({"version": 1, "items": items} if items else None)


def _split_oversized(line: Sequence[Segment], limit: int) -> List[List[Segment]]:
    """Split a single over-limit line into pieces, keeping segments intact."""
    pieces: List[List[Segment]] = []
    current: List[Segment] = []
    used = 0
    for seg in line:
        seg_text, styles, url = seg
        while u16_len(seg_text) > limit:
            head, seg_text = seg_text[: limit], seg_text[limit:]
            if head:
                current.append((head, styles, url))
            pieces.append(current)
            current, used = [], 0
        if used + u16_len(seg_text) > limit and current:
            pieces.append(current)
            current, used = [], 0
        current.append((seg_text, styles, url))
        used += u16_len(seg_text)
    if current:
        pieces.append(current)
    return pieces


def _split_table_row(line: str) -> List[str]:
    from gateway.platforms.helpers import split_markdown_table_row
    return split_markdown_table_row(line)


def _table_separator_re():
    from gateway.platforms.helpers import TABLE_SEPARATOR_RE
    return TABLE_SEPARATOR_RE


def _render_table_block(block: Sequence[str]) -> str:
    """One GFM table → headings + bullets (the shape Telegram/Discord users see)."""
    headers = _split_table_row(block[0])
    row_label_table = len(headers) > 0 and len(_split_table_row(block[2])) == len(headers) + 1
    groups: List[str] = []
    for row in block[2:]:
        cells = _split_table_row(row)
        if not cells:
            continue
        label, values = cells[0], cells[1:]
        # Header names line up with the VALUE cells, not with the row label: shift by one unless the
        # table has its own label column. A blank header (the common "key|value" table a model emits
        # as ``|||`` / ``|---|---|``) simply drops out, leaving a bare bullet under the heading.
        value_headers = headers if row_label_table else headers[1:]
        bullets = [f"• {header.strip()}: {value}" if header.strip() else f"• {value}"
                   for header, value in zip(value_headers, values) if value.strip()]
        groups.append("\n".join([f"**{label}**", *bullets]) if label.strip() else "\n".join(bullets))
    return "\n\n".join(groups) if groups else "\n".join(block)


def _strip_quote_prefix(line: str) -> str:
    """The line as the table detector must see it: ``> |a|b|`` is still a table row.

    A model quoting a table in a blockquote used to slip past the detector (the line starts with ``>``,
    not ``|``), so the whole grid — pipes and dash rows — reached the phone verbatim.
    """
    stripped = line.lstrip()
    if stripped.startswith(">"):
        stripped = stripped[1:]
        if stripped.startswith(" "):
            stripped = stripped[1:]
        return stripped
    return line


def render_tables(text: str) -> str:
    """Rewrite GFM pipe tables as heading + bullets; fenced code and stray pipes are left alone.

    VK renders no tables at all — a raw ``| a | b |`` grid arrives as pipes and, on a phone, wraps
    into unreadable soup (live report: ``|||`` / ``|---|---|`` / ``|Статус|раскатка завершена|``). Discord
    solves this with the framework's ``convert_table_to_bullets``; this is the same idea with the
    header-label shift handled explicitly, then fed to the normal markdown renderer so headings still
    arrive bold. Quoted rows (``> |…|``) are converted too, losing the quote marker along with the grid.
    """
    if "|" not in text:
        return text
    separator = _table_separator_re()
    lines = text.replace("\r\n", "\n").split("\n")
    out: List[str] = []
    in_fence = False
    index = 0
    while index < len(lines):
        line = lines[index]
        is_fence = line.lstrip().startswith("```")
        in_fence ^= is_fence
        next_line = _strip_quote_prefix(lines[index + 1]) if index + 1 < len(lines) else ""
        if (not in_fence and not is_fence and "|" in _strip_quote_prefix(line) and separator.match(next_line)):
            end = index + 2
            while end < len(lines) and "|" in _strip_quote_prefix(lines[end]).strip():
                end += 1
            out.append(_render_table_block([_strip_quote_prefix(item) for item in lines[index:end]]))
            index = end
            continue
        out.append(line)
        index += 1
    return "\n".join(out)


def render_chunks(markdown: str, limit: int = DEFAULT_LIMIT) -> List[Tuple[str, Optional[Dict]]]:
    """Markdown → VK-ready ``(text, format_data)`` chunks, each within ``limit`` characters."""
    limit = max(200, int(limit))
    lines = parse_markdown(render_tables(markdown))
    if not lines:
        return [("", None)]
    blocks: List[List[Segment]] = []
    current: List[Segment] = []
    used = 0
    for line in lines:
        pieces = _split_oversized(line, limit) if _line_len(line) > limit else [list(line)]
        for piece in pieces:
            piece_len = _line_len(piece)
            if current and used + piece_len + 1 > limit:
                blocks.append(current)
                current, used = [], 0
            if current:
                current.append(("\n", frozenset(), None))
                used += 1
            current.extend(piece)
            used += piece_len
    if current:
        blocks.append(current)
    chunks: List[Tuple[str, Optional[Dict]]] = []
    for block in blocks or [[]]:
        text, fmt = _items_for(block)
        if text.strip() or not chunks:
            chunks.append((text, fmt))
    return chunks


def to_plain(markdown: str, limit: int = DEFAULT_LIMIT) -> str:
    """Markdown → plain text (captions, logs, keyword matching)."""
    return "\n".join(text for text, _ in render_chunks(markdown or "", limit)).strip()
