# Compatibility and evidence

## Supported surface

| Area | Status |
| --- | --- |
| Python | CPython 3.10 or newer for the package, tested on 3.10, 3.13 and 3.14; the plugin archive runs on 3.9 or newer |
| Operating systems | macOS and Linux. Windows is not supported: the hook stands down with one line on stderr and inspects nothing, so no prompt is withheld; the plugin launcher does the same under Git Bash; every CLI command except `help`, `--version` and `update` refuses with exit 2. This was simulated (platform patched, `fcntl` removed), not run on Windows. WSL runs as Linux, not yet verified |
| Prompt hooks | Codex CLI, Claude Code, GitHub Copilot CLI, and VS Code |
| Tool hooks | Claude Code `PreToolUse` and `PostToolUse`, masked, and `PostToolUseFailure`, reported only; VS Code `PreToolUse` reports and denies, `PostToolUse` reports only |
| `shim audit` | Claude Code history only: transcripts written by 2.1.270 to 2.1.291 were read; the record shapes are below |
| `shim watch` | Claude Code only. Codex is refused: it reads its endpoint from its own configuration, so the proxy is bypassed and the session measured as empty ([probe](probe-2026-09-codex-watch.md)). Copilot out of scope because a custom endpoint removes GitHub authentication |

## Install

The package runs on CPython 3.10 or newer; CI covers 3.10, 3.13 and 3.14, and
no newer release is refused by the package metadata. On a machine whose only Python
is 3.9, the plugin route needs no Python beyond 3.9 and the package route needs
`--python`: `uv tool install --python 3.12 --compile-bytecode shim`.

## The 0.2.0 names

The package was renamed from `shim_guard` to `shim_cli` in 0.3.0. From 0.3.0
through 0.3.2 a re-exporting compatibility package, a second console script,
the old settings variable and a second `shim-guard` marketplace entry kept an
install written by 0.2.0 working. 0.3.0 scheduled their removal for 0.5.0; 1.0
removed them, announced one release ahead in the 0.3.2 notes and in
`shim doctor`.

In 1.0 a client settings file that still carries `-m shim_guard.hook` makes the
client report `No module named shim_guard` on every prompt, and nothing is
inspected, until `shim install <client>` rewrites the line. `shim doctor
<client>` reports that shape, and a Copilot hook file under its 0.2.0 name, as
`FAIL` with that command, and counts no event from it as installed. Settings,
the ledger and the Copilot hook file still move on first contact.

A Claude Code plugin installed as `shim-guard@shim-guard` stopped updating at
0.3.2 and moves with:

```
/plugin uninstall shim-guard@shim-guard
/plugin marketplace add GetSHIM/shim-cli
/plugin install shim-cli@shim-cli
```

**Codex needed one migration in 0.3.0.** There the marketplace name in the manifest *is*
the identity, so renaming it to `shim-cli` orphans an install made under the old
name — `config.toml` still says the plugin is enabled while `codex plugin list`
reports nothing installed. Current Codex does not load a plugin hook at all (see
"Codex does not load the plugin's hook" below), so a Codex user who installed the
0.2.0 plugin replaces it with the package hook:

```
codex plugin remove shim-guard
codex plugin marketplace remove shim-guard
shim install codex
shim doctor codex
```

This affects the plugin only. A Codex user who installed the PyPI package is
unaffected, and the zero-install plugin path could not reach the archive under
Codex before 0.3.0, so no working Codex plugin install was broken.

Codex and Copilot install prompt hooks only. The repository contains no
Codex, Copilot or `PostToolBatch` tool adapter. Tool
coverage is based on live protocol evidence rather than documentation and is
printed by `shim doctor <client>`.

