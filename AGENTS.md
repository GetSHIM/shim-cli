# shim-cli agent guidance

Read `README.md`, `docs/architecture.md`, `docs/privacy.md`, and `docs/compatibility.md` before changing the detector, hook protocol, installer, or `shim watch`.

Apply the `ponytail:ponytail` and `code-writing-guidelines` skills to every implementation and review. Prefer the smallest complete change, standard-library boundaries, direct control flow, and no speculative abstractions.

## Safety boundaries

- The hook is synchronous, local, and network-free, with no daemon.
  `shim watch` is the one component that touches the network, and only as a
  forwarding proxy: it runs for the length of one command, binds to loopback
  only, forwards bytes unchanged, and originates no request of its own. Nothing
  under `src/shim_cli/watch/` may be imported from the hook path — the hook is
  a cold-start subprocess on every tool call and `tests/contracts` enforces it.
- Never log or persist raw prompts, findings, or replacement values. The only
  prompt-derived file is one `0600` typed redaction in OS temporary storage per
  supported block; remove it if the block response cannot be serialized. The
  session spool and the opt-in ledger hold entity *names and counts* and a
  scrubbed target path — never the value that produced them, and never a shell
  command. `shim watch` keeps sizes and counts only; no request or response body
  reaches disk.
- Safe hook input must produce exactly empty stdout and stderr.
- Handled prompt errors must block with a generic native response. On Windows
  the hook stands down with one line and inspects nothing: that is the absence
  of the inspector, not a handled error, so it does not block.
- A tool event shim cannot finish inspecting fails closed in every mode,
  `observe` included, as a prompt does. That covers the deadline, analysis
  failure, nesting deeper than `MAX_DEPTH` (64), input over 1 MB, malformed
  JSON, unreadable settings and a shim module that fails to import. A call
  (`PreToolUse` on Claude Code and VS Code) is denied. A Claude Code result is
  withheld: a built-in tool's result keeps its schema shape down to depth 64
  (keys, kind words, zeroed numbers) with every other string replaced by
  `[withheld by shim]`, and an MCP result is replaced whole. Where the client
  can do neither (`PostToolUseFailure`, VS Code `PostToolUse`, a built-in
  result whose shape never arrived), the user and the model are told it was
  not inspected and the model is told not to repeat values from it. Messages
  and records carry only the category (`deadline`, `too large`,
  `settings unreadable`, `analysis failed`), never payload text. If even the
  client adapter cannot load, the static answers in `hook.py` still deny the
  call, withhold an MCP result, report any other result and block the prompt.
  The deadline is counted from the process's start as the kernel records it:
  inspection stops at 20 s, and if no answer is out at 25 s a timer writes a
  fixed refusal for the event, exactly once, inside the 30 s every client
  registration uses.
- Tool inputs are classified per client by where they go (`policy.TOOLS`).
  The input of a local built-in tool (`Read`, `Grep`, `Glob`, VS Code
  `read_file`, ...: the `local-tool` direction) is never rewritten or refused,
  only reported. Known network tools (`WebFetch`, `WebSearch`, VS Code
  `fetch_webpage`) are masked under `enforce`. An MCP tool, or any tool shim
  does not know, is never rewritten: under `enforce` a call carrying a finding
  is denied. Commands and local writes keep their own directions. The diet
  reshapes only results of network and MCP tools.
- In every mode, and whatever else it finds, the hook refuses an agent's write
  to shim's settings, to a client settings file that holds or switches on
  shim's hooks, or to a file in the plugin folder running the hook, and an
  agent's shell command that runs `shim` with `config`, `revert` or
  `uninstall`. The reason names only the file or the subcommand. Other routes
  (the shell writing the file, an interpreter, `sh -c`, MCP tools) are not
  caught, and `docs/privacy.md` must keep saying so. If your active plugin
  folder (`claude --plugin-dir`, `chat.pluginLocations`, `CLAUDE_PLUGIN_ROOT`)
  is your working checkout of `plugins/shim-cli`, agent edits inside it are
  refused: point the plugin at a separate clone.
- Placeholder write-back (D3). Every redaction shim hands the model (a masked
  tool event, the prompt block's redacted copy, Copilot's rewritten prompt)
  first records the placeholder *names* it issued in the session's `.names`
  store beside the spool: type and number runs only, never values or
  fingerprints, capped apart from the log, cleared at `SessionEnd`. If they
  cannot be recorded, nothing masked is handed on. In every mode, a
  `Write`/`Edit`/`MultiEdit`/`NotebookEdit`, VS Code edit or `apply_patch`
  (added lines), or any MCP or unknown tool input, whose new text holds a
  placeholder issued in this session is denied with a reason; an unreadable
  store denies it too. Placeholder-shaped text shim never issued in this
  session is left alone. Shell commands are not read for it.
