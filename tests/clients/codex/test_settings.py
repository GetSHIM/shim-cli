from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

import pytest

from shim_cli.clients.codex.settings import (
    HOOK_TIMEOUT_SECONDS,
    MAX_CONFIG_BYTES,
    MINIMUM_CODEX_VERSION,
    TESTED_CODEX_VERSION,
    add_hook,
    config_path,
    has_inline_hooks,
    hook_command,
    hook_group,
    remove_hook,
    target_path,
)


def test_codex_0149_settings_are_exact_and_deterministic(tmp_path: Path) -> None:
    interpreter = tmp_path / "venv's python"
    document = add_hook(None, interpreter)
    assert document == add_hook(None, interpreter)
    assert document.endswith(b"\n")
    assert json.loads(document) == {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "command": hook_command(interpreter),
                            "timeout": 30,
                            "type": "command",
                        }
                    ]
                }
            ]
        }
    }
    assert shlex.split(hook_command(interpreter)) == [
        str(interpreter),
        "-I",
        "-B",
        "-m",
        "shim_cli.hook",
    ]
    assert b'"matcher"' not in document
    assert b'"async"' not in document
    assert HOOK_TIMEOUT_SECONDS == 30
    assert TESTED_CODEX_VERSION == MINIMUM_CODEX_VERSION == "0.149.0"


def test_target_path_respects_injected_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ignored"))
    assert target_path(tmp_path) == tmp_path / ".codex" / "hooks.json"


def test_target_path_respects_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "custom-codex"))
    assert target_path() == tmp_path / "custom-codex" / "hooks.json"
    assert config_path() == tmp_path / "custom-codex" / "config.toml"


def test_inline_hook_detection_is_bounded_and_refuses_links(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    assert not has_inline_hooks(config)

    config.write_text('model = "gpt-5"\n')
    assert not has_inline_hooks(config)
    config.write_text("[hooks]\nUserPromptSubmit = []\n")
    assert has_inline_hooks(config)

    config.unlink()
    config.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError):
        has_inline_hooks(config)

    real = tmp_path / "real"
    real.mkdir()
    (real / "config.toml").write_text("[hooks]\nUserPromptSubmit = []\n")
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    assert has_inline_hooks(linked / "config.toml")

    exposed = tmp_path / "exposed"
    exposed.mkdir()
    exposed.chmod(0o770)
    to_exposed = tmp_path / "to-exposed"
    to_exposed.symlink_to(exposed, target_is_directory=True)
    with pytest.raises(ValueError):
        has_inline_hooks(to_exposed / "config.toml")

    fifo = tmp_path / "config.fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError):
        has_inline_hooks(fifo)


def test_inline_hook_detection_normalizes_read_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('model = "gpt-5"\n')
    real_fstat = os.fstat
    calls = 0

    def fail_after_read(descriptor: int):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic read failure")
        return real_fstat(descriptor)

    monkeypatch.setattr(os, "fstat", fail_after_read)
    with pytest.raises(ValueError, match="cannot be inspected"):
        has_inline_hooks(config)


def test_inline_hook_detection_normalizes_parser_recursion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('model = "gpt-5"\n')

    def recurse(_: str) -> None:
        raise RecursionError

    monkeypatch.setattr("shim_cli.clients.codex.settings.tomllib.loads", recurse)
    with pytest.raises(ValueError, match="cannot be inspected"):
        has_inline_hooks(config)


def test_inline_hook_detection_refuses_unsafe_ownership_and_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('model = "gpt-5"\n')
    config.chmod(0o620)
    with pytest.raises(ValueError):
        has_inline_hooks(config)

    config.chmod(0o600)
    linked = tmp_path / "linked.toml"
    os.link(config, linked)
    with pytest.raises(ValueError):
        has_inline_hooks(config)
    linked.unlink()

    monkeypatch.setattr(os, "geteuid", lambda: config.stat().st_uid + 1)
    with pytest.raises(ValueError):
        has_inline_hooks(config)


def test_hook_command_requires_absolute_interpreter() -> None:
    with pytest.raises(ValueError):
        hook_command("python3")
    with pytest.raises(ValueError):
        hook_command("/tmp/unsafe\x1b-python")


