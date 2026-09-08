"""PRD-18: read the captured sessions and state what they show.

    python scripts/probe/summarize_study.py --root /tmp/study

The hypothesis under test is that the context diet does not break the
provider's prompt cache. Two things in the series decide it:

  - **the shape.** After the first request of a session, each later request
    should read a cache roughly the size of the previous request's total input
    and create only the tokens the new tool result added. A broken prefix looks
    the opposite way round: a large creation and a small or zero read.
  - **the difference.** With the diet on, the created tokens per request should
    be smaller, because the tool result they carry is smaller. Nothing else
    about the series should change.

Sessions run back to back share the provider's cache, so a later session can
open with a read of a prefix an earlier one created. That is reported rather
than corrected: it is a property of the account, not of the diet.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _rows(series: list[dict]) -> list[dict]:
    rows = []
    for index, usage in enumerate(series, start=1):
        created = usage["cache_creation_input_tokens"]
        read = usage["cache_read_input_tokens"]
        rows.append(
            {
                "request": index,
                "request_bytes": usage["request_bytes"],
                "uncached": usage["input_tokens"],
                "created": created,
                "read": read,
                "total_input": usage["input_tokens"] + created + read,
                "stop_reason": usage.get("stop_reason", ""),
            }
        )
    return rows


def _turn(rows: list[dict]) -> list[dict]:
    """The conversation that grows, which is the only thing a prefix can cache.

    Claude Code issues a last request that is *smaller* than the one before it
    — 87,000 bytes shorter in one session, 133,000 in another — and reads a
    static prefix of about 117,000 tokens rather than the conversation's. It is
    a separate call that shares only the system prompt and tool definitions.
    Counting it reports a broken prefix that never broke, so the run is cut at
    the first request that does not grow. How many were dropped is reported,
    because a rule that silently discards data is not a measurement.
    """
    kept = rows[:1]
    for previous, row in zip(rows, rows[1:], strict=False):
        if row["request_bytes"] < previous["request_bytes"]:
            break
        kept.append(row)
    return kept


def _monotonic(rows: list[dict]) -> bool:
    """Does every request read at least as much cache as the one before it?"""
    reads = [row["read"] for row in rows]
    return all(
        later >= earlier for earlier, later in zip(reads, reads[1:], strict=False)
    )


def _cold_start(rows: list[dict]) -> int:
    """Leading requests that read nothing, because there was nothing to read."""
    count = 0
    for row in rows:
        if row["read"]:
            break
        count += 1
    return count


def _prefix_preserved(rows: list[dict]) -> bool:
    """Once the cache exists, does each read pick up what the last one paid for?

    Allowing a small shortfall: the client sets its own cache breakpoints, so a
    read can trail the previous total by the tail that fell after the last one.
    A session that opens cold is not a broken prefix, so the leading requests
    that read nothing are excluded rather than counted as failures.
    """
    warm = rows[_cold_start(rows) :]
    for earlier, later in zip(warm, warm[1:], strict=False):
        if later["read"] < earlier["total_input"] * 0.9:
            return False
    return True


def describe(label: str, series: list[dict]) -> dict:
    every = _rows(series)
    rows = _turn(every)
    if not rows:
        return {"label": label, "requests": 0}
    after_first = rows[1:]
    return {
        "label": label,
        "requests": len(rows),
        "trailing_requests": len(every) - len(rows),
        "rows": rows,
        "created_total": sum(row["created"] for row in rows),
        "read_total": sum(row["read"] for row in rows),
        "created_after_first": sum(row["created"] for row in after_first),
        "median_created_after_first": (
            sorted(row["created"] for row in after_first)[len(after_first) // 2]
            if after_first
            else 0
        ),
        "final_request_bytes": rows[-1]["request_bytes"],
        "opened_warm": rows[0]["read"] > 0,
        "cold_start_requests": _cold_start(rows),
        "reads_never_fall": _monotonic(rows),
        "prefix_preserved": _prefix_preserved(rows),
    }


def read_series(root: Path) -> dict:
    """Straight from the saved reports, so a rerun of this script sees every
    field the proxy recorded rather than whatever `series.json` was written
    with at the time."""
    series = {}
    for path in sorted((root / "reports").glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        report = document.get("report")
        if not report:
            continue
        series[document["label"]] = [
            {
                "request_bytes": exchange["request_bytes"],
                "stop_reason": exchange["stop_reason"],
                **exchange["usage"],
            }
            for exchange in report["exchanges"]
        ]
    return series


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    arguments = parser.parse_args(argv)

    series = read_series(arguments.root)
    summaries = [describe(label, rows) for label, rows in series.items()]

    for summary in summaries:
        print(f"\n=== {summary['label']}  ({summary['requests']} requests)")
        if not summary["requests"]:
            continue
        print(
            f"{'#':>3} {'req bytes':>11} {'uncached':>9} "
            f"{'created':>9} {'read':>10} {'total in':>10}"
        )
        for row in summary["rows"]:
            print(
                f"{row['request']:>3} {row['request_bytes']:>11,} "
                f"{row['uncached']:>9,} {row['created']:>9,} "
                f"{row['read']:>10,} {row['total_input']:>10,}"
            )
        print(
            f"    cold-start requests: {summary['cold_start_requests']}   "
            f"reads never fall: {summary['reads_never_fall']}   "
            f"prefix preserved: {summary['prefix_preserved']}"
        )
        print(
            f"    created after the first request: "
            f"{summary['created_after_first']:,} "
            f"(median {summary['median_created_after_first']:,})"
        )

    print("\n--- verdict inputs ---")
    intact = [s for s in summaries if s["requests"] > 1]
    if intact:
        print(
            "prefix preserved in every multi-request session: "
            f"{all(s['prefix_preserved'] for s in intact)}"
        )
        print(
            "cache reads never fall within a session:         "
            f"{all(s['reads_never_fall'] for s in intact)}"
        )
    for name in ("diet-on", "diet-off"):
        group = [s for s in intact if s["label"].startswith(name)]
        if group:
            print(
                f"{name}: created-after-first per session "
                + ", ".join(f"{s['created_after_first']:,}" for s in group)
            )
    (arguments.root / "summary.json").write_text(
        json.dumps(summaries, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\nwritten to {arguments.root / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
