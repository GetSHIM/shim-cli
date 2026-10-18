"""Which shell command lines run `shim config`, `shim revert` or `shim uninstall`.

An agent that runs one of these can turn shim off, so the hook refuses it. A
command line that only mentions them, as text or data, must not be refused.
"""

from __future__ import annotations

import pytest

from shim_cli.policy import shim_control


@pytest.mark.parametrize(
    ("command", "subcommand"),
    (
        ("shim config --only email", "config"),
        ("shim config", "config"),
        ("shim config --only US_SSN --yes", "config"),
        ("FOO=1 shim revert claude", "revert"),
        ("FOO=1 BAR='a b' shim revert claude --yes", "revert"),
        ("cd x && /usr/local/bin/shim uninstall", "uninstall"),
        ("~/.local/bin/shim revert codex", "revert"),
        # Click reads `--` as the end of shim's options and runs what follows.
        ("shim -- config --only EMAIL --yes", "config"),
        ("shim -- revert claude", "revert"),
        ("command shim -- config", "config"),
        # macOS's default disk is case-insensitive: PATH finds these as shim.
        ("SHIM config", "config"),
        ("Shim revert claude", "revert"),
        ("/usr/local/bin/SHIM uninstall", "uninstall"),
        ("\u017fhim config", "config"),
        ("command shim config --reset --yes", "config"),
        ("command -- shim config", "config"),
        ("exec shim revert claude", "revert"),
        ("exec -- shim uninstall", "uninstall"),
        ("exec -a name shim config", "config"),
        ("env shim config", "config"),
        ('env -i PATH="$PATH" shim config', "config"),
        ("env -u HOME shim revert claude", "revert"),
        ('env -iu HOME PATH="$PATH" shim config', "config"),
        ("env -u HOME -C /tmp shim config", "config"),
        ("env --unset NAME shim config", "config"),
        ("env --unset=NAME shim config", "config"),
        ("env --chdir /tmp shim config", "config"),
        ("env --ch /tmp shim revert claude", "revert"),
        ("env --split-string -i shim config", "config"),
        ("env -- shim config", "config"),
        # zsh, the default shell on macOS.
        ("=shim config", "config"),
        ("noglob shim config", "config"),
        ("nocorrect shim revert claude", "revert"),
        ("true; - shim config", "config"),
        ("coproc shim config", "config"),
        ("true; shim config --reset", "config"),
        ("true;shim config", "config"),
        ("false || shim revert claude", "revert"),
        ("yes | shim revert claude", "revert"),
        ("sleep 1 & shim config", "config"),
        ("ls\nshim config --reset --yes", "config"),
        ("(shim config --reset)", "config"),
        ("{ shim config; }", "config"),
        ("if shim revert claude; then echo ok; fi", "revert"),
        ("! shim config", "config"),
        ("time shim config", "config"),
        ("while true; do shim config; done", "config"),
        ("echo $(shim config --json)", "config"),
        ('echo "$(shim config --json)"', "config"),
        ('echo "settings: $(FOO=1 shim config --json) done"', "config"),
        ("echo `shim revert claude`", "revert"),
        ('echo "`shim revert claude`"', "revert"),
        ("x=$(shim config --reset --yes)", "config"),
        ("2>/dev/null shim config", "config"),
        ("shim config >/dev/null 2>&1", "config"),
        ("shim \\\nconfig", "config"),
        ("'shim' config", "config"),
        ('"/usr/local/bin/shim" "revert" claude', "revert"),
        ("cat <<'EOF'\nignored\nEOF\nshim config", "config"),
        ("cat <<-EOF\n\tignored\n\tEOF\nshim revert claude", "revert"),
        ("true # cat <<EOF\nshim config", "config"),
    ),
)
def test_a_command_line_that_runs_shim_s_controls_is_named(
    command: str, subcommand: str
) -> None:
    assert shim_control(command) == subcommand


@pytest.mark.parametrize(
    "command",
    (
        "",
        "echo shim config",
        'grep "shim config" README.md',
        "grep -rn 'shim revert' docs",
        "shimmer config",
        "shim-hook config",
        "/opt/shim/bin/tool config",
        "shim doctor claude",
        "shim install claude --yes",
        "shim status",
        "shim help config",
        # shim has no option that runs a subcommand after it.
        "shim --version config",
        "shim --help revert",
        # A second `--` is shim's first argument: "No such command '--'".
        "shim -- -- config",
        "shim --version -- config",
        # Not shim on any disk: the dotted capital I folds to i and a dot.
        "SH\u0130M config",
        "shim",
        "which shim",
        "for word in shim config; do echo $word; done",
        "command -v shim",
        "git commit -m 'shim config: keep options'",
        'git commit -m "shim revert now removes every group; shim config next"',
        "echo 'a; shim config'",
        'echo "a && shim revert claude"',
        "echo '$(shim config)'",
        'echo "\\$(shim config)"',
        "echo hi # shim config",
        "echo hi # then; shim config",
        "# shim config --reset",
        "python3 -m pytest tests/cli -k 'shim config'",
        # No module in shim_cli runs the CLI: only the hook has a __main__.
        "python3 -m shim_cli.cli.app config",
        "python3 -m shim_cli revert claude",
        # A here-document is data: the commit message the agent writes.
        "git commit -F - <<'EOF'\nshim config: keep options\nshim revert claude\nEOF",
        "git commit -m \"$(cat <<'EOF'\nshim config: keep options\nEOF\n)\"",
        "cat > notes.md <<EOF\n    shim config --only EMAIL\nEOF",
        "cat <<-'END'\n\tshim uninstall\n\tEND",
        "cat <<< 'shim config'",
    ),
)
def test_a_command_line_that_only_mentions_them_is_not(command: str) -> None:
    assert shim_control(command) == ""


def test_an_unterminated_quote_or_here_document_still_reads_what_came_before() -> None:
    assert shim_control("shim config --only 'EMAIL") == "config"
    assert shim_control("shim revert claude; cat <<EOF\nshim config") == "revert"
    assert shim_control("echo 'shim config") == ""
