"""One event, one numbering: a value keeps its placeholder in every field of
a tool event and in the page the HTML diet makes of it, and two values never
share one."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from html import unescape

import pytest

from shim_cli.clients.claude.tool_events import TOOL_EVENTS
from shim_cli.events.html_text import HEADER
from shim_cli.events.payload import inspect, mask
from shim_cli.events.pipeline import process
from shim_cli.guard import DEFAULT_ENTITIES, evaluate
from shim_cli.guard.entities import compile_custom
from shim_cli.policy import ENFORCE

JANE = "jane.doe@example.com"
JOHN = "john.roe@example.com"
HTML = ("json", "html")
PADDING = "<div class='row'><span class='cell'>ok</span></div>\n" * 60


def _page(body: str) -> str:
    return (
        "<!doctype html><html><head><title>Billing</title></head><body>"
        f"{body}\n{PADDING}</body></html>"
    )


def _bash(stdout: str, stderr: str) -> bytes:
    return json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "session_id": "s",
            "tool_name": "Bash",
            "tool_input": {"command": "./notify.sh"},
            "tool_response": {
                "stdout": stdout,
                "stderr": stderr,
                "interrupted": False,
                "isImage": False,
            },
        }
    ).encode()


def test_two_values_in_two_fields_get_two_numbers() -> None:
    result, findings, _changed = mask(
        {"stdout": f"sent to {JANE}", "stderr": f"bounce from {JOHN}"}, evaluate
    )

    assert result == {"stdout": "sent to <EMAIL_1>", "stderr": "bounce from <EMAIL_2>"}
    assert len(findings) == 2


def test_one_value_keeps_its_number_across_fields_and_within_one() -> None:
    result, _findings, _changed = mask(
        {
            "owner": JANE,
            "billing": JOHN,
            "note": f"{JOHN} replaces {JANE}; ask {JANE}",
        },
        evaluate,
    )

    assert result == {
        "owner": "<EMAIL_1>",
        "billing": "<EMAIL_2>",
        "note": "<EMAIL_2> replaces <EMAIL_1>; ask <EMAIL_1>",
    }


def test_each_event_numbers_from_one() -> None:
    first, _, _ = mask({"stdout": JANE}, evaluate)
    second, _, _ = mask({"stdout": JOHN}, evaluate)

    assert (first, second) == ({"stdout": "<EMAIL_1>"}, {"stdout": "<EMAIL_1>"})


def test_a_page_numbers_its_values_once_for_all_of_them() -> None:
    page = _page(
        f"<p>Sales: {JANE}</p><p>Billing: billing&#64;example&#46;com</p>"
        f"<p>Again: {JANE}</p>"
    )

    result = inspect(
        {"content": [{"text": page}, {"text": f"cc {JOHN}"}]}, evaluate, HTML
    )

    text = result.value["content"][0]["text"]
    assert result.transforms == ("html",)
    assert "Sales: <EMAIL_1>\nBilling: <EMAIL_2>\nAgain: <EMAIL_1>" in text
    assert result.value["content"][1]["text"] == "cc <EMAIL_3>"


def test_an_iban_split_across_two_spans_is_masked_whole() -> None:
    page = _page(
        "<p>Refunds are paid to <span>GB82 WEST 1234</span>"
        "<span> 5698 7654 32</span>.</p>"
    )

    result = inspect({"text": page}, evaluate, HTML)

    text = result.value["text"]
    assert "Refunds are paid to <IBAN_1>." in text
    assert not re.search(r"1234|5698|7654|GB82", text)
    assert [decision.counts for _path, decision in result.findings] == [(("IBAN", 1),)]


def test_a_page_that_joins_one_value_still_reads_another_whole() -> None:
    # The phone is joined to its label and found only in the markup; the
    # IBAN is cut in two by tags and found only on the page. Both are masked.
    page = _page(
        "<p>Refunds are paid to <span>GB82 WEST 1234</span>"
        "<span> 5698 7654 32</span>.</p>"
        "<p><span>05320101234</span><span>Mobile</span></p>"
    )

    result = inspect({"text": page}, evaluate, HTML)

    text = result.value["text"]
    assert "\nRefunds are paid to <IBAN_1>.\n<PHONE_2>Mobile\n" in text
    assert not re.search(r"1234|5698|7654|GB82|0532", text)
    assert [decision.counts for _path, decision in result.findings] == [
        (("IBAN", 1), ("PHONE", 1))
    ]


@pytest.mark.parametrize(
    ("markup", "shown"),
    [
        (
            "<span>Email</span><span>jane.doe@example.com</span>"
            "<span>Phone</span><span>0532 010 12 34</span>"
            "<span>IBAN</span><span>TR33 0006 1005 1978 6457 8413 26</span>"
            "<span>Primary</span>",
            "Email<EMAIL_1>Phone<PHONE_1>IBAN<IBAN_1>Primary",
        ),
        ("ID <span>10000000146</span><span>7</span>", "ID <TR_NATIONAL_ID_1>7"),
        ("<b>4242 4242 4242 4242</b><b>1</b>", "<CREDIT_CARD_1>1"),
        ("<span>05320101234</span><span>Mobile</span>", "<PHONE_1>Mobile"),
        ("<span>10.20.30.40</span><span>5</span>", "<IP_ADDRESS_1>5"),
        # Joined, the page reads its end and the next element as another
        # type: the two overlap, and are masked as one.
        ("<span>+90 532 010 12 34</span><span>99</span>", "<PHONE_1>"),
        # Found across a line break the page turns into a space.
        (
            "<span>TR33 0006 1005\n1978 6457 8413 26</span><span>Primary</span>",
            "<IBAN_1>Primary",
        ),
    ],
    ids=[
        "labelled-row",
        "national-id",
        "card",
        "phone",
        "ip",
        "intl-phone",
        "line-broken-iban",
    ],
)
def test_a_value_its_own_element_kept_apart_is_masked_when_the_page_joins_it(
    markup: str, shown: str
) -> None:
    page = _page(f"<p>{markup}</p>")

    result = inspect({"text": page}, evaluate, HTML)

    text = result.value["text"]
    assert text.startswith(HEADER)
    assert f"\n{shown}\n" in text
    assert result.transforms == ("html",)


@pytest.mark.parametrize(
    ("pattern", "markup", "shown"),
    [
        # Found as written, `&amp;`, and looked for as the page shows it, `&`.
        (
            "R&(?:amp;)?D-[0-9]{4}(?![0-9])",
            "<p><span>R&amp;D-7731</span><span>5</span></p>",
            "<CUSTOM_1>5",
        ),
        # Found in the markup itself, which no page shows.
        (
            "<td>ACME-[0-9]{4}</td>",
            "<table><tr><td>Ticket</td><td>ACME-7731</td></tr></table>",
            "Ticket<CUSTOM_1>",
        ),
    ],
    ids=["entity", "markup"],
)
def test_a_custom_value_found_in_the_markup_is_masked_on_the_page(
    pattern: str, markup: str, shown: str
) -> None:
    custom = compile_custom([{"name": "ORDER", "pattern": pattern}])

    def scan(text, numbering=None):
        return evaluate(text, DEFAULT_ENTITIES, custom, numbering=numbering)

    result = inspect({"text": _page(markup)}, scan, HTML)

    assert f"\n{shown}\n" in result.value["text"]
    assert result.transforms == ("html",)


def test_a_value_that_runs_across_tags_is_masked_in_the_markup() -> None:
    custom = compile_custom([{"name": "REF", "pattern": r"ref:\S+"}])

    def scan(text, numbering=None):
        return evaluate(text, DEFAULT_ENTITIES, custom, numbering=numbering)

    # The value starts and ends in scripts the page drops, so nothing marks
    # where its middle lands: it is masked in the markup, before conversion.
    page = _page("<script>a='ref:</script><b>ACME-7731</b><script>'; b=1</script>")

    result = inspect({"text": page}, scan, HTML)

    assert result.value["text"].startswith(HEADER)
    assert "ACME" not in result.value["text"]


SPLIT_IBAN = (
    "<p>Refunds are paid to <span>GB82 WEST 1234</span><span> 5698 7654 32</span>.</p>"
)


@pytest.mark.parametrize(
    ("beside", "shown"),
    [
        (
            "<p>Login: admin / password: <code>Hunter2xyz!</code></p>",
            "Login: admin / password: <SECRET_1>",
        ),
        ("<p>password=<b>hunter2xyz</b></p>", "password=<SECRET_1>"),
        (
            "<p>token=<span>ghp_0123456789abcdefghijABCDEFGHIJ012345</span></p>",
            "token=<SECRET_1>",
        ),
    ],
    ids=["code", "bold", "span"],
)
def test_a_value_across_tags_leaves_the_page_read_whole(
    beside: str, shown: str
) -> None:
    # The secret's value runs on across its closing tags, so it is masked in
    # the markup; the rest of the page is still converted, then read.
    result = inspect({"text": _page(SPLIT_IBAN + beside)}, evaluate, HTML)

    text = result.value["text"]
    assert result.transforms == ("html",)
    assert f"\nRefunds are paid to <IBAN_1>.\n{shown}\n" in text
    assert not re.search(r"1234|5698|7654|GB82|[Hh]unter2|ghp_", text)
    assert [sorted(decision.counts) for _path, decision in result.findings] == [
        [("IBAN", 1), ("SECRET", 1)]
    ]


@pytest.mark.parametrize(
    "held",
    [
        "<i class='material-icons'>&#xE000;</i>",
        "<i>\ue000\ue0010\ue002</i>",
        # Shaped like a mark around a value: it is the page's text, not one.
        "<i>&#xE000;0&#xE001;abc&#xE002;</i>",
        "<i>&#xE000;7&#xE001;abc&#xE002;</i>",
    ],
    ids=["icon-entity", "literal", "entity-index-in-range", "entity-index-past-end"],
)
def test_a_page_holding_private_use_characters_is_read_whole(held: str) -> None:
    page = _page(f"{SPLIT_IBAN}<p>Sales: {JANE}</p><p>{held}</p>")

    result = inspect({"text": page}, evaluate, HTML)

    text = result.value["text"]
    assert result.transforms == ("html",)
    assert "\nRefunds are paid to <IBAN_1>.\nSales: <EMAIL_1>\n" in text
    assert f"\n{unescape(re.sub('<[^>]*>', '', held))}\n" in text
    assert not re.search(r"1234|5698|7654|GB82", text)
    assert [sorted(decision.counts) for _path, decision in result.findings] == [
        [("EMAIL", 1), ("IBAN", 1)]
    ]


def test_a_page_whose_markup_scan_is_partial_is_withheld() -> None:
    def partial_on_markup(text, numbering=None):
        decision = evaluate(text, numbering=numbering)
        if text.startswith("<!doctype"):
            return replace(decision, partial=True)
        return decision

    result = inspect({"text": _page(f"<p>Sales: {JANE}</p>")}, partial_on_markup, HTML)

    assert result.status == "partial"
    assert result.value is None


def test_a_page_that_joins_a_value_still_masks_one_only_its_text_shows() -> None:
    page = _page(
        "<p>Billing: billing&#64;example&#46;com</p>"
        "<p><span>05320101234</span><span>Mobile</span></p>"
    )

    result = inspect({"text": page}, evaluate, HTML)

    text = result.value["text"]
    assert "\nBilling: <EMAIL_1>\n<PHONE_1>Mobile\n" in text
    assert [decision.counts for _path, decision in result.findings] == [
        (("EMAIL", 1), ("PHONE", 1))
    ]


def test_a_value_the_page_drops_is_not_counted() -> None:
    page = _page(f"<script>var owner = '{JOHN}';</script><p>Sales: {JANE}</p>")

    result = inspect(
        {"content": [{"text": page}, {"text": "cc kim.poe@example.com"}]},
        evaluate,
        HTML,
    )

    # The markup is scanned first, so the script's address took the first
    # number although the model never reads it: numbers can skip, never repeat.
    assert "\nSales: <EMAIL_2>\n" in result.value["content"][0]["text"]
    assert JOHN not in result.value["content"][0]["text"]
    assert result.value["content"][1]["text"] == "cc <EMAIL_3>"
    assert [decision.counts for _path, decision in result.findings] == [
        (("EMAIL", 1),),
        (("EMAIL", 1),),
    ]


def test_a_value_the_page_joins_numbers_on_into_the_next_field() -> None:
    page = _page("<p><span>05320101234</span><span>Mobile</span></p>")

    result = inspect(
        {"content": [{"text": page}, {"text": "call 05320109876 or 05320101234"}]},
        evaluate,
        HTML,
    )

    assert "\n<PHONE_1>Mobile\n" in result.value["content"][0]["text"]
    assert result.value["content"][1]["text"] == "call <PHONE_2> or <PHONE_1>"


def test_a_page_whose_scan_is_partial_is_masked_as_it_came() -> None:
    def partial_on_pages(text, numbering=None):
        decision = evaluate(text, numbering=numbering)
        if text.startswith(HEADER):
            return replace(decision, partial=True)
        return decision

    page = _page(f"<p>Sales: {JANE}</p><p>Owner: {JOHN}</p>")

    result = inspect(
        {"a": f"owner {JOHN}", "b": page, "c": f"cc {JANE}"}, partial_on_pages, HTML
    )

    assert result.transforms == ()
    assert result.value["a"] == "owner <EMAIL_1>"
    assert result.value["b"] == page.replace(JANE, "<EMAIL_2>").replace(
        JOHN, "<EMAIL_1>"
    )
    assert result.value["c"] == "cc <EMAIL_2>"


def test_a_placeholder_is_not_read_again_by_a_custom_pattern_on_a_page() -> None:
    custom = compile_custom([{"name": "TICKET", "pattern": "[A-Z]+_[0-9]+"}])

    def scan(text, numbering=None):
        return evaluate(text, DEFAULT_ENTITIES, custom, numbering=numbering)

    page = _page(f"<p>Owner: {JANE}</p><p>See OPS_12</p>")

    result = inspect({"text": page}, scan, HTML)

    text = result.value["text"]
    assert "Owner: <EMAIL_1>\nSee <CUSTOM_1>" in text
    [(_path, decision)] = result.findings
    assert decision.counts == (("EMAIL", 1), ("CUSTOM", 1))


def test_a_masked_read_keeps_its_line_numbers() -> None:
    key = "\n".join(["0123456789abcdef" * 4] * 3)
    content = (
        "KEY = '''\n-----BEGIN RSA PRIVATE KEY-----\n"
        f"{key}\n-----END RSA PRIVATE KEY-----\n'''\nraise ValueError\n"
    )

    result = inspect({"file": {"content": content}}, evaluate)

    masked = result.value["file"]["content"]
    assert masked.count("\n") == content.count("\n")
    assert masked.split("\n").index("raise ValueError") == 7


def test_the_pipeline_numbers_every_field_of_an_entity_scoped_event() -> None:
    outcome = process(
        TOOL_EVENTS["PostToolUse"],
        _bash(f"sent to {JANE}\n", f"bounce from {JOHN}\nretry {JANE}\n"),
        lambda direction, tool: ENFORCE,
        evaluate,
        (),
        lambda tool, event: ("EMAIL",),
    )

    emitted = json.loads(outcome.output)["hookSpecificOutput"]["updatedToolOutput"]
    assert emitted["stdout"] == "sent to <EMAIL_1>\n"
    assert emitted["stderr"] == "bounce from <EMAIL_2>\nretry <EMAIL_1>\n"
    assert outcome.record.entities == (("EMAIL", 3),)


def test_the_hook_numbers_every_field_of_one_event() -> None:
    from shim_cli.hook import _output

    output = _output(_bash(f"sent to {JANE}\n", f"bounce from {JOHN}\n"), "claude")

    emitted = json.loads(output)["hookSpecificOutput"]["updatedToolOutput"]
    assert emitted["stdout"] == "sent to <EMAIL_1>\n"
    assert emitted["stderr"] == "bounce from <EMAIL_2>\n"


def test_a_joined_value_keeps_its_placeholder_after_a_masked_block() -> None:
    def scan(text, numbering=None):
        return evaluate(
            text, DEFAULT_ENTITIES, (), {"PHONE": 4, "IBAN": 4}, numbering=numbering
        )

    key = "\n".join(["0123456789abcdef" * 4] * 3)
    page = _page(
        f"<pre>-----BEGIN RSA PRIVATE KEY-----\n{key}\n"
        "-----END RSA PRIVATE KEY-----\n</pre>"
        "<p><span>05320101234</span><span>Mobile</span></p>"
        "<p>Pay <span>GB82 WEST 1234</span><span> 5698 7654 32</span></p>"
    )

    result = inspect({"text": page}, scan, HTML)

    text = result.value["text"]
    assert "\n<SECRET_1>\n\n\n\n\n<PHONE_1:1234>Mobile\nPay <IBAN_1:5432>\n" in text
    assert "0123456789abcdef" not in text