def test_shared_hook_document_preserves_unrelated_content_and_order(
    tmp_path: Path,
) -> None:
    interpreter = tmp_path / "python"
    existing = {
        "version": 1,
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup",
                    "hooks": [{"type": "command", "command": "existing"}],
                }
            ],
            "UserPromptSubmit": [
                {"hooks": [{"type": "prompt", "template": "existing"}]}
            ],
        },
    }

    installed = add_hook(json.dumps(existing).encode(), interpreter)
    parsed = json.loads(installed)
    assert list(parsed) == ["version", "hooks"]
    assert list(parsed["hooks"]) == ["SessionStart", "UserPromptSubmit"]
    assert parsed["hooks"]["UserPromptSubmit"] == [
        existing["hooks"]["UserPromptSubmit"][0],
        hook_group(interpreter),
    ]
    assert add_hook(installed, interpreter) == installed
    assert (
        remove_hook(installed, interpreter)
        == (json.dumps(existing, ensure_ascii=False, indent=2) + "\n").encode()
    )


def test_remove_only_hook_returns_empty_document(tmp_path: Path) -> None:
    interpreter = tmp_path / "python"
    assert remove_hook(add_hook(None, interpreter), interpreter) == b"{}\n"


@pytest.mark.parametrize(
    "content",
    [
        b'{"hooks":{},"hooks":{}}',
        b'{"value":NaN}',
        b'{"value":1e999}',
        b"[]",
        b'{"hooks":null}',
        b'{"hooks":[]}',
        b'{"hooks":{"Event":{}}}',
        b'{"hooks":{"Event":[[]]}}',
        b'{"hooks":{"Event":[{}]}}',
        b'{"hooks":{"Event":[{"hooks":null}]}}',
        b'{"hooks":{"Event":[{"hooks":{}}]}}',
        b'{"hooks":{"Event":[{"hooks":[[]]}]}}',
        b'{"hooks":{"Event":[{"hooks":[{}]}]}}',
        b'{"hooks":{"Event":[{"hooks":[{"type":"command","command":""}]}]}}',
    ],
)
def test_hook_transform_rejects_malformed_or_unsafe_documents(
    content: bytes,
) -> None:
    with pytest.raises(ValueError):
        add_hook(content)


def test_hook_transform_rejects_duplicate_shim_groups(tmp_path: Path) -> None:
    interpreter = tmp_path / "python"
    group = hook_group(interpreter)
    content = json.dumps({"hooks": {"UserPromptSubmit": [group, group]}}).encode()
    with pytest.raises(ValueError, match="ambiguous"):
        add_hook(content, interpreter)
    with pytest.raises(ValueError, match="ambiguous"):
        remove_hook(content, interpreter)


def test_hook_matching_is_type_sensitive_and_ignores_key_order(tmp_path: Path) -> None:
    interpreter = tmp_path / "python"
    exact = hook_group(interpreter)
    reordered = {
        "hooks": [
            {"type": "command", "timeout": 30, "command": exact["hooks"][0]["command"]}
        ]
    }
    assert (
        len(
            json.loads(
                add_hook(
                    json.dumps({"hooks": {"UserPromptSubmit": [reordered]}}).encode(),
                    interpreter,
                )
            )["hooks"]["UserPromptSubmit"]
        )
        == 1
    )

    modified = hook_group(interpreter)
    modified["hooks"][0]["timeout"] = 30.0
    installed = json.loads(
        add_hook(
            json.dumps({"hooks": {"UserPromptSubmit": [modified]}}).encode(),
            interpreter,
        )
    )
    assert installed["hooks"]["UserPromptSubmit"] == [modified, exact]
    reverted = json.loads(remove_hook(json.dumps(installed).encode(), interpreter))
    assert reverted["hooks"]["UserPromptSubmit"] == [modified]


def test_noop_transforms_preserve_original_bytes(tmp_path: Path) -> None:
    interpreter = tmp_path / "python"
    installed = json.dumps(
        {"hooks": {"UserPromptSubmit": [hook_group(interpreter)]}},
        separators=(",", ":"),
    ).encode()
    unrelated = b'{ "hooks": {"UserPromptSubmit": []} }\n'
    no_hooks = b'{ "version": 1 }\n'

    assert add_hook(installed, interpreter) is installed
    assert remove_hook(unrelated, interpreter) is unrelated
    assert remove_hook(no_hooks, interpreter) is no_hooks


def test_hook_transform_caps_input_and_output() -> None:
    with pytest.raises(ValueError, match="too large"):
        add_hook(b" " * (MAX_CONFIG_BYTES + 1))
    content = json.dumps({"padding": "x" * (MAX_CONFIG_BYTES - 10)}).encode()
    with pytest.raises(ValueError, match="too large"):
        add_hook(content)
