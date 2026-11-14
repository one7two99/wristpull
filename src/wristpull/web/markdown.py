"""A small Markdown-to-HTML renderer for the coach's answers (stdlib only, everything escaped).

Supports what the coach writes: ``#``/``##``/``###`` headings, paragraphs, ``-``/``*`` bullet lists,
``1.`` numbered lists, pipe tables with a ``---`` separator row, **bold**, *italic*, `code` and
line breaks inside a paragraph. Everything else is plain text; raw HTML never passes through.
"""

from __future__ import annotations

import html
import re

from markupsafe import Markup

_BULLET = re.compile(r"^\s*[-*•]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")


def inline(text: str) -> str:
    """Escape, then mark up code, bold and italic."""
    out = html.escape(text, quote=False)
    out = _CODE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)
    out = _ITALIC.sub(lambda m: f"<em>{m.group(1)}</em>", out)
    return out


def _cells(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [c.strip() for c in inner.split("|")]


def _is_table_row(line: str) -> bool:
    return line.strip().startswith("|") or line.count("|") >= 2


def render(markdown: str) -> Markup:
    """The answer as safe HTML (``Markup``): headings become ``<h3>``/``<h4>`` under the page's own headings."""
    lines = markdown.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        heading = _HEADING.match(stripped)
        if heading:
            level = 3 if len(heading.group(1)) <= 2 else 4  # "#" and "##" -> h3, deeper -> h4
            out.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
            i += 1
            continue
        if _is_table_row(stripped) and i + 1 < n and _SEPARATOR.match(lines[i + 1]):
            head = _cells(stripped)
            i += 2
            rows: list[list[str]] = []
            while i < n and lines[i].strip() and _is_table_row(lines[i]):
                rows.append(_cells(lines[i]))
                i += 1
            width = len(head)
            body = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in (r + [""] * width)[:width]) + "</tr>" for r in rows)
            out.append("<div class=\"table-box\"><table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + f"</tr></thead><tbody>{body}</tbody></table></div>")
            continue
        for pattern, tag in ((_BULLET, "ul"), (_NUMBERED, "ol")):
            if pattern.match(line):
                items: list[str] = []
                while i < n and pattern.match(lines[i]):
                    items.append(pattern.match(lines[i]).group(1))  # type: ignore[union-attr]
                    i += 1
                    while i < n and lines[i].strip() and not pattern.match(lines[i]) and not _HEADING.match(lines[i].strip()) and not _BULLET.match(lines[i]) and not _NUMBERED.match(lines[i]):
                        items[-1] += " " + lines[i].strip()  # a wrapped item continues on the next line
                        i += 1
                out.append(f"<{tag}>" + "".join(f"<li>{inline(item)}</li>" for item in items) + f"</{tag}>")
                break
        else:
            para: list[str] = []
            while i < n and lines[i].strip() and not _HEADING.match(lines[i].strip()) and not _BULLET.match(lines[i]) and not _NUMBERED.match(lines[i]) and not (_is_table_row(lines[i]) and i + 1 < n and _SEPARATOR.match(lines[i + 1])):
                para.append(inline(lines[i].strip()))
                i += 1
            out.append("<p>" + "<br>".join(para) + "</p>")
    return Markup("\n".join(out))
