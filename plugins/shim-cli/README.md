# shim-cli plugin

This plugin registers shim's local hooks. Claude Code gets the prompt,
verified `PreToolUse` and `PostToolUse`, `PostToolUseFailure` (report only),
`Stop`, and `SessionEnd` events. Codex
gets the prompt event only. VS Code gets the prompt, `PreToolUse`,
`PostToolUse` and `Stop`. There shim never masks; under `enforce` it stops a
prompt, or refuses a call before it runs, and after a tool it only reports:
measured against VS Code 1.137.0, a block after a tool is read straight through
by the model.

Three manifests sit side by side, one per format: `.claude-plugin/plugin.json`,
`.codex-plugin/plugin.json`, and `plugin.json`, the Agent Plugins v1 manifest
VS Code, GitHub Copilot CLI and the Copilot app read. Their hook files are
`hooks/claude.json`, `hooks/codex.json` and `com.github.copilot/hooks/hooks.json`,
each named by its manifest. Current Codex does not load a plugin's hook, so Codex
users install it with `shim install codex` instead; see `docs/compatibility.md`.
The Copilot clients run that last file too, so its command stands down when
`COPILOT_CLI` is set: `shim install copilot` is their route, and inspecting a
prompt twice helps nobody.

The plugin carries a self-contained hook in `bin/shim.pyz` on `main` and on
every tag, so it needs no package-manager step and no prerequisite beyond
Python 3.9 or newer, which is what a stock macOS provides. Claude Code and VS
Code reach it; current Codex loads no plugin hook.

**The plugin is the hooks, and only the hooks.** `shim watch`, `shim report`,
`shim config`, `shim audit` and `shim keys` are CLI commands and are not in the
archive. The hook is a
cold-start subprocess on every event and must not import command-only code.
Install the package to use those commands; for Claude, adding it does not
duplicate hook registration because the launcher simply prefers the package.

## What runs

`hooks/run-shim <client> [plugin-root]` resolves in this order and stops at
the first that works. An optional third argument names the event: the prompt
entries pass `prompt` and the `PreToolUse` entries pass `call`, and
`run-shim claude prompt` takes the root from `CLAUDE_PLUGIN_ROOT`.

1. `shim-hook` on `PATH` — the package install. Preferred when present: it is
   the newest build and starts faster, because a zipapp has no bytecode cache
   and reparses its modules on every event. It runs as `<python> -E -B
   shim-hook <client>`, with the Python its first line names, so no `PYTHON*`
   variable reaches it (`PYTHONPATH=.` cannot put a project's `token.py` ahead
   of the standard library). If the first line names no Python, or that Python
   is gone or cannot run shim, as after a tool environment's base Python is
   upgraded away, the launcher moves on to the archive.
2. `<plugin-root>/bin/shim.pyz` — the bundled archive, run as `python -I -B
   shim.pyz <client>`. The root is the second argument when one is given, else
   `CLAUDE_PLUGIN_ROOT`. Claude Code sets `CLAUDE_PLUGIN_ROOT`. Codex sets both
   that and its own `PLUGIN_ROOT` (measured on 0.151.0), and `codex.json`
   passes `${PLUGIN_ROOT}` as the argument, as the Codex plugin guide
   documents. Interpreters are tried newest first, `python3.14` down to
   `python3.9`, then `python3` and `python`; one that cannot start, such as a
   pyenv shim whose version is missing, the macOS `xcode-select` stub or a
   Python 2, is skipped for the next, and its error stays on stderr. Python
   older than 3.9 withholds a prompt, refuses a tool call and reports any other
   event.
3. Nothing runnable — the prompt is **withheld**, with the same answer the hook
   gives when it cannot inspect (Claude Code: `shim could not inspect this
   prompt, so it was withheld.`), and one line on stderr: `shim: no python3
   found on PATH; the prompt was withheld.`, `shim: no python3 on PATH could
   run the bundled archive; ...` or `shim: no hook available. ...`. A tool call
   is refused with `shim: could not inspect this tool call, so it was not
   allowed.`, and any other event gets `shim: <reason>; this event was not
   inspected.` This holds in every mode, because without Python the settings
   cannot be read.

On Windows (Git Bash, MSYS, Cygwin) it does none of this: it writes
`shim: shim-cli does not support Windows yet; nothing was inspected.` to stderr
and allows the prompt.

A disabled, crashed or timed-out hook can still let a prompt through, as the
client decides; a missing, broken or too-old Python no longer does.

`shim doctor <client>` prints a coverage table of what shim sees and can change
at each event, reports which of the three launchers is live, warns when the
bundled archive and the installed package disagree about their version, and
fails when both this plugin and a `shim install claude` hook are active in
Claude Code — that combination inspects every prompt and every tool event
twice.

## Choosing an installation method

The marketplace plugin and `shim install codex` / `shim install claude` are
alternative hook registrations. Do not use both for the same client. Either
client can add the package for CLI commands without duplicating registration:
the plugin launcher switches to the package hook.

```console
uv tool install --python 3.12 --compile-bytecode shim
# or
pipx install --python python3.12 --fetch-missing-python shim
```

`--python` matters on a machine whose only Python is the system 3.9:
without it `uv` quietly installs the last release that ran there, 0.2.0.
Neither line needs Python 3.12 installed first: `uv` fetches it itself, and
`--fetch-missing-python` makes `pipx` do the same. When `python3 --version`
already says 3.10 or newer, `pipx install shim` is enough.

`bin/shim.pyz` is built by `scripts/build_zipapp.py` and committed on `main` and
on every tag. A fork or partial copy without it falls back to `PATH` or to
case 3.

An agent cannot edit this folder while it runs the hook: shim refuses an
agent's write to any file in the plugin folder running the hook, and to the
settings that hold or switch on shim's hooks, in every mode. If you develop
shim-cli and point `--plugin-dir` or `chat.pluginLocations` at your working
checkout, agent edits inside `plugins/shim-cli` are refused; point the plugin
at a separate clone.
