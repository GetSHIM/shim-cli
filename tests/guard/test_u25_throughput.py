"""The phone scan reads number-dense text inside the hook's deadline, and
what it remembers between calls stays bounded however much it reads."""

from __future__ import annotations

import gc
import importlib
import random
import sys
import time
import tracemalloc

import phonenumbers

from shim_cli.guard import evaluate
from shim_cli.guard.recognizers import _phone_spans, _phone_walk

_OWNER = "owner,ops@example.com,AKIAIOSFODNN7EXAMPLE\n"


def test_a_megabyte_of_the_reported_number_dense_line_is_read_in_time() -> None:
    """`- 0 - 0 - 0` repeated on one line, the shape 1.2.2's notes give: on
    the build they measured, 900 KB of it reached the deadline and was
    withheld. 9c988c2 made it fast; this guards that it stays so."""
    hook = importlib.import_module("shim_cli.hook")
    text = _OWNER + "- 0 " * ((1_000_000 - len(_OWNER)) // 4)

    started = time.process_time()
    decision = evaluate(text)
    spent = time.process_time() - started

    assert decision.partial is False
    assert decision.redacted_text.startswith("owner,<EMAIL_1>,<SECRET_1>\n")
    assert spent < hook.HOOK_DEADLINE_SECONDS / 2


def test_the_phone_scan_holds_a_bounded_amount_however_much_it_reads() -> None:
    """A long-lived process (`shim watch`) kept about 20 MB of candidates,
    parts and their outcomes. Numbers no region reads whole, so every region
    tries every part, and candidates written beside wide characters (four bytes
    a character in the memos' keys) fill every memo; what stays after them is
    the memos' bound, which is pinned."""
    from shim_cli.guard.recognizers import _PHONE_MEMO_BYTES

    pick = random.Random(7)
    numbers = "".join(
        f"({pick.randrange(200, 999)}) {pick.randrange(200, 999)}-"
        f"{pick.randrange(10_000):04d}\n"
        for _ in range(22_000)
    )
    wide = "".join(
        "📞 "
        + " ".join(str(pick.randrange(10**17, 10**18)) for _ in range(21))
        + " 📞\n"
        for _ in range(600)
    )
    _phone_walk.cache_clear()
    phonenumbers.PhoneMetadata.load_all()
    _phone_spans("(212) 555-0143 1 - 2/3–4 5")
    gc.collect()

    tracemalloc.start()
    try:
        _phone_spans(numbers)
        _phone_spans(wide)
        gc.collect()
        held = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()

    assert _PHONE_MEMO_BYTES <= 10 * 2**20
    assert held <= _PHONE_MEMO_BYTES


def test_a_memo_never_holds_more_than_its_budget() -> None:
    """A key of wide characters takes four bytes a character: the memo counts
    what its entries take, not how many characters they have, so it never
    holds more than its budget, at its fullest either."""
    from shim_cli.guard.recognizers import _Memo

    budget = 2**20
    gc.collect()
    tracemalloc.start()
    try:
        memo = _Memo(budget)
        fullest = 0
        for row in range(30_000):
            key = ("📞" if row % 2 else "") + f"{row:06d}" * 12
            value = key[-12:]
            memo.keep(key, value, sys.getsizeof(value))
            fullest = max(fullest, tracemalloc.get_traced_memory()[0])
    finally:
        tracemalloc.stop()

    assert memo
    assert fullest <= budget
