"""Turn a captured study run into the fixture `tests/probe` asserts against.

    python scripts/probe/build_study_fixture.py --root /tmp/study \\
        --out tests/fixtures/probe/study-2026-09-08.json

Only numbers and labels cross over. The ledger records the study reads carry a
scrubbed target path, and the workspace they name is a temporary directory on
whoever ran it, so no path is copied into the fixture at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from study import _record  # noqa: E402
from summarize_study import describe  # noqa: E402

from shim_cli.events.diet import DEFAULT_TRANSFORMS, shrink  # noqa: E402

KEEP = (
    "label",
    "requests",
    "cold_start_requests",
    "reads_never_fall",
    "prefix_preserved",
    "created_after_first",
    "median_created_after_first",
)


def _dataset(records: int) -> str:
    document = {
        "dataset": "data-1",
        "records": [_record(1000 + index) for index in range(records)],
    }
    return json.dumps(document, indent=2) + "\n"


def diet_numbers(records: int, truncated_records: int, cut: int) -> dict:
    text = _dataset(records)
    compacted, applied = shrink(text, DEFAULT_TRANSFORMS)
    truncated = _dataset(truncated_records)[:cut]
    _, none_applied = shrink(truncated, DEFAULT_TRANSFORMS)
    return {
        "records_per_file": records,
        "file_bytes": len(text),
        "compacted_bytes": len(compacted),
        "transforms": list(applied),
        "truncated_source_records": truncated_records,
        "client_truncates_at_bytes": cut,
        "transforms_on_truncated": list(none_applied),
    }


def ledger_summary(root: Path, name: str) -> dict:
    path = root / f"{name}-records.json"
    if not path.is_file():
        return {}
    records = json.loads(path.read_text(encoding="utf-8"))
    results = [
        item
        for item in records
        if item.get("event") == "PostToolUse" and (item.get("in_bytes") or 0) > 1_000
    ]
    return {
        "records": len(records),
        "large_tool_results": len(results),
        "transformed": sum(1 for item in results if item.get("transforms")),
        "in_bytes": [item["in_bytes"] for item in results],
        "out_bytes": [item["out_bytes"] for item in results],
        "repeated_targets": _repeats(records),
    }


def _repeats(records: list[dict]) -> int:
    """R3, hook side: how many tool results repeat a target already seen."""
    seen: set[tuple] = set()
    repeats = 0
    for item in records:
        if item.get("event") != "PostToolUse":
            continue
        key = (item.get("tool_name"), item.get("target"))
        if not key[1]:
            continue
        if key in seen:
            repeats += 1
        else:
            seen.add(key)
    return repeats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--records", type=int, default=50)
    parser.add_argument("--truncated-records", type=int, default=120)
    parser.add_argument("--truncated-at", type=int, default=34_388)
    parser.add_argument("--client", default="Claude Code")
    parser.add_argument("--model", default="claude-sonnet-5")
    parser.add_argument("--recorded", default="2026-09-08")
    arguments = parser.parse_args(argv)

    series = json.loads((arguments.root / "series.json").read_text())
    sessions = [
        {key: value for key, value in describe(label, rows).items() if key in KEEP}
        for label, rows in series.items()
    ]
    document = {
        "recorded": arguments.recorded,
        "client": arguments.client,
        "model": arguments.model,
        "sessions": sessions,
        "diet": diet_numbers(
            arguments.records, arguments.truncated_records, arguments.truncated_at
        ),
        "ledger": {
            name: ledger_summary(arguments.root, name)
            for name in ("ledger-on", "ledger-off")
        },
    }
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"written to {arguments.out}")
    for session in sessions:
        print(
            f"  {session['label']:>16} requests={session['requests']:>2} "
            f"prefix_preserved={session['prefix_preserved']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
