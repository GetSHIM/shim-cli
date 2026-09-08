from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULES = ("shim_cli.hook", "shim_guard.hook")
ENFORCE_PROMPT = (
    'enabled_entities = ["EMAIL", "PHONE", "CREDIT_CARD", "IBAN", "IP_ADDRESS", '
    '"MAC_ADDRESS", "US_SSN", "TR_NATIONAL_ID", "TR_VKN", "SECRET", "DB_URI"]\n'
    "\n[mode]\n"
    'user-prompt = "enforce"\n'
)
# The block response names a 0600 redaction file whose suffix is random.
_REDACTION = re.compile(rb"shim-redacted-[^\"]+")


def _payload(client: str, prompt: str) -> bytes:
    if client == "copilot":
        event: dict[str, object] = {
            "sessionId": "session",
            "timestamp": 1,
            "cwd": "/workspace",
            "prompt": prompt,
            "transformedPrompt": prompt,
        }
    else:
        event = {
            "session_id": "thr_test",
            "transcript_path": None,
            "cwd": "/workspace",
            "hook_event_name": "UserPromptSubmit",
            "model": "gpt-5",
            "turn_id": "turn_test",
            "permission_mode": "default",
            "prompt": prompt,
        }
    return json.dumps(event, separators=(",", ":")).encode()


FIXTURES = {
    "safe": lambda client: _payload(client, "Explain merge sort."),
    "finding": lambda client: _payload(client, "Contact alice@example.com"),
    "unicode": lambda client: _payload(client, "Merhaba İstanbul 🌍 alice@example.com"),
    "malformed": lambda client: b"{not json",
    "empty": lambda client: b"",
}


def _run(module: str, client: str, raw: bytes, home: Path) -> tuple[int, bytes, bytes]:
    home.mkdir(parents=True, exist_ok=True)
    config = home / "enforce.toml"
    config.write_text(ENFORCE_PROMPT, encoding="utf-8")
    environment = os.environ.copy()
    environment["TMPDIR"] = str(home)
    environment["SHIM_CONFIG"] = str(config)
    result = subprocess.run(
        (sys.executable, "-I", "-B", "-m", module, client),
        input=raw,
        capture_output=True,
        cwd=ROOT,
        env=environment,
        check=False,
        timeout=60,
    )
    scrub = str(home).encode()
    return (
        result.returncode,
        _REDACTION.sub(b"shim-redacted-X", result.stdout.replace(scrub, b"<tmp>")),
        _REDACTION.sub(b"shim-redacted-X", result.stderr.replace(scrub, b"<tmp>")),
    )


@pytest.mark.parametrize("client", ("claude", "codex", "copilot"))
@pytest.mark.parametrize("fixture", tuple(FIXTURES))
def test_the_old_module_answers_exactly_like_the_new_one(
    client: str, fixture: str, tmp_path: Path
) -> None:
    raw = FIXTURES[fixture](client)
    new = _run("shim_cli.hook", client, raw, tmp_path / "new")
    old = _run("shim_guard.hook", client, raw, tmp_path / "old")

    assert old == new


def test_the_old_package_exposes_the_hook_entry_point() -> None:
    source = (
        "import shim_cli.hook, shim_guard, shim_guard.hook;"
        "print(shim_guard.hook.main is shim_cli.hook.main,"
        " shim_guard.__version__ == shim_cli.__version__)"
    )
    result = subprocess.run(
        (sys.executable, "-I", "-B", "-c", source),
        capture_output=True,
        cwd=ROOT,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.split() == [b"True", b"True"]


def _modules(name: str) -> set[str]:
    source = (
        f"import {name}; import json, sys;"
        "sys.stdout.write(json.dumps(sorted(sys.modules)))"
    )
    result = subprocess.run(
        (sys.executable, "-I", "-B", "-c", source),
        capture_output=True,
        cwd=ROOT,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode()
    # The alias package and module are themselves the only permitted difference.
    return {
        module
        for module in json.loads(result.stdout)
        if module != "shim_cli" and not module.startswith("shim_guard")
    }


def test_the_old_module_imports_nothing_the_new_one_does_not() -> None:
    assert _modules("shim_guard.hook") - _modules("shim_cli.hook") == set()


@pytest.mark.parametrize("module", MODULES)
def test_neither_module_pulls_the_cli_or_the_proxy(module: str) -> None:
    imported = _modules(module)

    assert not [name for name in imported if name.startswith("shim_cli.watch")]
    assert not [name for name in imported if name in ("typer", "rich")]
