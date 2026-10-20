from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from shim_cli.guard import analyze, evaluate

ADDRESS = "jane.doe@example.com"


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("/work/x@example.com/notes.txt", "/work/<EMAIL_1>/notes.txt"),
        (
            f"/Users/u/Library/CloudStorage/GoogleDrive-{ADDRESS}/plan.txt",
            "/Users/u/Library/CloudStorage/<EMAIL_1>/plan.txt",
        ),
        (f"https://host/u/{ADDRESS}", "https://host/u/<EMAIL_1>"),
        (
            f"https://api.example.com/v1/users/{ADDRESS}",
            "https://api.example.com/v1/users/<EMAIL_1>",
        ),
        (f"https://example.com/u?{ADDRESS}", "https://example.com/u?<EMAIL_1>"),
        (f"?e={ADDRESS}&f=1", "?e=<EMAIL_1>&f=1"),
        (f"/search?ref&{ADDRESS}", "/search?ref&<EMAIL_1>"),
        (f"https://example.com/page#{ADDRESS}", "https://example.com/page#<EMAIL_1>"),
        (f"#{ADDRESS}", "#<EMAIL_1>"),
        ("https://host/u/jane[at]example.com", "https://host/u/<EMAIL_1>"),
    ),
)
def test_an_address_after_a_path_or_url_separator_masks_only_the_address(
    text: str, expected: str
) -> None:
    """The local part accepted `/`, `?`, `#` and `&`, so a URL host and path or a
    whole folder prefix vanished into one placeholder: a WebFetch URL became
    `https://<EMAIL_1>` and a Read path `/<EMAIL_1>/notes.txt`."""
    assert evaluate(text).redacted_text == expected


def test_the_same_address_in_two_paths_is_found_as_the_same_value() -> None:
    """A path prefix swallowed into the span made one address two different
    values. Each span is now the address alone, so per-event numbering (D4)
    gives both one placeholder."""
    log = (
        f"GET /api/v2/users/{ADDRESS}/orders 200\n"
        f"GET https://shop.example.com/u/{ADDRESS} 404"
    )

    found = [log[item.start : item.end] for item in analyze(log)]

    assert found == [ADDRESS, ADDRESS]
    assert evaluate(log).redacted_text == (
        "GET /api/v2/users/<EMAIL_1>/orders 200\n"
        "GET https://shop.example.com/u/<EMAIL_1> 404"
    )


@pytest.mark.parametrize(
    "text",
    (
        "git clone git@github.com:org/repo.git",
        "git remote add origin git@gitlab.com:group/sub/repo.git",
        "git push deploy@example.com:~/site/app.git main",
    ),
)
def test_a_git_remote_is_not_an_address(text: str) -> None:
    assert evaluate(text).redacted_text == text


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        (f"Contact {ADDRESS}: she knows", "Contact <EMAIL_1>: she knows"),
        (f"owner: {ADDRESS}:", "owner: <EMAIL_1>:"),
        (f"{ADDRESS}:hunter2", "<EMAIL_1>:hunter2"),
        (f"{ADDRESS}:8080", "<EMAIL_1>:8080"),
        (f"a/{ADDRESS}", "a/<EMAIL_1>"),
        (f"mailto:{ADDRESS}", "mailto:<EMAIL_1>"),
        (f"{ADDRESS}:hunter/2", "<EMAIL_1>:hunter/2"),
        (f"combo: {ADDRESS}:Pa55/w0rd", "combo: <EMAIL_1>:Pa55/w0rd"),
        (f"{ADDRESS}:8080/api", "<EMAIL_1>:8080/api"),
        (f"{ADDRESS}:pass/digit", "<EMAIL_1>:pass/digit"),
        (f"Contact {ADDRESS}:/", "Contact <EMAIL_1>:/"),
        (f"{ADDRESS}:/home/jane", "<EMAIL_1>:/home/jane"),
        (f"{ADDRESS}:~/x", "<EMAIL_1>:~/x"),
        (
            f"{ADDRESS},john.smith@corp-example.com:x/y",
            "<EMAIL_1>,<EMAIL_2>:x/y",
        ),
        (
            "PASSWORD=s3cr3t jane@example.com:x/y",
            "PASSWORD=<SECRET_1> <EMAIL_1>:x/y",
        ),
        (
            "scp build.tar deploy@example.com:/var/www/",
            "scp build.tar <EMAIL_1>:/var/www/",
        ),
        (
            "rsync -a out/ deploy@example.com:~/site/",
            "rsync -a out/ <EMAIL_1>:~/site/",
        ),
    ),
)
def test_an_address_before_a_colon_stays_masked_unless_a_git_remote_follows(
    text: str, expected: str
) -> None:
    """An `email:password` combo line keeps its address masked even when the
    password holds a `/`: only a path ending in `.git` marks a remote. An scp
    or rsync target to a host that looks like an address stays masked: noise,
    never a missed address."""
    assert evaluate(text).redacted_text == expected


def test_an_mcp_call_with_a_combo_line_is_still_refused(tmp_path: Path) -> None:
    """D1: an MCP input carrying an address is refused under enforce. A `/` in
    the password must not let the address through."""
    home = tmp_path / "home"
    temporary = tmp_path / "tmp"
    home.mkdir()
    temporary.mkdir()
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SHIM_", "XDG_", "CLAUDE_"))
    }
    environment.update(HOME=str(home), TMPDIR=str(temporary))
    event = {
        "session_id": "u09",
        "hook_event_name": "PreToolUse",
        "tool_name": "mcp__slack__send",
        "tool_input": {"text": f"login {ADDRESS}:hunter/2"},
    }

    result = subprocess.run(
        (sys.executable, "-I", "-B", "-m", "shim_cli.hook", "claude"),
        input=json.dumps(event).encode(),
        capture_output=True,
        env=environment,
        check=False,
        timeout=60,
    )

    assert (result.returncode, result.stderr) == (0, b"")
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"].startswith(
        "shim: blocked EMAIL (1) in mcp__slack__send."
    )
