from __future__ import annotations

import time

import pytest

from shim_cli.events import html_text
from shim_cli.events.html_text import HEADER, HTML_MIN_CHARS, to_text

# Markup that is dropped, so a page is well over twice its text.
PADDING = "<div class='spacer' data-role='layout'></div>\n" * 60


def _page(body: str, head: str = "<title>Synthetic page</title>") -> str:
    return (
        f"<!DOCTYPE html>\n<html lang='en'><head>{head}</head>\n<body>\n"
        f"{body}\n{PADDING}</body></html>\n"
    )


def _body(page: str) -> list[str]:
    converted = to_text(page)
    assert converted is not page, "the page was left unchanged"
    lines = converted.split("\n")
    assert lines[0] == HEADER
    return lines[1:]


def test_the_title_comes_first_and_scripts_styles_and_head_are_dropped() -> None:
    page = _page(
        "<script>var hidden = '<div>';</script><style>p{color:red}</style>"
        "<noscript>enable scripts</noscript><template><p>later</p></template>"
        "<svg><title>icon</title><text>drawn</text></svg>"
        "<iframe src='x'>framed</iframe><p>Visible text</p>",
        head="<meta charset='utf-8'><title>Ops   handbook</title>"
        "<link rel='stylesheet' href='a.css'><script>head()</script>",
    )

    assert _body(page) == ["Ops handbook", "Visible text"]


def test_comments_doctype_and_unknown_tags_leave_only_their_text() -> None:
    page = _page(
        "<!-- a comment --><?php echo 1; ?><p>One <span class='x'>two</span> "
        "<b>three</b></p>"
    )

    assert _body(page)[1:] == ["One two three"]


def test_headings_lists_links_and_images_keep_their_meaning() -> None:
    page = _page(
        "<h1>Deploy</h1><h3>Rollback</h3>"
        "<ul><li>first</li><li>see <a href='https://example.com/docs'>the docs</a>"
        "</li></ul><a>no address</a><a href=''>empty address</a>"
        "<img src='a.png' alt='  release   diagram '><img src='b.png' alt=''>"
    )

    assert _body(page)[1:] == [
        "# Deploy",
        "### Rollback",
        "- first",
        "- see the docs (https://example.com/docs)",
        "no addressempty address[image: release diagram]",
    ]


def test_indented_markup_leaves_one_space_between_words() -> None:
    page = _page(
        "<ul>\n  <li>\n    first\n  </li>\n</ul>\n<h2>  Title  </h2>\n"
        "<p>Hello <b> bold </b> world</p>"
    )

    assert _body(page)[1:] == ["- first", "## Title", "Hello bold world"]


def test_table_cells_are_separated_within_a_row() -> None:
    page = _page(
        "<table><tr><th>Name</th><th>Value</th></tr>"
        "<tr><td>alpha</td><td>1</td></tr><tr><td>beta</td><td>2</td></tr></table>"
    )

    assert _body(page)[1:] == ["Name | Value", "alpha | 1", "beta | 2"]


def test_whitespace_collapses_outside_pre_and_is_kept_inside() -> None:
    page = _page(
        "<p>  spaced \n\n  out\t text  </p>"
        "<pre>  keep   this\n    indent</pre><textarea>a  b</textarea>"
    )

    assert _body(page)[1:] == ["spaced out text", "  keep   this", "    indent", "a  b"]


def test_blocks_end_lines_and_blank_lines_collapse() -> None:
    page = _page("one<br><br><br><br>two<div>three</div>four<hr>five")

    assert _body(page)[1:] == ["one", "", "two", "three", "four", "five"]


def test_entities_are_decoded() -> None:
    page = _page("<p>Fish &amp; chips &lt;b&gt; &#8364;5 &eacute;t&eacute;</p>")

    assert _body(page)[1:] == ["Fish & chips <b> €5 été"]


@pytest.mark.parametrize("placeholder", ["<EMAIL_1>", "<IBAN_1:4>", "<SECRET_12>"])
def test_a_placeholder_shim_wrote_survives_as_text(placeholder: str) -> None:
    page = _page(
        f"<p>Contact {placeholder} today</p><a href='mailto:{placeholder}'>x</a>"
    )

    assert _body(page)[1:] == [
        f"Contact {placeholder} today",
        f"x (mailto:{placeholder})",
    ]