The local integrations follow the client-native protocol references: the
[Codex hook documentation](https://developers.openai.com/codex/hooks/), Claude
Code [hooks](https://code.claude.com/docs/en/hooks) and
[settings](https://code.claude.com/docs/en/settings), and GitHub's
[Copilot hooks reference](https://docs.github.com/en/copilot/reference/hooks-reference).
Codex leaves inline `config.toml` hooks untouched. Claude uses shell-free
arguments and native structured tool responses. Copilot uses
`userPromptTransformed` to replace the model-facing prompt; the original can
remain visible in its timeline.

**VS Code refuses before a tool and reports after it.** Measured on 20
September 2026 against VS Code 1.137.0 with Copilot Chat 0.65.0, through a
capture-only hook. Captures: `tests/fixtures/probe/vscode/`.

| Event | Tried | Result |
| --- | --- | --- |
| `UserPromptSubmit` | `continue: false` | **Stops it.** "A hook prevented chat from continuing", with the hook's reason, and no answer. |
| `PreToolUse` | `permissionDecision: "deny"` | **Denies it.** The command never ran and no `PostToolUse` followed. |
| `PostToolUse` | `decision: "block"` | **Does nothing to the result.** The model quoted the secret out of the blocked terminal output. |
| `PostToolUse` | `continue: false` | **Does not stop the turn.** The model answered from the result anyway. |

So `events/pipeline.py` carries a third field on `Adapter`, `power`, and every
adapter states which of `rewrite`, `refuse` or `report-only` its client grants
at that event. `_decide` caps the action at it: a mask becomes a refusal where
only refusal exists, and a refusal becomes a report where the model already
holds the data. The session record then says `found` rather than `blocked`,
because reporting a block that the model read through would be worse than
saying nothing.

Two further limits from the same run. A `read_file` result arrives as
`tool_response: ""`, so a file read can only be inspected by its path at
`PreToolUse`; the transcript records `success: true` and no content either. A
`run_in_terminal` result does arrive in full. Tool names are VS Code's own
(`read_file`, `run_in_terminal`) and inputs are camelCase (`filePath`), so a
Claude-shaped adapter would not match them.

The shipped default for tool traffic is `enforce`, chosen when masking was
free. In VS Code that would mean denying calls nobody asked to have denied, so
a mode that was never written down reports instead; `[mode] outbound =
"enforce"` turns denial on.

**The Agent Plugins manifest is what the submission checks read.** `plugin.json`
at the plugin root declares the `agent-plugins.org` v1 schema, whose fields are
closed: `displayName` and a `hooks` path, which the Claude manifest carries,
fail the gate. github/awesome-copilot resolves the manifest relative to the
listing's `source.path`, which is why the file sits in `plugins/shim-cli/`
rather than at the repository root, and it accepts only a tag or a full commit
SHA as `source.ref`.

**The same file reaches three clients.** VS Code, GitHub Copilot CLI and the
Copilot app all read `com.github.copilot/hooks/hooks.json`. Copilot CLI has its
own hook route through `shim install copilot`, and its `postToolUse` can replace
a result with `modifiedResult` where VS Code cannot, so the plugin command
stands down there rather than inspecting every prompt twice. Copilot CLI 1.0.85
sets `COPILOT_CLI=1` on every hook process, which is the guard; `VSCODE_PID` is
not usable for this, because it is also set in any terminal inside VS Code.

**Each client is pointed at one hook file, named by its own manifest.** Claude Code
reads `hooks/claude.json` from `.claude-plugin/plugin.json`, and
`.codex-plugin/plugin.json` names `hooks/codex.json` for Codex; there is no `hooks/hooks.json`
at the plugin root, which Claude Code 2.1.263 also loaded by convention and ran
beside its own file, so every Claude Code command passes the Claude directory's
rule that a hook command starts at `${CLAUDE_PLUGIN_ROOT}` with no inline program.
Before 1.0.2 the Codex command opened with `[ -z "${PLUGIN_ROOT}" ] && exit 0` so
that Claude Code, which leaves `PLUGIN_ROOT` unset, stood it down; with the file
out of Claude Code's reach the guard is gone. Whether Codex honours the manifest's
`hooks` path could not be measured, because Codex 0.151.0 and 0.159.0 load no
plugin hook at all; Codex users install the hook with `shim install codex`.

**A Codex hook does not run until it is trusted.** From 0.151.0 Codex holds a
persisted trust record per hook and silently skips any hook it does not have
one for: no warning, no line in the transcript, and prompts reach the model
uninspected. Writing the fragment is therefore only half of `shim install
codex` — review and trust it in Codex, which is why `shim install codex` ends
by sending you to `/hooks` and `shim doctor codex` ends on `Codex hook
activation is client UI state; verify shim with /hooks`. shim
cannot read that record and does not write it; a diagnosis that claimed to
would be guessing. `codex exec --dangerously-bypass-hook-trust` runs enabled
hooks without it, which is useful to confirm an install and wrong as a habit.

**Codex runs hooks in a daemon that keeps its first environment.** Codex 0.159.0
starts an `app-server-daemon` with the first session and runs every later
session's hooks inside it. The hook sees the variables of the session that
started the daemon, not of the current one: a `SHIM_CONFIG` or `CODEX_HOME` set
for a later session does not reach it until the daemon restarts. A relative
`CODEX_HOME` or `SHIM_CONFIG` is refused, and the prompt is withheld.

**Codex does not load the plugin's hook.** Measured 29 September 2026 on Codex
0.151.0 and 0.159.0, in a separate `CODEX_HOME`: `codex plugin marketplace add
GetSHIM/shim-cli` and `codex plugin add shim-cli@shim-cli` installed 1.0.1 and
`codex plugin list` reported it `installed, enabled`, but `/hooks` listed no
installed hook for any event, and a prompt carrying `AKIAIOSFODNN7EXAMPLE` reached
the model. A launcher instrumented to log every invocation logged none, with the
1.0.1 layout and with the hook file named in `.codex-plugin/plugin.json`, under
`codex exec --dangerously-bypass-hook-trust` as well. `codex features list` shows
`plugin_hooks` as `removed`; Codex's plugin guide says plugin hooks follow plugin
enablement. On the same Codex 0.159.0 the package hook written by `shim install
codex` appeared in `/hooks`, was trusted there and ran on every prompt. The
payload Codex handed it, captured and replayed with `user-prompt = "enforce"`,
was answered with `shim blocked this prompt: SECRET (1).`
The README therefore sends Codex users to `shim install codex`.

## Claude Code history, as `shim audit` reads it

Each session is one file, `~/.claude/projects/<folder>/<session id>.jsonl` (or
under `$CLAUDE_CONFIG_DIR/projects`), where the folder is the session's working
directory with every character other than a letter or digit replaced by `-`:
`/Users/you/my_app` becomes `-Users-you-my-app`. Each line is one JSON record.
`shim audit` takes a record's project from its `cwd` and its date from its
`timestamp`. A typed prompt, trimmed to the keys it reads:

```json
{"type":"user","cwd":"/Users/you/my_app","timestamp":"2026-10-01T09:30:00.000Z","message":{"role":"user","content":"deploy with AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"}}
```

Read from transcripts Claude Code 2.1.270 to 2.1.286 wrote on 1 October 2026
(key names only), and from the field-test transcripts of 2.1.284:

| Record | Where the text is | Counted as |
| --- | --- | --- |
| `type: "user"`, `message.content` a string, or `text` blocks | the string or the blocks | `your prompt` |
| `type: "user"`, `tool_result` blocks | `content`, a string or `text` blocks; `is_error: true` for a failed call | the tool's name, joined through `tool_use_id`, or `failed <tool>` |
| `type: "assistant"`, `text` and `thinking` blocks | `text`, `thinking` | model output |
| `type: "attachment"`, `attachment.type: "file"` | `attachment.content.file.content` (2.1.270 and later), or `attachment.content` as a string (2.1.284 field test) | `@<displayPath>` |
| `attachment.type: "queued_command"` | `attachment.prompt`, `text` blocks | `your prompt` |
| `attachment.type: "edited_text_file"` | `attachment.snippet` | `edited <file name>` |

Skipped: `isCompactSummary` user records (a summary of what is already in the
file), the prompt a parent agent wrote into a sub-agent transcript
(`<session id>/subagents/agent-<id>.jsonl`, whose tool results count under the
parent session), the structured `toolUseResult` copy of a tool result, and every
other attachment type (hook context, prompt snapshots, environment, skill and
tool listings). `history.jsonl`, beside `projects/`, holds one line per
interactive prompt with the keys `display`, `pastedContents`, `project`,
`sessionId` and `timestamp`; `sessionId` is the transcript's file name.

## 1.1.2 release evidence

Recorded 6 October 2026 on Linux 6.18 x86_64, CPython 3.13.16, uv 0.12.23, on
the 1.1.2 candidate with the rebuilt `bin/shim.pyz`. Two detector fixes and the
tested Claude Code version; no hook protocol change.

| Evidence | Recorded result |
| --- | --- |
| Local gate | `python scripts/check.py` steps green: lint, format, types, build, `git diff --check`; 2,566 tests passed and 2 skipped as root. The other two, `tests/cli/test_audit_purge.py`'s read-only folder cases, cannot fail a write as root and passed as an unprivileged user. |
| Claude Code | tested: 2.1.291. The candidate installed with `uv tool install --python 3.12 --compile-bytecode` (CPython 3.12.3) into a scratch home, `shim install claude`, then `claude -p` sessions on three models with `--output-format stream-json`; every hook event ran. A `Read` of a synthetic `.env` reached the model as `AWS_ACCESS_KEY_ID=<SECRET_1>`, `BILLING_IBAN=<IBAN_1>`, `OWNER_EMAIL=<EMAIL_1>`, `DB_PASSWORD=<SECRET_2>`, and Claude Code's own transcript held the placeholders, not the values. A `Read` of `app.py` reached it with `datetime.fromtimestamp(1759744800)` and `ipaddress.IPv4Address(3221225985)` unchanged and `"tel (<PHONE_1>)"`; asked to add an hour, the model's `Edit` matched and the file on disk changed by that one number (1.1.1 handed it `fromtimestamp<PHONE_1>)`). `cat config.yaml` through Bash: `postgresql://<DB_URI_1>@db-prod.kasa.internal:5432/kasa`, `0.0.0.0:8080` unchanged. Grep results were masked the same way. A prompt with `ops@example.com`: `UserPromptSubmit says: shim: found EMAIL (1) in your prompt. Not modified.`; under `user-prompt = "enforce"` it was blocked with the `0600` redacted copy, which, read back through `--resume`, gave the model `<EMAIL_1>`. A WebFetch of `https://httpbin.org/get?email=ops@example.com` with no allow rule was refused, the denial showing `<EMAIL_1>`. `cat .env; exit 3` reported `unmasked 2 SECRET, 1 EMAIL, 1 IBAN (failed Bash)`. A session's record was gone after its `SessionEnd`. `shim audit` over those transcripts counted the two prompts and the failed Bash, not the blocked prompt, and, after the placeholder fix, no secret in model output where the code before this fix counted one (`` `DB_PASSWORD=<SECRET_2>` ``). The plugin loaded with `--plugin-dir plugins/shim-cli` and no `shim-hook` on `PATH`, so the archive ran: the same `.env` and `app.py` reads, the same placeholders, the timestamp unchanged. |
| Codex CLI | tested: 0.159.0. Not re-run: no Codex client on the recording machine. The detector fixes reach the Codex prompt hook through the same `evaluate`, covered by the suite. |
| GitHub Copilot CLI | tested: 1.0.83. Not re-run, as Codex. |
| VS Code | tested: 1.137.0. Not re-run, as Codex. |
| Windows | Unchanged: every command still refuses with exit 2 before touching a file (`test_on_windows_every_command_refuses_before_touching_a_file`). |

## 1.1.1 release evidence

Recorded 3 October 2026 on macOS 26.4 arm64, CPython 3.13.5, uv 0.12.5, on the
1.1.1 candidate with the rebuilt `bin/shim.pyz`. One hook change, Claude Code's
masked tool input, and two lines of CLI output; the rest is documentation.

| Evidence | Recorded result |
| --- | --- |
| Local gate | `python scripts/check.py` green: 2,557 tests, lint, format, types, build. |
| Claude Code | tested: 2.1.286. The candidate wheel installed with pipx on CPython 3.14.3, `shim install claude` written to a scratch settings file and loaded with `--settings`, no other settings. `claude -p` asked to WebFetch `https://example.com/?ref=docs`: refused, nothing had approved it. The same fetch with `?email=ops@example.com`: refused as well, the denial showing `?email=<EMAIL_1>`; under 1.1.0 this fetch ran. With `--allowedTools 'WebFetch(domain:httpbin.org)'`, a fetch of `https://httpbin.org/get?email=ops@example.com` ran and httpbin echoed `args.email` as `<EMAIL_1>`. The plugin loaded with `--plugin-dir plugins/shim-cli`, no `shim-hook` on `PATH`, so the archive ran: the email fetch was refused with `<EMAIL_1>` in the denial. |
| Codex CLI | tested: 0.159.0. `shim install codex` into a scratch `CODEX_HOME` ends `WARN Codex skips a hook you have not trusted, without warning: open /hooks in Codex, review the shim entry and enable it.` The hook is unchanged and was not re-run. |
| GitHub Copilot CLI | tested: 1.0.83, run on 1.0.85. `shim install copilot` and `shim doctor copilot` against a scratch `COPILOT_HOME`: `PASS The shim plugin stands down in GitHub Copilot CLI, so nothing is inspected twice.`, `PASS GitHub Copilot CLI has no trust step; the hook runs from the next session.`, and one warning, `GitHub Copilot CLI 1.0.85 is newer than tested 1.0.83.`; exit 0. The hook is unchanged and was not re-run. |
| VS Code | tested: 1.137.0. Not re-run: 1.1.1 changes no VS Code hook. |
| Python 3.14 | The full suite on CPython 3.14.3: 2,557 passed. `pipx install --python python3.14` of the built 1.1.1 wheel: `shim --version` answered `shim 1.1.1`. `pipx install --python python3.12 --fetch-missing-python` of the same wheel on a machine with no Python 3.12 fetched CPython 3.12.15 and installed it. |
| Documentation | Three simulated first-time users followed the 1.1.0 docs from zero in sandboxed homes, on Claude Code, on Codex, GitHub Copilot CLI and VS Code, and through every recipe; what stopped or misled them is fixed, each changed claim checked against the code or by running it. The cookbook's CI step was run in a throwaway repository under `bash -eo pipefail`: a clean text change exits 0, an added `AKIAIOSFODNN7EXAMPLE` 1, an added PNG and a deleted file 0, no change 0, and a missing `origin/main` fails. |
| Windows | Unchanged: every command still refuses with exit 2 before touching a file (`test_on_windows_every_command_refuses_before_touching_a_file`). |

## 1.1.0 release evidence

Recorded 1 October 2026 on macOS 26.4 arm64, CPython 3.13.5, uv 0.12.5, on the
1.1.0 candidate with the rebuilt `bin/shim.pyz`. The hook is the 1.0.3 hook; what
is new are two commands that read files on this machine, run against Claude
Code's own history and synthetic files.

| Evidence | Recorded result |
| --- | --- |
| Local gate | `python scripts/check.py` green: 2,554 tests, lint, format, types, build. |
| Claude Code | tested: 2.1.286. **`shim audit`** read a real 166 MB history in 70 s and analysed every text in it. **`--purge`**, limited with `--project` to a scratch folder holding two throwaway sessions, run in a pseudo-terminal: `deleted 1 session (1 file, 0 directories) and 0 lines of history.jsonl, on this computer only.`; `claude --resume` found the kept session and answered `No conversation found` for the deleted one, and `history.jsonl` was byte-identical to a copy taken before. **`shim keys`:** with only a `CLAUDE.md` line pointing at it and `Read` allowed, the model ran `shim keys .env` instead of opening the file and said it had seen no value. |
| Codex CLI | tested: 0.159.0. Not re-run: 1.1.0 changes no hook, installer or message, and `shim audit` reads Claude Code's history only. |
| GitHub Copilot CLI | tested: 1.0.83. Not re-run, for the same reason. |
| VS Code | tested: 1.137.0. Not re-run, for the same reason. |
| Python 3.14 | The full suite on CPython 3.14.3: 2,554 passed. `pipx install --python python3.14` of the built 1.1.0 wheel: `shim --version` answered `shim 1.1.0`, `shim keys .env` listed a synthetic file (`DB_PASSWORD  set  SECRET`), and `shim audit` on an empty home answered `shim: no Claude Code history at ~/.claude/projects.` with exit 2. |
| Windows | Simulated, not run: with the Windows check set, `shim audit`, `shim audit --purge` and `shim keys` refuse with exit 2 and write nothing, as every other command does (`test_on_windows_every_command_refuses_before_touching_a_file`). |

## 1.0.3 release evidence

Recorded 1 October 2026 on macOS 26.4 arm64, CPython 3.13.5, uv 0.12.5, on the
1.0.3 candidate with the rebuilt `bin/shim.pyz`. The hook ran from the candidate,
installed into a scratch project's own `.claude/settings.json` and driven with
`claude -p --setting-sources project`; every value was synthetic.

| Evidence | Recorded result |
| --- | --- |
| Local gate | `python scripts/check.py` green: 2,434 tests, lint, format, types, build. |
| Claude Code | tested: 2.1.286. **Named secrets:** a `Read` of a `.env` reached the model as `AWS_SECRET_ACCESS_KEY=<SECRET_2>`, `SLACK_BOT_TOKEN=<SECRET_3>`, `LEDGER_API_TOKEN=<SECRET_4>`, with `MAX_TOKENS=4096` left as it was, and the model answered "A hook masked the sensitive values before they reached me." **Connection strings:** the model was handed `postgresql://<DB_URI_1>@db-prod.kasa.example.internal:5432/kasa` and named the production host. **Failed commands:** `cat .env && cat config/local_overrides.env` showed `shim: found DB_URI (1), EMAIL (1), SECRET (5) in a failed Bash. Claude Code does not let this output be masked; the model has these values.` and the summary line `unmasked 5 SECRET (failed Bash)`. **`@` files:** `@.env explain these variables` showed `shim: @.env holds DB_URI (1), SECRET (5), EMAIL (1). …`; under `enforce` the prompt was stopped and no value reached the transcript; `@q.sql` holding `LIKE '%admin%'` was read, found clean and answered with no shim line; a 314 KB `@big.log` was not attached by the client, and shim said nothing. **Code that names a secret:** a `Read` of a Python file holding `api_key_header = "X-Api-Key"`, `token_type = "Bearer"`, `self.auth_token = settings.auth_token`, `token_ids = tokenizer.encode(text)` and one `LEDGER_API_TOKEN` masked only the token (`masked 1 SECRET (Read client.py)`), and the model quoted the rest verbatim. **Percent signs:** under `enforce`, a prompt holding `LIKE '%admin%'` and `echo %DATE%` was answered, not withheld. **Red team:** 27 sessions in four rounds worked as a user would on a synthetic project (`.env`, docker-compose, Helm values, Kubernetes secrets, JSON and Django settings, tfvars, `.npmrc`, logs, a customer CSV, git history and a planted prompt injection): onboarding, `git diff` and `git log -p`, a sub-agent, edits and copies of `.env`, a password grep, `base64 .env`, base64 in Helm values, a JSON file and an `export` line, a grep for base64 blobs, a registry secret and a stack trace. Four gaps found there were fixed before this release (YAML passwords read as type names, base64 output, base64 beside a path in grep output, a wrapped base64 block in grep's numbered output). What still reached the model was values the agent disguised on request (a hex dump, spaced characters) or decoded and reprinted without their key, files attached with `@` under the default `warn`, and a pasted key, which shim reports. |
| Codex CLI | tested: 0.159.0. Not re-run: 1.0.3 changes no Codex hook file, installer or message. The detector changes are the same code for every client and are graded by the corpus. |
| GitHub Copilot CLI | tested: 1.0.83. Not re-run, for the same reason: neither the `com.github.copilot` hook file nor `shim install copilot` changed. |
| VS Code | tested: 1.137.0. Not re-run: the plugin's `com.github.copilot/hooks/hooks.json`, which VS Code reads, is unchanged. A `~/.claude/settings.json` written by `shim install claude`, which VS Code also reads by default, gains a `PostToolUseFailure` entry, and VS Code passes over an event name it does not know: in the 1.139.1 bundle its hook-file event map has no `PostToolUseFailure`, and an unmapped key is skipped. |
| Python 3.14 | The full suite on CPython 3.14.3: 2,434 passed. `pipx install --python python3.14` of the built 1.0.3 wheel: `shim --version` answered `shim 1.0.3`, and `shim doctor claude` ran on Python 3.14.3 (`PASS Local hook runner allowed and protected direct fixtures correctly.`). |
| Windows | Simulated, not run: `sys.platform` set to `win32`, `fcntl` and the `SIGALRM` timer API removed. The hook printed `shim: shim-cli does not support Windows yet; nothing was inspected.`, wrote nothing to stdout and exited 0; the launcher did the same with `uname` printing `MINGW64_NT-10.0-19045`; `shim install claude` refused with exit 2. |

## 1.0.2 release evidence

Recorded 29 September 2026 on macOS 26.4 arm64, CPython 3.13.5, uv 0.12.5, on the
1.0.2 candidate with the rebuilt `bin/shim.pyz`.

| Evidence | Recorded result |
| --- | --- |
| Local gate | `python scripts/check.py` green: 2,058 tests, lint, format, types, build. |
| Claude Code | tested: 2.1.263, run on 2.1.273. The candidate plugin loaded with `--plugin-dir plugins/shim-cli`, no `shim-hook` on `PATH`, so the archive ran. The debug log read `Read manifest hooks for plugin shim-cli (enabled=true): ./hooks/claude.json` and no other hook file. A prompt carrying `AKIAIOSFODNN7EXAMPLE` gave one result, `{"systemMessage":"shim: found SECRET (1) in your prompt. Not modified."}`, no exit 127 and no second hook run. A `Read` of a synthetic `.env` logged `replaced tool output`, and the model answered that `<SECRET_1>` was not what the file held, because a hook had masked it. `claude plugin validate --strict` passed on both manifests. |
| Codex CLI | tested: 0.159.0. The candidate package in a scratch environment, `shim install codex` into a separate `CODEX_HOME`, the hook trusted in `/hooks`, `user-prompt = "enforce"`: `Blocked by hook` / `shim blocked this prompt: SECRET (1).` with the redacted-prompt line. The plugin route loads no hook on this version; see "Codex does not load the plugin's hook". |
| GitHub Copilot CLI | tested: 1.0.83. Not re-run: 1.0.2 changes neither the `com.github.copilot` hook file nor `shim install copilot`. |
| VS Code | tested: 1.137.0. Not re-run: 1.0.2 does not change the file VS Code reads. |
| Plugin scanner 2.0.1116 | Repository root 92/100, critical 0, high 0, medium 1 (`CLAUDE_MARKETPLACE_STRICT_INVALID`: a top-level `strict` fails `claude plugin validate --strict`), low 2. `plugins/shim-cli` 89/100, critical 0, high 0, medium 0, low 2. |
| Plugin scanner 3.8.0 | Repository root 94/100, `plugins/shim-cli` 89/100; critical 0, high 0, medium 0, low 2 on both. |

## 1.0.1 release evidence

Recorded 20 September 2026 on macOS 26.4 arm64, CPython 3.13.5, uv 0.12.5.

| Evidence | Recorded result |
| --- | --- |
| Local gate | `python scripts/check.py` green: 2,032 tests, lint, format, types, build. |
| VS Code | tested: 1.137.0, Copilot Chat 0.65.0. Hook protocol captured from a running client through a capture-only hook; fixtures in `tests/fixtures/probe/vscode/`. `continue: false` on `UserPromptSubmit` stopped the turn (`A hook prevented chat from continuing`, with the hook's reason, and no answer). `permissionDecision: "deny"` on `PreToolUse` refused `cat blocked.txt`; no `PostToolUse` followed. `decision: "block"` on a `PostToolUse` terminal result did **not** withhold it: the model answered with the secret from the blocked output. `continue: false` at the same event did not stop the turn either. `read_file` results arrive as `tool_response: ""`; `run_in_terminal` results arrive in full. Then the shipped plugin itself, through `hooks/run-shim vscode` and the committed archive: a prompt carrying a synthetic address answered `{"systemMessage":"shim: found EMAIL (1) in your prompt. Not modified."}` in 483 ms; `cat secrets.env` through the terminal tool answered `shim: found DB_URI (1), EMAIL (1), SECRET (1) in run_in_terminal. Not modified.` with `additionalContext` telling the model not to repeat the values, and the model described the file without quoting one; `Stop` rendered `shim — this session / warned 2 EMAIL (run_in_terminal, your prompt) / 1 DB_URI / 1 SECRET / overhead 168 ms median, 218 ms p95`. The archive path costs more than the package: 483 ms on the first event, 168 ms median after. |
| GitHub Copilot CLI | tested: 1.0.83, run on 1.0.85. The plugin loaded with `--plugin-dir` and the `com.github.copilot` hook stood down: a prompt carrying a synthetic AWS key produced no shim line and no error. `COPILOT_CLI=1` is set on every hook process, which is what the guard keys on; `VSCODE_PID` is not usable, because a terminal inside VS Code sets it too. |
| Claude Code | tested: 2.1.263, detection re-checked on 2.1.273. The new root `plugin.json` does not disturb Claude Code's own manifest: `claude plugin install shim-cli@shim-cli` into an isolated `CLAUDE_CONFIG_DIR` reported the plugin enabled at its version. Live on this build through the committed archive, with the hook registered in a scratch workspace's own `.claude/settings.json` rather than in any real user file: a `Read` of a synthetic `.env` reached the model as `AWS_ACCESS_KEY_ID=<SECRET_1>` and `SUPPORT_EMAIL=<EMAIL_1>`, and the model answered that a masking layer had replaced the values and it would not route around it. The same fixture masks identically through the package and the archive. |
| Codex CLI | tested: 0.151.0. `codex plugin add shim-cli@shim-cli` into a separate `CODEX_HOME` listed the plugin installed and enabled from `plugins/shim-cli`; the root manifest did not change its detection. No live prompt was run for this build. |
| A Claude install under VS Code | `~/.claude/settings.json` is read by VS Code by default. With `user-prompt = "enforce"`, the hook now answers a VS Code payload with `continue: false` and a Claude payload with `decision: "block"` plus `suppressOriginalPrompt`, from the same installed hook line. Covered by `tests/clients/vscode/test_claude_install_under_vscode.py`. |

## 1.0.0 release evidence

Recorded 15 September 2026 on macOS 26.4 arm64, CPython 3.13.5, uv 0.12.5, on the
1.0.0 candidate at `60f75ce`. Every client ran in an isolated configuration: a
project-scoped Claude Code plugin install, a separate `CODEX_HOME`, and a temporary
`HOME` for the package route, so no real client settings were written.

| Evidence | Recorded result |
| --- | --- |
| Local gate | `python scripts/check.py` green on the candidate: 1,987 tests, lint, format, types, build. |
| Tag-time re-verification | `release.yml` re-runs the same gate on the tagged tree, rebuilds from a clean snapshot and requires the fresh build to match the tested artifacts byte for byte. It refuses the tag while this record carries a pending marker. |
| Claude Code | tested: 2.1.263. Plugin installed through `claude plugin marketplace add` and `claude plugin install shim-cli@shim-cli`, `PATH` holding only `/usr/bin/python3` 3.9.6 and no shim package; the cached `bin/shim.pyz` is byte identical to the candidate's. A Turkish prompt with a synthetic IBAN: `UserPromptSubmit says: shim: found IBAN (1) in your prompt. Not modified.` A `Read` of `orders.json` reached the model as `"created": 1757496600, "amount": 2000, "cost": 0.0376118499` and the model quoted `1757496600`. A `Read` of a synthetic `.env` reached it as `AWS_ACCESS_KEY_ID=<SECRET_1>`, `DATABASE_URL=<DB_URI_1>`, `SUPPORT_EMAIL=<EMAIL_1>`, and the model answered that a masking layer had replaced the values and `<EMAIL_1>` stands in for the real value, which is not in the file. `Stop`: `masked 1 DB_URI, 1 EMAIL, 1 SECRET (Read .env)`, `warned 1 IBAN (your prompt)`, `1 PHONE (bare numbers, left as they were) (Read orders.json)`, `overhead 148 ms median, 165 ms p95`. `shim doctor claude` on the package: `PASS Claude Code 2.1.263 ... is tested.`, `PASS Coverage: 5 of 5 events installed.`, exit 0. |
| Claude Code `shim watch` | Subscription sign-in, `shim watch -- claude -p "Read calc.py and explain it in one sentence."`: `input 198,465 tokens (exact)`, `spend ~$1.55 (approximate, 2026-08-30 prices; API-key equivalent, this session is on a subscription, not a bill)`, `nothing was modified, and no request body was written to disk`. `--json`: `"spend_basis": "subscription"`, `auth_route` `subscription`. |
| Codex CLI | tested: 0.151.0. Plugin added from the candidate's marketplace into a separate `CODEX_HOME`; trust granted for the invocation with `codex exec --dangerously-bypass-hook-trust`, because persisted trust needs the interactive UI. `user-prompt = "enforce"` with a synthetic email: `hook: UserPromptSubmit Blocked`. `observe`: `hook: UserPromptSubmit Completed`, then the account's usage limit refused the model call. This run found that 0.3.1 and 0.3.2's plugin hook never inspected a Codex prompt (fixed in 0.3.3). `shim watch -- codex`: `FAIL shim watch does not support codex. Codex takes its endpoint from its own configuration, so the proxy would be bypassed and the session measured as empty. The Codex prompt hook is unaffected.` |
| GitHub Copilot CLI | tested: 1.0.83. Copilot Free sign-in, candidate package in a separate `COPILOT_HOME`. `shim install copilot`: `PASS Installed shim for GitHub Copilot CLI.`; `shim doctor copilot`: `PASS shim's exact GitHub Copilot CLI hook group is present.`, `PASS Coverage: 1 of 1 events installed.`, exit 0. The hook ran in `copilot -p` without a `/hooks` step. A safe prompt: the reply `ok` and no shim line. A synthetic email, the model asked to repeat the prompt verbatim: `please email the report to <EMAIL_1> today`. The hook command pointed at a missing interpreter, same prompt: `please email the report to ayse.yilmaz@example.com today`, exit 0, so a failing hook lets the prompt through unchanged. |
| Package on a 3.9-first `PATH` | `python3` → 3.9.6 first on `PATH`: `uv tool install --python 3.12 --compile-bytecode` of the candidate wheel installed `shim 1.0.0` with exactly two executables, `shim` and `shim-hook`. `shim install` previewed each fragment in words (`Would add 5 hook entries ... Nothing else in the file changes.`); Codex's install ended `WARN Codex runs a hook only after you trust it: open Codex and accept the shim hook when asked.` A Claude Code prompt through the package hook: nothing on a safe prompt, `shim: found CREDIT_CARD (1) in your prompt. Not modified.` on a synthetic card, 47 ms median. |
| Upgrade from 0.2.0 | A home built with PyPI `shim==0.2.0` (`shim install` for all three clients, `shim config --ledger`), then the candidate installed over it. The 0.2.0 hook line: `No module named 'shim_guard'`, exit 1. `shim doctor claude`: `FAIL hook installed in the 0.2.0 shape, which 1.0 does not run; run shim install claude.` and `WARN Coverage: 0 of 5 events installed`; the same for Codex and Copilot. `shim install` per client: `moved settings to ~/.config/shim/config.toml`, `Replaced the 0.2.0 hook line with the current one.`, and on Copilot `removed the old hook file`. The rewritten hook: `shim: found EMAIL (1) in your prompt. Not modified.`; doctor exit 0, `PASS No 0.2.0 names are left on disk.` |
| When something is wrong | The numbers table on the candidate (`evaluate`, 400 samples each): 2026 timestamps 0 %, random ten-digit integers 7 % (ids with a Turkish mobile shape), ten-decimal floats 0 %, eight ordinary tool-output lines 0 masked; `+90 532 123 45 67`, `(555) 123-4567`, `0212 555 12 34`, `5321234567` and `Tel: 4155552671` still masked. A settings file that does not parse: the prompt hook withholds the prompt with `shim could not inspect this prompt, so it was withheld. Run shim doctor claude for the reason.`, and doctor says `FAIL Settings at ... are invalid: Unclosed array (at line 2, column 1). Run shim config --reset to start over, or edit the line above.` once. The plugin launcher with no Python on `PATH`: `shim: no python3 found on PATH; the prompt was not inspected.`, exit 0. |
| Leaving | `shim revert` for all three clients on the upgraded home: Claude settings kept an unrelated hook and `"theme": "dark"` byte for byte; Codex `hooks.json` became `{}`; the Copilot hook file was removed. `uv tool uninstall shim` removed `shim` and `shim-hook`. Left on disk: the two client settings files and `~/.config/shim/config.toml`. |
| Python floor | CI green on 3.10 and 3.13 (macOS and Ubuntu) for every pull request of the 1.0 cycle; `archive-on-3-9` green; the committed archive answers identically on 3.9 and 3.13. |
| SBOM and attestation | `release.yml` fails the release unless the SBOM lists `shim` at the tag's version and every unconditional pin of `requirements.lock`, and verifies the wheel, source archive and `shim.pyz` against their attestations before publishing. 0.3.2, the first tag to carry it: 11 library components; from a clean download `gh attestation verify shim-0.3.2-py3-none-any.whl --bundle shim-0.3.2.intoto.jsonl --repo GetSHIM/shim-cli` exits 0, and the SBOM bundle verifies with `--predicate-type https://cyclonedx.org/bom`. |
| Supply-chain workflows | On `main` at `f9deea3`, pushed 15 September 2026: CodeQL, Scorecard, prose and CI green. Dependabot opens grouped pull requests (#14 Python, #18 Actions). |

## 0.3.0 release evidence

Recorded 8 September 2026 on macOS 26.4 arm64, CPython 3.13.5, uv 0.12.5.

| Evidence | Recorded result |
| --- | --- |
| Local gate | `python scripts/check.py` green: lock, `ruff check`, `ruff format --check`, `ty`, **1,856 tests**, wheel and source distribution built. |
| Tag-time re-verification | `release.yml` re-runs the same gate on the tagged tree, rebuilds from a clean snapshot and requires the fresh build to match the tested artifacts byte for byte. The tag is cut only from a commit whose `Verify` run is green. |
| Claude Code | tested: 2.1.263. Prompt and tool hooks exercised live; `shim watch` measured request and response, both directions reported apart, `stop_reason` read from the wire. The `Stop` last-block limitation is captured as a fixture. |
| Codex CLI | tested: 0.151.0. Prompt hook installed into a real `~/.codex` and exercised live: `observe` passed the prompt through, `enforce` blocked it before the model call. Hook trust is a client-side record shim cannot read; an untrusted hook runs silently not at all. |
| Codex `shim watch` | Refused, with the reason measured rather than assumed. See [the September 2026 probe](probe-2026-09-codex-watch.md). The transport works; the shipped implementation set an environment variable Codex ignores. |
| GitHub Copilot CLI | **Not run live for 0.3.0.** 1.0.83 is installed locally; tested: 1.0.80. Install and diagnosis paths are covered by the suite; no live client run was made for 0.3.0. |
| Context diet under the proxy | Seven scripted Claude Code sessions, 8 September 2026. The cache prefix held in every one, including a session whose configuration changed mid-run. A 22,199-byte tool result became 13,374 with the diet on and 21,690 with it off. [Study](study-2026-09-08-image-repeat-cache.md). |
| Python floor | 3.10 is exercised by CI only; no local 3.10 run was made. The bundled archive targets 3.9 and is rebuilt and compared by a contract test. |
| Supply-chain workflows | CodeQL, Scorecard, Dependabot and the prose check are configured and their pinning is asserted by `tests/contracts/test_workflows.py`. They run on pull requests into `main` and on pushes to `main`; a first green run of each is a condition of the release, not a claim of this document. |
| SBOM and attestations, from 0.3.2 | `shim-cli.sbom.cdx.json` is scanned from the tagged wheel installed with `requirements.lock`, and the release fails unless it lists `shim` at the tag's version and every unconditional pin in the lock: **11 library components** (`shim` and ten dependencies; `tomli` and `colorama` are conditional and absent on CPython 3.13), measured with syft 1.42.3 on the tagged tree. The wheel, sdist and `shim.pyz` are attested, the release job verifies all three before publishing, and the bundles are release assets. Against a downloaded wheel: `gh attestation verify shim-0.3.2-py3-none-any.whl --bundle shim-0.3.2.intoto.jsonl --repo GetSHIM/shim-cli` for provenance, and the same with `--bundle shim-0.3.2-py3-none-any.whl.sigstore.json --predicate-type https://cyclonedx.org/bom` for the SBOM. |

## Dated development evidence

These facts guided implementation. They are not evidence for the 0.2.0 tag and
must not be copied into its release record without a fresh run.

| Evidence | Recorded result |
| --- | --- |
| Locally inspected prompt clients | Codex CLI 0.149.0, Claude Code 2.1.251, GitHub Copilot CLI 1.0.80 |
| Claude tool protocol probe | Claude Code 2.1.250 on 29 August 2026; 71 sanitised fixtures from seven events |
| Interactive prompt clients | Codex ChatGPT sign-in, Claude first-party sign-in, and GitHub Copilot OAuth exercised on macOS 26.5.2 arm64 |
| Hook activation and timeout behavior | Hooks reviewed and activated; safe and finding prompts exercised; forced timeout or error observed to fail open at the client boundary |
| `shim watch` | Claude verified end to end on 30 August 2026 against a live subscription sign-in; request forwarded unchanged, streaming preserved, and provider usage read from the wire |
| `shim watch` scan scope | Claude Code 2.1.263 on 8 September 2026. One minimal request measured 191,599 bytes, 921 text leaves and 169,134 characters; a working request measured 4,402 leaves, 256,517 characters and 35 levels of nesting, the depth coming from an MCP tool's recursive JSON schema. The hook's own limits (2,000 leaves, 200,000 characters, depth 24) would report almost every real request as unmeasured, so the proxy carries its own. |
| `shim watch` both directions | Claude Code 2.1.263 on 8 September 2026. A synthetic three-IBAN file read through the Read tool and echoed back reported `request 3 IBAN in messages`, `response 3 IBAN in model text`, `compare IBAN 3 in request, 3 in response`. |
| `Stop` model output | Claude Code 2.1.263 on 8 September 2026. `last_assistant_message` is present and carries **only the turn's last text block**: a turn that said `CHECKING`, called `Read`, then answered held just the answer. Text the model produced before a tool call in the same turn is not counted. Capture: `tests/fixtures/probe/claude/Stop-none-model-reply-1.json`. |
| Codex `shim watch` transport | Codex CLI 0.151.0 on 8 September 2026, ChatGPT sign-in, macOS 26.4.0 arm64. With the base URL passed as a config override every request reached the proxy; with `OPENAI_BASE_URL` alone **nothing did**. `chatgpt.com` returned 200 to a request re-sent by Python's `http.client` with a stock TLS context, `cf-ray` present, no challenge — so there is no fingerprint rejection. A WebSocket upgrade was attempted and fell back to HTTP 0.602 s after a 426. The usage shape is still uncaptured: the account's quota returned 429 before any turn completed. [Probe](probe-2026-09-codex-watch.md). |
| Claude auth header shape | Claude Code subscription sign-in on 8 September 2026 sends `authorization` and no `x-api-key`, with `anthropic-beta` and `anthropic-version`; upstream 200 through the same harness. |
| Codex live prompt hook | Codex CLI 0.151.0 on 8 September 2026, ChatGPT sign-in, macOS 26.4.0 arm64. With the hook trusted, a prompt carrying a synthetic address reported `hook: UserPromptSubmit Completed` under `observe` and `hook: UserPromptSubmit Blocked` under `enforce`, the blocked prompt never reaching the model. The same prompt with the hook untrusted produced no hook line at all and was sent unchanged. |
| `PostToolUseFailure` channel | Claude Code 2.1.286 on 30 September 2026, a capture hook answering with shim's report object for `cat .env && cat missing-file`: the input carries `tool_name`, `tool_input`, `error` (a string that starts `Exit code 1`), `is_interrupt` and `duration_ms`, and no `tool_response`. The `systemMessage` was shown (`PostToolUseFailure:Bash says: shim: found …`), and the model answered that a hook had told it not to repeat the values and repeated none. A returned `updatedToolOutput` was ignored on 2.1.278 and 2.1.284: the model quoted the unmodified output. |
| `PreToolUse` masked input and permission | Claude Code 2.1.286 on 3 October 2026, `claude -p` with shim's hook loaded through `--settings` and no other settings. `updatedInput` beside `permissionDecision: "allow"` approved the call: a WebFetch with no allow rule ran once its URL held a masked email, while the same fetch without one was refused for want of approval. `updatedInput` alone was applied and left the decision to the permission rules: refused under `-p` with `?email=<EMAIL_1>` in the denial, and, with WebFetch allowed for httpbin.org, run with the masked value (httpbin echoed `args.email` as `<EMAIL_1>`). `permissionDecision: "ask"` with the same input was refused under `-p` ("A PreToolUse hook asked for confirmation"). Under 1.1.0's `allow`, a deny rule for `WebFetch(domain:example.com)` still refused the masked call, and an ask rule for it still required approval (refused under `-p`). |
| `Stop` scan cost | 66 KB final assistant text, hook end to end: 41 ms median, 50 ms p95 on macOS 26.5.2 arm64, CPython 3.13.5. Text beyond the detector's 100,000-character limit is not scanned and the record says `truncated`. |

The native Claude capture and the decisions made from it are preserved in the
[August 2026 probe](probe-2026-08.md). Repository fixtures contract the prompt
shape for all three clients and the two installed Claude tool-event shapes.

## Detector migration evidence

The evaluation unit is exact output, not category presence. The old
`guard-v1` corpus asserted only category sets, so a finding at the wrong offset
could pass. It has been superseded by:

| Corpus | Cases | Contract |
| --- | ---: | --- |
| `guard-v2.json` | 146 | Exact redacted output for every case, plus source spans for normalization-sensitive cases. |
| `guard-tools-v1.json` | 24 | Exact output at 25 scanned paths in captured tool payloads, per event and policy direction. |
| `parity-v1.json` | 475 | Exact findings, spans, scores, and redacted output from the previous Presidio implementation. |
| `custom-v1.json` | 10 | Exact output with your own patterns, including where they overlap a built-in type. |
| `reveal-v1.json` | 8 | Exact output with the last digits kept by `[reveal]`. |

Of the 475 parity cases, 430 remain byte-identical. Eight are deliberately
left unmasked: `0.0.0.0` and `::1`, which identify no person or remote host and
whose masking erased a meaningful bind-address distinction; five bare ids the
phone recognizer used to claim; and a connection string with no credentials in
it. Thirty-seven still mask, over fewer characters: thirty-six connection
strings lose only their user and password, and one address no longer runs into
a query string. All of them live in `DELIBERATE_DIVERGENCES` or `NARROWED_SPANS`
with reasons and tightly pinned new output. The parity corpus is generated
migration evidence and must never be regenerated to make a test pass.

The fixture-bound metrics report 100% synthetic precision, recall, and exact
output. That is deterministic contract evidence, not a real-world statistical
guarantee. Every implementation category has a positive and a targeted safe
negative, and the secret-assignment rule has prose negatives.

The detector is first-party and offline. `presidio-analyzer`, its spaCy
pipeline, and `tldextract` were removed; recognizers, checksums, and the public
suffix table are shipped in the package. `phonenumbers` is the one third-party
module on the hook path, enforced by the import contract.

## Historical performance evidence

Before the 0.2.0 release, 20 safe and 20 blocking fresh-process invocations of
the installed package were alternated without warm-up on Darwin 25.4.0 arm64,
macOS 26.4, and CPython 3.13.5:

| Fixture | p50 | p95 | Maximum |
| --- | ---: | ---: | ---: |
| Safe prompt | 63 ms | 66 ms | 67 ms |
| Email block | 64 ms | 67 ms | 69 ms |

Earlier hook-path measurements were:

| Hook path | Interpreter | p50 |
| --- | --- | ---: |
| Installed package | CPython 3.13 | 55–70 ms |
| Bundled `shim.pyz` | CPython 3.13 | about 110 ms |
| Bundled `shim.pyz` | macOS system CPython 3.9.6 | about 205–280 ms |
| Nothing runnable, allow and warn | — | about 6 ms |

The previous Presidio-based implementation measured p50 2,410 ms and p95
4,262 ms on the same development line. Host load dominates these figures, so
they are historical comparisons rather than a release guarantee. Detector
analysis has a 20-second deadline, the outer hook has a 25-second deadline, and
client settings use 30 seconds.

## 0.2.0 release evidence

Evidence was recorded from the `shim` 0.2.0 wheel on 2 September 2026. The
release owner accepted the remaining client-boundary risk and directed the
release without further local client testing.

| Required evidence | 0.2.0 value |
| --- | --- |
| Supported client versions and platform | Codex CLI 0.152.0, Claude Code 2.1.210, and GitHub Copilot CLI 1.0.82 inspected on macOS 26.6.2 arm64 with CPython 3.13.9 |
| Authentication routes | Codex ChatGPT sign-in confirmed; Claude and Copilot routes not revalidated |
| Trusted-hook activation | Exact Codex user hook reviewed and trusted; Claude and Copilot hook configurations installed and diagnosed but not activated in a live client |
| Safe, finding, timeout, and error behavior | Codex safe and synthetic-email finding paths passed; remaining live client and fault paths not rerun; protocol fixtures remain enforced in CI |
| Synthetic corpus and quality metrics | `guard-v2`, `guard-v2-metrics.json`, and `guard-tools-v1.json` |
| Fresh-process latency | `benchmark-hook.json`, generated from the tag |

Repository settings must protect `v*` tags and require reviewers for the
`release` and `pypi` environments; workflow code cannot enforce those
GitHub-side controls.

The tag workflow builds and tests the wheel and source distribution, then
builds the tagged source again on a separate runner and requires byte-identical
artifacts. It generates the plugin archive, locked runtime requirements,
benchmark, hashes, SBOM, and attestations. Release assets also include the
detector corpora and this compatibility record. These checks remain redundant
because they protect different trust boundaries.

The release record must agree with [the 0.2.0 release notes](releases/0.2.0.md),
package and plugin versions, the lock, the committed plugin archive, and the
tag name before the evidence marker is removed.
