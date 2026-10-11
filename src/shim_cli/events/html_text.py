"""A whole HTML page in a tool result, as text. Opt-in: markup is dropped."""

from __future__ import annotations

import re
from html.parser import HTMLParser

from shim_cli.guard.entities import PLACEHOLDER

HTML_TEXT_VERSION = 1
HTML_MIN_CHARS = 2_048
HTML_MAX_RATIO = 0.5
HEADER = (
    "[shim: converted from HTML to text; markup removed. Save the page to a file "
    "and Read it to see the markup.]"
)

_DROPPED = frozenset(
    ("script", "style", "noscript", "template", "svg", "iframe", "head")
)
_BLOCKS = frozenset(
    (
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
        "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table",
        "tr", "ul",
    )
)  # fmt: skip
_VERBATIM = frozenset(("pre", "textarea"))
_PAGE_STARTS = ("<!doctype html", "<html")
_WHITESPACE = re.compile(r"\s+")
_BLANK_LINES = re.compile(r"\n{3,}")


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list = []
        self.title: list = []
        # Open dropped elements by tag, as depths: a stack searched on every end
        # tag made nested hostile markup quadratic.
        self.dropped: dict = {}
        self.verbatim = 0
        self.links: list = []
        self.cells = 0
        self.in_title = False

    def _end_line(self) -> None:
        if self.out and not self.out[-1].endswith("\n"):
            self.out.append("\n")

    def _write(self, text: str) -> None:
        if self.verbatim:
            self.out.append(text)
            return
        text = _WHITESPACE.sub(" ", text)
        if not self.out or self.out[-1].endswith((" ", "\n")):
            text = text.lstrip(" ")
        if text:
            self.out.append(text)

    def handle_starttag(self, tag, attrs) -> None:
        if tag == "body" and "head" in self.dropped:
            self.dropped.clear()
        if tag == "title" and not self.title and self.dropped.keys() <= {"head"}:
            self.in_title = True
            return
        if tag in _DROPPED:
            self.dropped[tag] = self.dropped.get(tag, 0) + 1
            return
        if self.dropped:
            return
        attributes = dict(attrs)
        if tag in _BLOCKS:
            self._end_line()
        if tag == "br":
            self.out.append("\n")
        elif tag in _VERBATIM:
            self.verbatim += 1
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("- ")
        elif tag == "a":
            self.links.append(attributes.get("href") or "")
        elif tag == "img" and (attributes.get("alt") or "").strip():
            self._write(f"[image: {' '.join(attributes['alt'].split())}]")
        elif tag in ("table", "tr"):
            self.cells = 0
        elif tag in ("td", "th"):
            if self.cells:
                self.out.append(" | ")
            self.cells += 1

    def handle_endtag(self, tag) -> None:
        if tag == "title" and self.in_title:
            self.in_title = False
            return
        if tag in _DROPPED:
            if self.dropped.get(tag) == 1:
                del self.dropped[tag]
            elif tag in self.dropped:
                self.dropped[tag] -= 1
            return
        if self.dropped:
            return
        if tag in _VERBATIM and self.verbatim:
            self.verbatim -= 1
        elif tag == "a" and self.links:
            href = self.links.pop()
            if href:
                self._write(f" ({href})")
        if tag in _BLOCKS:
            self._end_line()

    def handle_data(self, data) -> None:
        if self.in_title:
            self.title.append(data)
        elif not self.dropped:
            self._write(data)


def to_text(text: str) -> str:
    """The page as text, or `text` itself when it does not parse to its end,
    leaves a dropped element open, is not a whole page or would not be at most
    half its size. Raises only the hook's `TimeoutError`."""
    if len(text) < HTML_MIN_CHARS or not text.lstrip()[:14].lower().startswith(
        _PAGE_STARTS
    ):
        return text
    try:
        parser = _Text()
        # A placeholder shim wrote is text, not a tag: escape it so the parser
        # decodes it back to the same characters.
        parser.feed(PLACEHOLDER.sub(lambda m: "&lt;" + m.group(0)[1:-1] + "&gt;", text))
        # `close()` re-scans an unfinished tag, comment or quote once per `<`
        # after it, quadratic on a hostile page; `feed()` alone stops there.
        # An unclosed `<svg>` would drop the rest of the page without a word.
        if parser.dropped or parser.rawdata.strip():
            return text
        title = " ".join("".join(parser.title).split())
        lines = [line.rstrip() for line in "".join(parser.out).split("\n")]
        body = _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip("\n")
    except TimeoutError:
        raise
    except Exception:  # the parser's own errors differ by Python version
        return text
    if not title and not body:
        return text
    result = "\n".join(part for part in (HEADER, title, body) if part)
    return result if len(result) <= HTML_MAX_RATIO * len(text) else text


__all__ = ["HEADER", "HTML_MAX_RATIO", "HTML_MIN_CHARS", "HTML_TEXT_VERSION", "to_text"]