- Placeholder numbering (D4). Within one prompt or tool event a value always
  gets the same number and two values never share one, across every field,
  every piece of a long field and the `html` diet's single scan of the
  converted page; numbers start above any placeholder-shaped text the event
  already holds. The numbering lives in memory for that event only: no value
  fingerprint is written to disk. A masked span keeps its line breaks.
- The plugin launcher withholds a prompt and denies a tool call when no hook
  can run, falls through interpreters that cannot start, and runs Python
  isolated (`-I` for the archive, `-E` for the package script).
  `scripts/build_zipapp.py` `INCLUDED` must list every module the hook imports,
  for write tools too; `tests/plugins/test_u07_plugin_self_defense.py` runs the
  archive through the launcher.
- Entity settings default to every built-in type except the opt-in types in
  `guard/entities.py`; malformed or unsafe settings fail closed, and no
  settings field may contain prompt-derived data.
- Never modify real user configuration in development or tests; use temporary paths.
- Rebuild `plugins/shim-cli/bin/shim.pyz` only in a release commit: the plugin
  installs from `main`, so a rebuilt archive ships unreleased code to its users.
- This repository is public and much of its documentation and corpus comes from
  running the tool for real. Nothing committed may carry the machine it was
  written on — a home directory, an editor path, a scratch directory, a
  provider request or organisation id. Test credentials must be *obviously*
  synthetic (`0123456789abcdef`, a vendor's published example key), never a
  plausible-looking random string: a fake that looks real trips secret scanners
  and costs a reviewer time. `tests/contracts/test_no_local_leakage.py` checks
  every tracked file, with the markers read from the environment so it protects
  whoever runs it.
- A hook is shim's when its handler is exactly what a release wrote, whatever
  the interpreter: type `command`, the release's keys, and
  `<python> -I -B -m shim_cli.hook` (or the 0.2.0 `shim_guard.hook`) plus the
  client's argument, a shell command quoted exactly as shim quotes it. Any
  other hook is the user's and is never changed or removed, including one that
  runs shim and something else, another hook type, an extra argument or an
  extra key.
- Install may create a missing client settings file. It leaves exactly one
  current shim group per registered event: it removes every other shim group
  (another installation's, a 0.2.0 group, a stray event, a duplicate) and
  appends its own last, preserving unrelated settings and hooks. Revert removes
  every shim group and retains the document even when empty.
- Install and status run the exact isolated hook command they register on a
  safe and a sensitive fixture before calling it working. It is the only guard
  against a hook that fails open at the client, as a `pip install --user`
  shim does under `-I`.
- Keep install and revert idempotent, leave Codex inline `config.toml` hooks
  untouched, preview only shim's fragment, and require manual setup for
  malformed, unsafe, or concurrently changed files and for a group where
  shim's handler sits beside another hook.
- Do not add clients, auth, telemetry, daemons, plugin frameworks, or
  compatibility shims without an approved requirement. `shim watch` is the only
  approved proxy and it is opt-in per command: nothing may route a client
  through it automatically, and no shell profile may be edited to do so.
- Detection may be relaxed only in the direction that drops noise, never in the
  direction that drops a credential. Mask through (D6): where the end of a
  secret value is ambiguous (`PASSWORD=s3cr3tPass|next`), the mask may run
  through trailing `;`, `&` or `|` text, so a password is never partly exposed;
  in a list of fields, the list's separator ends the value. `parity-v1.json` is
  generated evidence and
  must not be regenerated to make a test pass; a deliberate difference from it
  goes in `DELIBERATE_DIVERGENCES` with its reason and its new expected result
  pinned as tightly as the old one.
- A cap that turns "a lot of sensitive data" into "no protection at all" is a
  bug, not a safeguard. Bound work by input size, which is already bounded, and
  keep masking whatever was found.

## Checks

Use CPython 3.13 and a compatible uv 0.12.x. CI also covers the CPython 3.10
floor and 3.14. Run the complete local check from the repository root:

```bash
python scripts/check.py
```

`scripts/benchmark_hook.py` gates hook latency in CI and in `release.yml`: a
fixture whose median passes its ceiling in `CEILINGS_MS` fails the run. Do not
pass `--ceiling-scale` in a workflow, and raise a ceiling only with a measured
reason.
