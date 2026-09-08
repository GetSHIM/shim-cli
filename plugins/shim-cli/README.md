# SHIM Guard plugin

This plugin registers SHIM Guard's local hooks. Claude Code gets the prompt,
verified `PreToolUse` and `PostToolUse`, `Stop`, and `SessionEnd` events. Codex
gets the prompt event only.

Release-tag Claude plugins carry a self-contained hook in `bin/shim.pyz`, so
they need no package-manager step and no prerequisite beyond Python 3.10 or
newer. The Codex launcher currently uses `shim-guard-hook` from the installed
CLI package. A development checkout may not contain the release archive.

**The plugin is the hooks, and only the hooks.** `shim watch`, `shim report`,
and `shim config` are CLI commands and are not in the archive. The hook is a
cold-start subprocess on every event and must not import command-only code.
Install the package to use those commands; for Claude, adding it does not
duplicate hook registration because the launcher simply prefers the package.

## What runs

`hooks/run-shim-guard` resolves in this order and stops at the first that works:

1. `shim-guard-hook` on `PATH` — the package install. Preferred when present:
   it is the newest build and starts faster, because a zipapp has no bytecode
   cache and reparses its modules on every event.
2. For Claude, `${CLAUDE_PLUGIN_ROOT}/bin/shim.pyz` — the archive bundled in a
   release tag.
3. Nothing runnable — the prompt is **allowed** and one line is written to
   stderr explaining why it was not inspected.

Case 3 never blocks. A guard that cannot run is a guard that is off, not a
reason to refuse someone's prompt. The same holds for a tool event: an empty
stdout leaves the tool call and its result exactly as the client produced them.

`shim doctor <client>` prints a coverage table of what SHIM sees and can change
at each event, reports which of the three launchers is live, warns when the
bundled archive and the installed package disagree about their version, and
fails when both this plugin and a `shim install` hook are active for the same
client — that combination inspects every prompt and every tool event twice.

## Choosing an installation method

The marketplace plugin and `shim install codex` / `shim install claude` are
alternative hook registrations. Do not use both for the same client. Claude
can add the package for CLI commands without duplicating registration: the
plugin launcher switches to the package hook. Codex requires that package hook
today.

```console
uv tool install shim
shim help
```

`bin/shim.pyz` is built by `scripts/build_zipapp.py` and committed only for a
release tag. Between tags it may be absent; the launcher then falls back to
`PATH` or to case 3.
