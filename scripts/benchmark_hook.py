"""Benchmark the installed hook without recording prompt-derived data."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import subprocess
import tempfile
import time
from pathlib import Path

SAFE_INPUT = b'{"hook_event_name":"UserPromptSubmit","prompt":"Explain merge sort."}'
BLOCK_INPUT = (
    b'{"hook_event_name":"UserPromptSubmit","prompt":"Contact alice@example.com"}'
)
# One turn's final assistant text at the top of the range shim will meet.
STOP_INPUT = json.dumps(
    {
        "hook_event_name": "Stop",
        "session_id": "00000000-0000-4000-8000-000000000001",
        "stop_hook_active": False,
        "last_assistant_message": (
            "The quick brown fox jumps over the lazy dog. " * 1_500
        ),
    }
).encode()
HOOK_COMMAND = ("-I", "-B", "-m", "shim_cli.hook")
HOOK_TIMEOUT_SECONDS = 35
COPY_INSTRUCTION = "Copy and paste this as your next prompt:"
READ_INSTRUCTION = "Read this file and use its contents as my prompt: "


def percentile(samples: list[float], fraction: float) -> float:
    if not samples:
        raise ValueError("samples are required")
    return sorted(samples)[math.ceil(len(samples) * fraction) - 1]


def summary(samples: list[float]) -> dict[str, float]:
    return {
        "p50_ms": round(percentile(samples, 0.50), 3),
        "p95_ms": round(percentile(samples, 0.95), 3),
        "max_ms": round(max(samples), 3),
    }


def _valid_block(output: bytes, temporary: Path) -> bool:
    try:
        document = json.loads(output)
        lines = document["reason"].splitlines()
        path = Path(lines[2].removeprefix(READ_INSTRUCTION))
        return (
            document["decision"] == "block"
            and lines[:2] == ["shim blocked this prompt: EMAIL (1).", COPY_INSTRUCTION]
            and len(lines) == 3
            and lines[2].startswith(READ_INSTRUCTION)
            and path.parent == temporary
            and path.read_bytes() == b"Contact <EMAIL_1>"
        )
    except (AttributeError, IndexError, KeyError, OSError, TypeError, ValueError):
        return False


def run_hook(
    python: Path,
    payload: bytes,
    expected: bytes | None,
    raw: bytes,
    environment: dict[str, str],
) -> float:
    started = time.perf_counter()
    try:
        result = subprocess.run(
            (str(python), *HOOK_COMMAND),
            input=payload,
            capture_output=True,
            check=False,
            env=environment,
            timeout=HOOK_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("hook benchmark could not run") from error
    elapsed_ms = (time.perf_counter() - started) * 1_000
    matches = (
        result.stdout == expected
        if expected is not None
        else _valid_block(result.stdout, Path(environment["TMPDIR"]))
    )
    if (
        result.returncode != 0
        or not matches
        or result.stderr
        or (raw and raw in result.stdout + result.stderr)
    ):
        raise RuntimeError("hook benchmark contract failed")
    return elapsed_ms


def installed_python_version(python: Path) -> str:
    try:
        result = subprocess.run(
            (str(python), "-c", "import platform; print(platform.python_version())"),
            capture_output=True,
            check=False,
            encoding="utf-8",
            timeout=HOOK_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("installed Python could not report its version") from error
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("installed Python could not report its version")
    return result.stdout.strip()


def benchmark(python: Path, samples_per_fixture: int) -> dict[str, object]:
    if samples_per_fixture < 1:
        raise ValueError("samples must be positive")
    safe_samples: list[float] = []
    block_samples: list[float] = []
    stop_samples: list[float] = []
    with tempfile.TemporaryDirectory(prefix="shim-guard-benchmark-") as directory:
        temporary = Path(directory).resolve()
        config = temporary / "config.toml"
        config.write_text('[mode]\nuser-prompt = "enforce"\n', encoding="utf-8")
        config.chmod(0o600)
        environment = os.environ.copy()
        environment["SHIM_GUARD_CONFIG"] = str(config)
        environment["TMPDIR"] = str(temporary)
        for _ in range(samples_per_fixture):
            safe_samples.append(run_hook(python, SAFE_INPUT, b"", b"", environment))
            block_samples.append(
                run_hook(
                    python,
                    BLOCK_INPUT,
                    None,
                    b"alice@example.com",
                    environment,
                )
            )
            stop_samples.append(
                run_hook(python, STOP_INPUT, b"", b"quick brown fox", environment)
            )
    timings = {
        "safe": summary(safe_samples),
        "block": summary(block_samples),
        "stop": summary(stop_samples),
    }
    return {
        "schema_version": 1,
        "sample_counts": {
            "safe": len(safe_samples),
            "block": len(block_samples),
            "stop": len(stop_samples),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "python": installed_python_version(python),
        "timings_ms": timings,
    }


def self_check() -> None:
    samples = [float(value) for value in range(1, 21)]
    assert percentile(samples, 0.50) == 10.0
    assert percentile(samples, 0.95) == 19.0
    assert summary(samples)["max_ms"] == 20.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "python", nargs="?", type=Path, help="Installed Python executable"
    )
    parser.add_argument("--samples-per-fixture", type=int, default=20)
    parser.add_argument("--output", type=Path, help="Write JSON to this path")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if args.python is None:
        parser.error("python is required unless --self-check is used")
    try:
        result = benchmark(args.python, args.samples_per_fixture)
    except (RuntimeError, ValueError):
        raise SystemExit("hook benchmark failed") from None
    output = json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n"
    if args.output is None:
        print(output, end="")
    else:
        args.output.write_text(output, encoding="utf-8")


if __name__ == "__main__":
    main()