def test_exactly_the_threshold_converts_and_one_less_does_not() -> None:
    page = _page("<p>Body</p>")
    trimmed = page.replace(PADDING, "", 1)
    filler = HTML_MIN_CHARS - len(trimmed)
    at = trimmed.replace(
        "</body>", "<i></i>" * (filler // 7) + " " * (filler % 7) + "</body>"
    )
    below = at.replace("<i></i>", "", 1) + " " * 6

    assert len(at) == HTML_MIN_CHARS
    assert len(below) == HTML_MIN_CHARS - 1
    assert to_text(at).startswith(HEADER)
    assert to_text(below) is below


def test_a_page_that_is_mostly_text_is_left_unchanged() -> None:
    page = (
        "<!doctype html><html><body><p>"
        + "plain words that carry the page " * 100
        + "</p></body></html>"
    )

    assert to_text(page) is page


@pytest.mark.parametrize(
    "text",
    [
        "<div>" + "fragment, not a whole page " * 100 + "</div>",
        "Plain output that mentions <html> later on. " * 60,
        '{"html": "<!doctype html>"}' + " " * 3000,
        "﻿<!doctype html>" + PADDING,
    ],
)
def test_fragments_and_other_text_are_left_unchanged(text: str) -> None:
    assert len(text) >= HTML_MIN_CHARS
    assert to_text(text) is text


def test_leading_whitespace_and_case_do_not_matter() -> None:
    page = "\n  \t<!DocType HTML>" + _page("<p>Body</p>").split("\n", 1)[1]

    assert to_text(page).split("\n")[1:] == ["Synthetic page", "Body"]


@pytest.mark.parametrize(
    "body",
    [
        "<p>unclosed <b>bold <i>italic</p> after</b></i>",
        "<div><span>misnested</div></span><li>no list",
        "<table><td>no row</td><td>cells</td></table><a href='x'>open link",
    ],
)
def test_malformed_markup_gives_the_same_text_every_time(body: str) -> None:
    page = _page(body)

    first = to_text(page)
    assert first.startswith(HEADER)
    assert to_text(page) == first
    assert to_text(first) == first


@pytest.mark.parametrize(
    "tail",
    [
        '<a x="never closed',
        "<!-- never closed",
        "<script>never closed",
        "<!never closed",
    ],
)
def test_a_page_that_does_not_parse_to_its_end_is_left_unchanged(tail: str) -> None:
    page = _page("<p>Body</p>") + tail

    assert to_text(page) is page


@pytest.mark.parametrize("tag", ["script", "svg", "iframe", "noscript", "template"])
def test_an_unclosed_dropped_element_leaves_the_page_unchanged(tag: str) -> None:
    # Everything after it would be dropped without a word.
    page = _page(f"<p>Body</p><{tag}><p>the rest of the page</p>")

    assert to_text(page) is page


def test_hostile_pages_finish_in_linear_time_and_unchanged() -> None:
    head = "<!doctype html>"
    pages = [
        head + '<a x="' * 20_000,
        head + "<a" * 50_000,
        head + "<!--" * 25_000,
        head + "</" * 50_000,
        head + "<?" * 50_000,
        head + "<" * 100_000,
        head + "<li>x" * 20_000,
    ]

    started = time.perf_counter()
    results = [to_text(page) for page in pages]
    elapsed = time.perf_counter() - started

    # `close()` took minutes on the first of these; `feed()` alone is linear.
    assert elapsed < 10
    assert all(
        result is page for result, page in zip(results[:5], pages[:5], strict=True)
    )


def _seconds(page: str) -> float:
    started = time.perf_counter()
    to_text(page)
    return time.perf_counter() - started


@pytest.mark.parametrize(
    ("opening", "closing"),
    [
        ("<svg>", "</iframe>"),
        ("<iframe>", "</svg>"),
        ("<noscript><template>", "</template></noscript>"),
        ("<svg><iframe><noscript><template>", "</template></noscript></iframe></svg>"),
        ("<template>", "</noscript>"),
    ],
)
def test_nested_dropped_elements_take_linear_time(opening: str, closing: str) -> None:
    count = 1_000_000 // len(opening + closing)
    hostile = "<!doctype html><html><body>" + opening * count + closing * count
    # The same page with ordinary tags in place of the dropped ones.
    benign = hostile
    for name in ("noscript", "template", "iframe", "svg"):
        benign = benign.replace(name, "div")

    # Each end tag used to search every element still open: 17.9 s at 560 KB.
    assert _seconds(hostile) < 3 * _seconds(benign) + 0.2


def test_the_hook_deadline_is_not_swallowed(monkeypatch) -> None:
    def expired(self, data):
        raise TimeoutError("shim hook deadline exceeded")

    monkeypatch.setattr(html_text._Text, "handle_data", expired)

    with pytest.raises(TimeoutError):
        to_text(_page("<p>Body</p>"))


def test_an_error_inside_the_parser_leaves_the_text_unchanged(monkeypatch) -> None:
    def broken(self, data):
        raise AssertionError("synthetic parser failure")

    monkeypatch.setattr(html_text._Text, "handle_data", broken)
    page = _page("<p>Body</p>")

    assert to_text(page) is page


def test_a_converted_page_is_converted_once() -> None:
    page = _page("<h2>Second</h2><p>text</p>")

    once = to_text(page)

    assert once.startswith(HEADER + "\n")
    assert to_text(once) == once
    assert html_text.HTML_TEXT_VERSION == 1
