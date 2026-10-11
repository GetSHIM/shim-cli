# Command reference

Every command shim has, what it prints, and what it exits with. The README is
the tour, this is the map, and the [cookbook](cookbook.md) is the recipes.

Everything here runs locally. No command in this document sends anything
anywhere, with one exception that is marked as such: `shim watch` forwards your
client's traffic to the provider your client was already going to talk to.

## Contents

- [Conventions](#conventions) — flags and exit codes shared by every command
- [Error codes](#error-codes) — the `code` and `fix` of every JSON error and doctor check
- [Setting up](#setting-up) — `install`, `status`, `doctor`, `revert`, `update`
- [Seeing what happened](#seeing-what-happened) — `report`, `ledger`, `watch`, `audit`
- [Changing what is detected](#changing-what-is-detected) — `config`
- [Checking text directly](#checking-text-directly) — `scan`, `redact`, `keys`, `demo`
- [The settings file](#the-settings-file) — every key in `config.toml`
- [Environment variables](#environment-variables)
- [Where shim keeps things](#where-shim-keeps-things)

## Conventions

**`--json`** is available on every command except `help` and `update`, and
writes one JSON object to stdout instead of the human table. Use it in scripts;
the text layout is not a stable interface and the JSON is. An error is an
object with `"status": "error"`, the sentence the human output prints in
`error`, a stable `code` and a `fix` (or `null`); read `code`, not `error`,
whose wording may change. Every code is listed under [Error
codes](#error-codes).

**`--yes`** skips the confirmation prompt on the commands that change or delete
a file (`install`, `revert`, `config`, `ledger purge`). Without it they ask
first, and a `no`, or no terminal to answer from, changes nothing and exits `1`.
`config` shows the new settings before it asks; `install` asks without showing
the change, so run `shim install <client> --dry-run` first to see the target
file and the exact fragment. With `--json` nothing is asked: `config`,
`install` and `revert` when they would change a file, and `ledger purge` when
something is retained, refuse with exit `2` and `CONFIRMATION_REQUIRED` unless
you pass `--yes`. `install --dry-run --json` writes nothing and needs no
`--yes`. `audit --purge`
has no `--yes`: it always asks you to type `delete N`.

**`--help`** works on every command, and `shim help` is the same as
`shim --help`. Each command's help repeats its own options, so this document is
the thing to read when you want the whole surface at once.

**Exit codes** are the same everywhere except `shim watch`, which exits with
your client's own code:

| Code | Meaning |
| --- | --- |
| `0` | Fine. For `doctor`, this includes warnings: a healthy install prints one for Claude Code, two for Codex and none for GitHub Copilot CLI, each plus one when your client is newer than the tested version. |
| `1` | Nothing to show, a finding, or a question you answered no. `report` with no session (its `--json` exits `0`), `status` when no hook is installed, `scan` when it found something. |
| `2` | Refused. Bad input, an unsafe or unreadable file, a client shim cannot support. |

`scan` exits `1` when it found something and `0` when it found nothing, the
reverse of grep, so `shim scan < file && echo "clean"` works in CI. `redact`
exits `0`, because its answer is the rewritten text, unless stdin cannot be read
or scanned in full: then both print `Unable to process stdin.` and exit `1`.
They print the same when shim's settings file is refused or invalid, whatever
stdin holds; `shim config` prints the reason.

## Error codes

With `--json`, every error carries `code` and `fix`:

```json
{"code":"REVEAL_INVALID","command":"config","error":"that entity cannot reveal a tail","fix":"Use --reveal IBAN=N, CREDIT_CARD=N or PHONE=N with N from 1 to 4.","schema_version":1,"status":"error"}
```

`code` is stable; `error` is the sentence a person would read and may change.
`fix` names the command or action that clears the error, with the real client
name and file path filled in, or is `null` when there is nothing to run.
Two objects say `"status": "error"` without a `code` of their own:
`shim doctor --json` when a check failed, whose failing checks each carry one,
and `shim demo --json` when its synthetic sentence found nothing, which means
the detector is broken.

| Code | Means | Emitted by | Exit | Fix |
| --- | --- | --- | --- | --- |
| `CONFIRMATION_REQUIRED` | The command would change a file and `--json` cannot ask | `config`, `install`, `revert`, `ledger purge` | `2` | `Add --yes to apply without a question.` |
| `OPTIONS_CONFLICT` | Two options that cannot be used together | `config`, `audit --purge --json` | `2` | `Run the command with one of them.` |
| `TERMINAL_REQUIRED` | `audit --purge` asks for a typed confirmation and there is no terminal | `audit` | `2` | `Run shim audit --purge in a terminal.` |
| `SETTINGS_PATH_INVALID` | `SHIM_CONFIG` is not an absolute path | `config` | `2` | `Unset SHIM_CONFIG or set it to an absolute path.` |
| `SETTINGS_PATH_UNSAFE` | The settings folder is writable by someone else or cannot be created safely | `config` | `2` | As for `SETTINGS_REFUSED`, or for `SETTINGS_UNWRITABLE` when the system refused to create the folder |
| `SETTINGS_INVALID` | The settings file does not parse or holds a value shim does not accept | `config`, `audit`, `doctor` | `2` | `Edit the settings file the error names until it parses.` The error names `shim config --reset` as the last resort: it discards every setting |
| `SETTINGS_REFUSED` | The settings file was refused: a link, someone else's, or writable by another user | `config`, `audit`, `doctor` | `2` | `chmod 700 <folder> && chmod 600 <path>` with the real paths (`SHIM_CONFIG`, or `$XDG_CONFIG_HOME/shim`); a link, a file that belongs to another user, a hard link or an oversized file gets its own fix instead |
| `CUSTOM_PATTERN_INVALID` | A `--custom` or `--custom-literal` value is malformed or backtracks | `config` | `2` | `Change the pattern, or remove it with shim config --remove-custom NAME --yes.` |
| `REVEAL_INVALID` | A `--reveal` names a type or length that is not allowed | `config` | `2` | `Use --reveal IBAN=N, CREDIT_CARD=N or PHONE=N with N from 1 to 4.` |
| `SETTINGS_CHANGED` | The settings file changed, or became unsafe, while shim was writing it | `config` | `2` | `Run the command again.` |
| `SETTINGS_UNWRITABLE` | The settings file cannot be written, and trying again will not help: permission denied, a read-only file system, a full disk | `config` | `2` | Names the folder and what to change: `chmod u+rwx <folder>`, a writable file system, or free space |
| `CLIENT_SETTINGS_UNREADABLE` | The client's settings file could not be inspected | `install`, `status`, `revert`, `doctor` | `2` | `Run shim doctor <client>.`; in doctor, make the file and its folder readable |
| `CLIENT_SETTINGS_MALFORMED` | The client's settings file does not parse, so shim will not change it | `install`, `status`, `revert`, `doctor` | `2` | `Fix <path> by hand so it parses, then run the command again.` |
| `CLIENT_SETTINGS_UNSAFE` | The client's settings file is a link, someone else's, or otherwise unsafe | `install`, `status`, `revert`, `doctor` | `2` | `Make <path> a regular file owned by you, then run the command again.` |
| `CLIENT_SETTINGS_CHANGED` | The client's settings file changed while shim was writing it | `install`, `revert` | `2` | `Run the command again.` |
| `CLIENT_SETTINGS_UNWRITABLE` | The client's settings file cannot be written, and trying again will not help: permission denied, a read-only file system, a full disk | `install`, `revert` | `2` | As for `SETTINGS_UNWRITABLE`, with the client's folder |
| `DETECTOR_UNAVAILABLE` | The detector could not start, so the hook would not work | `install` | `2` | `Reinstall shim: uv tool install --reinstall shim, or pipx reinstall shim.` |
| `INVALID_DATE` | `--since` is not a `YYYY-MM-DD` date | `audit` | `2` | `Pass --since as YYYY-MM-DD.` |
| `HISTORY_NOT_FOUND` | There is no Claude Code history folder to read | `audit` | `2` | `null` |
| `HISTORY_UNREADABLE` | The Claude Code history could not be read | `audit` | `2` | `null` |
| `RECORDS_UNREADABLE` | The session records could not be read | `report` | `2` | `Run shim doctor claude (or codex, copilot); its session_record line names the cause.` |
| `LEDGER_UNREADABLE` | The ledger could not be read | `ledger show`, `ledger purge` | `2` | `Run shim doctor claude (or codex, copilot).` |
| `FILE_NOT_FOUND` | The path does not exist | `keys` | `2` | `null` |
| `NOT_A_FILE` | The path is not a regular file | `keys` | `2` | `null` |
| `FILE_TOO_LARGE` | The file is larger than 1 MB | `keys` | `2` | `null` |
| `NOT_UTF8` | The file is not UTF-8 text | `keys` | `2` | `null` |
| `STDIN_UNPROCESSABLE` | Stdin could not be read or scanned in full, or the settings file is refused or invalid | `scan`, `redact`, `demo` | `1` | `Pipe UTF-8 text; if the settings file is refused, shim config prints why.` |
| `NOTHING_TO_RUN` | No client follows `--` | `watch` | `2` | `shim watch -- claude` |
| `CLIENT_UNSUPPORTED` | The client cannot run through the proxy | `watch` | `2` | `shim install <client> installs the hook, which needs no proxy.`, or `shim watch -- claude` for a command that is not a client |
| `BASE_URL_ALREADY_SET` | `ANTHROPIC_BASE_URL` is already set | `watch` | `2` | `Unset ANTHROPIC_BASE_URL for this command.` |
| `EXECUTABLE_NOT_FOUND` | The client is not on `PATH` | `watch` | `2` | `null` |
| `PROXY_FAILED` | The proxy could not start; the client was not run | `watch` | `2` | `null` |
| `CLIENT_START_FAILED` | The client could not be started | `watch` | `2` | `null` |

`shim doctor --json` writes each check as `name`, `status`, `detail`, `code`
and `fix`; `code` and `fix` are `null` on a `PASS`. A `FAIL` makes doctor exit
`2`, a `WARN` alone exits `0`. Besides the shared codes above, its checks use:

| Code | Means | Status | Fix |
| --- | --- | --- | --- |
| `CLIENT_NOT_FOUND` | The client executable is not on `PATH` | `FAIL` | Install the client, or add its folder to `PATH` |
| `CLIENT_VERSION_UNKNOWN` | The client did not report a version shim can read | `FAIL` | Run `<client> --version`; reinstall the client if it fails |
| `CLIENT_TOO_OLD` | The client is older than the oldest version shim supports | `FAIL` | Update the client to the version the detail names |
| `CLIENT_NEWER_THAN_TESTED` | The client is newer than the version shim was tested against | `WARN` | Check shim once by hand: a prompt holding `ops@example.com` must be reported |
| `CODEX_HOOKS_DISABLED` | `codex features list` says hooks are off | `FAIL` | Set `hooks = true` under `[features]` in Codex's `config.toml`, or delete that line |
| `CODEX_HOOKS_UNCHECKED` | `codex features list` could not be run | `FAIL` | `Run codex features list; it must show hooks as true.` |
| `LEGACY_NAMES_PRESENT` | A 0.2.0 hook, settings file or ledger is still on disk | `FAIL` for a 0.2.0 hook, else `WARN` | The `shim install`, `shim config` or `shim report` the detail names |
| `HOOK_NOT_INSTALLED` | shim's hook is not in the client's settings file | `WARN` | `Run shim install <client>.` |
| `DETECTION_DISABLED` | Every entity type is turned off | `WARN` | `Turn types back on with shim config --enable ENTITY --yes.` |
| `CUSTOM_PATTERN_UNSAFE` | A custom pattern already in the file backtracks | `FAIL` | `Simplify the pattern, or remove it with shim config --remove-custom NAME --yes.` |
| `HOOK_RUNNER_FAILED` | The hook did not pass its own safe and sensitive fixtures | `FAIL` | `Reinstall shim: uv tool install --reinstall shim, or pipx reinstall shim.` |
| `HOOK_RESOLUTION_FAILED` | No hook is runnable, so prompts pass uninspected | `FAIL` | `Run shim install <client>.` |
| `ARCHIVE_VERSION_SKEW` | The plugin's bundled archive and the package are different versions | `WARN` | `Run claude plugin update shim-cli@shim-cli and restart Claude Code; in a cloned plugin folder, check out the newest release tag.` |
| `PLUGIN_NOT_DISCOVERABLE` | shim cannot see whether the client also has the plugin installed | `WARN` | Remove the plugin if `shim install` is also in use |
| `DUPLICATE_HOOKS` | shim is registered twice, so every event is inspected twice | `FAIL` | The uninstall or `shim revert claude` the detail names |
| `SESSION_RECORDS_UNWRITABLE` | Session records cannot be written; masking still works | `WARN` | Set `TMPDIR` to a folder you own, or make the `shim-session` folder in it yours with mode 700 |
| `HOOK_EVENTS_MISSING` | Fewer events are installed than shim can cover | `WARN` | `Run shim install <client>.` |
| `HOOK_ACTIVATION_UNVERIFIED` | Whether the client runs the hook is client UI state shim cannot read | `WARN` | Open `/hooks` in the client and check that shim is listed (and, in Codex, enabled) |

## Setting up

### `shim install <client>`

Writes shim's hook into the client's own settings file. Clients are `claude`,
`codex`, `copilot`, and `status`, `doctor`, `revert` and `demo` take the same
three. VS Code has no target in any of them: it runs the plugin, and you check
it with a test prompt (see [`shim doctor`](#shim-doctor-client)).

```console
shim install claude              # asks, then writes; it does not show the change
shim install claude --yes        # writes without asking
shim install claude --dry-run    # shows the target file and the change, and exits
```

`--dry-run` names the file, sums the change up in a sentence and then prints
the exact JSON fragment:

```
WARN Would create Claude Code hooks at /home/you/.claude/settings.json with
this fragment:
WARN Would add 6 hook entries (UserPromptSubmit, PostToolUse,
PostToolUseFailure, PreToolUse, SessionEnd, Stop), each running
/usr/bin/python3 -m shim_cli.hook claude. Nothing else in the file changes.
```

Installing is additive and surgical: your other hooks stay, and shim appends
itself last. If a 0.2.0-shaped fragment is present it is replaced rather than
duplicated, and the output says so. The file is written back as 2-space JSON,
so a file you formatted by hand keeps its content but not its layout.

A client settings file that does not parse, such as a `settings.json` with a
trailing comma, is left alone: install exits `2` with `FAIL Claude Code hook
configuration cannot be changed safely.`, naming neither the file nor the
error, and runs once you have fixed the JSON by hand. With `--json` the same
refusal is `CLIENT_SETTINGS_MALFORMED`, and its `fix` names the file.

`--json` with `--yes` writes one object: `client`, `target` (the settings
file), `action` (`create`, `update` or `noop`), `events` (the hook events
written, sorted), `replaced_legacy` (a 0.2.0 hook was replaced),
`preserved_hooks` (other hooks in the file were kept), `warnings` (the `WARN`
lines the text output prints) and `next_step` (what to do in the client, or
`null`). `--dry-run --json` writes `client`, `target`, `action`, `events`,
`fragment` (the exact object above), `summary` and `"dry_run": true`, and needs
no `--yes`.

**Codex needs one extra step.** From 0.151.0 Codex will not run a hook until
you trust it, and it skips an untrusted one silently — no warning, no
transcript line. It does not ask, either. After installing, open `/hooks` in
Codex, review the shim entry and enable it. `shim install codex` ends with
that reminder:

```
WARN Codex skips a hook you have not trusted, without warning: open /hooks in
Codex, review the shim entry and enable it.
```

`shim doctor codex` cannot verify it, because that record lives in the client
where shim cannot read it.

For Claude Code, register the hook one way, `shim install claude` or the
marketplace plugin, not both: `shim doctor claude` fails when it finds two.
You need the package either way for `shim audit`, `shim keys`, `shim report`
and `shim config`. The VS Code plugin together with `shim install copilot` is
the intended pairing: the plugin stands down in GitHub Copilot CLI, so nothing
is inspected twice.

### `shim status <client>`

One line: is shim's hook in that client's settings file?

Exits `0` when installed, `1` when not. `--json` gives `{"state": "installed"}`
or `"not_installed"`, which is the form to use in a script. A Claude Code hook
installed by 1.0.2 or earlier reads as not installed, and exits `1`, until
`shim install claude` adds its `PostToolUseFailure` entry.

### `shim doctor <client>`

The command to run when something looks wrong. It checks the client version
against what was tested, whether the hook group is present and exactly once,
whether anything from 0.2.0 is still on disk, whether your settings parse,
whether session records can be written, which interpreter will actually run,
and which events are covered.

```console
shim doctor claude
```

A healthy install ends with a coverage table. **The warnings above it are
normal and doctor exits `0`:**

- Claude Code: `WARN Claude Code hook activation is client UI state; verify
  shim with /hooks.`
- Codex: the same activation warning, and ``WARN Plugin installs are not
  discoverable for this client; if you installed both the plugin and `shim
  install`, remove one.``
- GitHub Copilot CLI: none. It prints `PASS GitHub Copilot CLI has no trust
  step; the hook runs from the next session.` and `PASS The shim plugin stands
  down in GitHub Copilot CLI, so nothing is inspected twice.`

Each client adds one more, such as `WARN Codex 0.160.0 is newer than tested
0.159.0.`, when yours is newer than the version shim was tested against. A
`FAIL` exits `2`.

`--json` writes `checks`, each with `name`, `status`, `detail`, `code` and
`fix` ([Error codes](#error-codes)), and the `coverage` rows. An agent fixing
an install reads `fix` from the first check whose `status` is `FAIL` or `WARN`.

Doctor cannot see whether the client runs the hook. To see it run:

- Claude Code: start a new session after installing, run `/hooks` and find
  shim under `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
  `PostToolUseFailure`, `Stop` and `SessionEnd`. Then, in a scratch folder, put
  `AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE` in a `.env` and ask Claude to read
  it: the model gets `<SECRET_1>`, and the turn ends with `shim — this
  session` and `masked 1 SECRET (Read .env)`.
- Codex: once the hook is trusted in `/hooks`, a prompt holding
  `ops@example.com` shows `shim: found EMAIL (1) in your prompt. Not
  modified.` Codex lets shim report a prompt, not change it.
- GitHub Copilot CLI: there is no trust step. Ask the model to repeat a prompt
  holding `ops@example.com` word for word; it answers with `<EMAIL_1>`.
- VS Code has no doctor target. Type a prompt holding `ops@example.com` into
  Copilot Chat and expect `shim: found EMAIL (1) in your prompt. Not
  modified.`, as measured on VS Code 1.137.0; no shim line at all means the
  plugin is not running.

The coverage line counts the events whose hook is in the client's settings
file, and the table's `Installed` column reads the same file:
`PASS Coverage: 6 of 6 events installed.` once shim is installed, and
`WARN Coverage: 0 of 6 events installed; run shim install claude.` before. An
install made by 1.0.2 or earlier reads `5 of 6`, because `PostToolUseFailure`
is new; `shim install claude` adds it and leaves the other five alone. A
hook still in the 0.2.0 shape does not count, because 1.0 does not run it:
doctor reports it as `FAIL` with `run shim install <client>`. The coverage
`WARN` on its own exits `0`.

Most `FAIL` lines name the command that fixes them. These do not, or not
fully:

- A shim settings file that does not parse gives the path, the parser's
  message with its line number, and `shim config --reset`. Fixing that line by
  hand is often better: `--reset` discards everything, including custom
  patterns and hand-written `[mode]` and `[entities]`.
- An invalid value, such as `user-prompt = "block"`, gives `are invalid: they
  are not readable as settings` and names neither the key nor the line. Check
  the values against [the settings file](#the-settings-file).
- A refused settings file gives the reason, such as `target is writable by
  another user`, and no command; `--json` carries the fix for that reason. For
  a mode it is `chmod 700 ~/.config/shim && chmod 600
  ~/.config/shim/config.toml`, with your real paths; `shim config --reset
  --yes` does not repair permissions.
- `FAIL Codex hook support is not enabled.` comes from `codex features list`,
  and usually means Codex's own `config.toml` (in `~/.codex`, or
  `$CODEX_HOME`) turns hooks off. Set `hooks = true` under `[features]`, or
  delete that line: hooks are on by default. The same line appears when
  `codex features list` itself fails.
- A client settings file that does not parse, such as a `settings.json` with a
  trailing comma, gives `FAIL Claude Code hook configuration needs manual
  review.` and names neither the file nor the error. Fix the JSON by hand;
  `shim install` will not change the file until it parses.

### `shim revert <client>`

Removes shim's hook group and leaves everything else in the file alone —
other hooks, permissions, unrelated keys. For Copilot it also deletes the hook
file, which belongs to shim alone.

```console
shim revert claude --yes
```

The file is written back as 2-space JSON ending in a newline: one already in
Claude Code's own format, which ends that way, comes back byte for byte, a
hand-formatted one comes back with
the same content in that layout, and a settings file shim itself created is
left as `{}`.

`--json` with `--yes` writes `client`, `target`, `action` (`remove` or
`noop`), `removed_legacy_file` and `deleted_file` (the Copilot hook file it
deleted, or `null`).

This does not remove your settings (`~/.config/shim/config.toml`) or the
ledger. See the README's Uninstall section for the full sequence.

### `shim update`

Updates shim in place using whichever installer put it there.

## Seeing what happened

### `shim report`

The current session's summary — the same text the client shows you at the end
of a turn, on demand.

```
shim — this session
  masked    2 EMAIL  (Read env.txt)
            1 DB_URI  (Read env.txt)
            1 SECRET  (Read env.txt)
  warned    1 IBAN  (your prompt)
  model     1 EMAIL in its replies (written by the model; it may repeat values it was given)
  overhead  102 ms median, 120 ms p95
```

`masked` is what the model did not see. `unmasked` is what a failed tool call
printed: Claude Code does not let it be masked, so the model saw it, and shim
told you and told the model not to repeat it. `warned` is what shim found in
your prompt and left alone — prompts are reported, not rewritten. `model` counts
what the model itself wrote back. It is kept apart from everything else and is
not called a leak, but it is not called invented either: the model may repeat
a value it was given, for example from a file you attached with `@`.

A file attached with `@` is counted under `warned` with its name, as `(@.env)`.
`skipped` counts what shim could not inspect and let through, such as an
attached file past the limits.

It reads the live session record, which Claude Code's `SessionEnd` deletes.
Run `shim report` in a second terminal while the session is still open, or
turn the ledger on first with `shim config --ledger --yes`: then it reads the
retained copy after the session ends and says so (`WARN From the retained
ledger; the live session has ended.`). It shows the most recent session from
any client and does not name the client. `WARN No session on record.` means
there is nothing to read, not that nothing was found.

Exits `1` when there is no session to show. `--json` exits `0` even then, with
`"source": "none"`, so a script must check that field: it is `"session"` for a
live record and `"ledger"` for a retained one.

### `shim ledger show`

The ledger is an opt-in record that outlives a session, kept for 30 days. It is
off by default; turn it on with `shim config --ledger`.

```console
shim ledger show
```

```
shim ledger — 4 events over 2 day(s), kept for 30 days
  2026-09-01   1 event   1 IBAN
  2026-09-08   3 events   1 IBAN, 1 SECRET
```

`--json` returns every entry. Entries hold entity names, counts, tool names and
timestamps — never the values that were found.

### `shim ledger purge`

Deletes the retained records. They also age out on their own after 30 days.
It asks first; `--yes` skips the question, and `--json` needs it when
something is retained.

### `shim watch -- <client>`

Runs your client through a local measuring proxy, so you can see what a session
actually sent and what it cost. The client and its arguments follow `--`:

```console
shim watch -- claude
shim watch -- claude -p "explain this repo"
```

**This is the one command that touches the network**, and only to forward your
client's own traffic to the provider it was already using. Nothing is modified
and no request body is written to disk — the report says so on its last line.

The report prints when the client exits. When no request went through the
proxy, text mode prints no report at all: you see `PASS Watching claude on
http://127.0.0.1:…`, the client's own output and nothing after it. `--json`
still writes one, with `"requests": 0`. A report looks like this:

```
shim watch — 2m 34s, 3 requests
  input     82,926 tokens  (exact)
    cache read   54,900   66%
    cache write  28,020
  output    1,080 tokens  (exact)
  cut off   1 of 3 responses stopped at the output limit (max_tokens)
  where the input went  (approximate — split by byte share)
    tools     ~      30,728   37%
    system    ~      51,456   62%
    messages  ~         708    1%
  request   3 DB_URI, 3 EMAIL in messages
  response  1 EMAIL in model text; 1 EMAIL in thinking
            written by the model; it may repeat values it was given
  compare   EMAIL   3 in request, 2 in response (text, thinking)
            DB_URI  3 in request, 0 in response
  spend     ~$0.69  (approximate, 2026-10-09 prices)
  costliest  one request ~$0.41 (claude-opus-5-5, 54,212 input tokens)
  largest   one request was 112,384 bytes, system 61% of it
  images    2 distinct, ~4,988 input tokens across 2 requests (estimated)
  largest tool result  Bash, 40,960 bytes (~10,240 tokens), carried by 2 requests
  cache     1 request rewrote a prefix the previous one had cached (~18,000 tokens written again, ~$0.09); request 3
  duplicates  1 request was sent while an identical one was still in flight (~27,640 input tokens billed again)
  nothing was modified, and no request body was written to disk
```

`(exact)` means the provider reported that number. `(approximate)` means shim
attributed it by byte share, and the `~` is there to keep you honest about it.
Images are the exception: their base64 bytes say nothing about their cost, so
each request's estimated image tokens get an `images` row of their own (and an
`images` key in `tokens_by_section`), and the rest is split by byte share. The
rows still add up to the exact total. An image whose size shim could not read
has no estimate, so its bytes stay in `messages` and the `images` line counts it
as `of unknown size` instead of pricing it at zero.
Each request is priced by its own model and token kinds, 1-hour cache writes
at their own rate, from a table dated in the line; a model the table does not
name exactly is listed as `not priced`, never priced as a neighbour. Once the
table is more than 90 days old, the line adds `older than 90 days; newer models
and price changes are not reflected`. `costliest` names the dearest request
when at least two were priced.
If you sign in with a subscription, `spend` is what the same traffic would cost
on an API key, not a bill, and the line says so. When some requests arrived
while both inspection slots were busy, the section header adds `2 of 4 requests
measured` and the response line gives the same reason as the inspection line.
Such a request is still priced, and still compared for cache rewrites, under
the model its response names.

`--json` writes the same report as one object, on one line of stdout after
the client exits, so it follows whatever the client printed there itself: read
the last line. Three of its fields say what a figure covers:

| Field | Where | Values |
| --- | --- | --- |
| `spend_basis` | top level | `"api-key"`, `"subscription"`, `"mixed"`, or `"unknown"` when a priced request sent neither auth header or nothing was priced |
| `prices_stale` | `approximate` | `true` when the price table (`priced_on`) is more than 90 days older than the run, else `false` |
| `spend_usd` | each of `exchanges` | that request's approximate cost, or `null` when its model is not in the table |
| `priced_as` | each of `exchanges` | the table row used (the model id without a snapshot date or `-latest`), or `null` |
| `cache_creation_1h_input_tokens` | each of `exchanges`' `usage` | the part of `cache_creation_input_tokens` written to the 1-hour cache, priced at the 1-hour rate |
| `images` | top level | `distinct`, `requests` that carried one, `estimated_tokens` (scaled to the model's tier, 28 px patches) and `unknown_size` (a header shim could not read) |
| `largest_tool_result` | top level | `tool`, `bytes` and the `requests` that carried it at that size (a later request that carries it shrunk does not count), or `null` |
| `cache_rewrites` | top level | `count`, `tokens` written again, `spend_usd` (or `null`) and the 1-based `requests`; compared within one model and one conversation, so a parallel agent on the same model is not the request before |
| `duplicates_in_flight` | top level | `count` and the `input_tokens` billed again |
| `image_count`, `estimated_image_tokens`, `cache_rewrite`, `duplicate_in_flight` | each of `exchanges` | the same, per request |
| `auth_route` | each of `exchanges` | `"api-key"` (an `x-api-key` header), `"subscription"` (`authorization` and no `x-api-key`), `""` (neither) |
| `response_scan_reason` | each of `exchanges` | `""` when the response was scanned; the request's `incomplete_reason` (`"slots busy"`, `"body too large"`, `"not JSON"`, `"too many fields"`) when it was not measured; otherwise `"unavailable"` or `"partial"` |

**Claude Code only.** Codex is refused with a reason: it reads its endpoint
from its own configuration, so the proxy would be bypassed and the session
measured as empty. The Codex prompt hook is unaffected. Copilot is out of scope
because a custom endpoint there removes GitHub authentication.

Refuses to start if `ANTHROPIC_BASE_URL` is already set, and tells you the two
ways forward.

`shim watch` exits with the client's own exit code, which `--json` also
records as `exit_code`, and with `2` when it refuses or cannot start the
client.

### `shim audit`

What your Claude Code sessions have already put in front of the model, counted
from Claude Code's own history (`~/.claude/projects`, or
`$CLAUDE_CONFIG_DIR/projects`). Nothing is changed, written or sent.

```console
shim audit
shim audit --since 2026-09-01 --project ~/work/kasa-mutabakat
```

```
shim audit — Claude Code history, 3 sessions in 2 projects, 2026-09-28 to 2026-09-30

reached the model
  SECRET          10  in 2 sessions   @.env 5 · failed Bash 5
  EMAIL            3  in 3 sessions   @.env 1 · failed Bash 1 · your prompt 1
  DB_URI           2  in 2 sessions   @.env 1 · failed Bash 1

by project
  ~/work/kasa-mutabakat      SECRET 10, DB_URI 2, EMAIL 2 · last 2026-09-30
  ~/work/site                EMAIL 1 · last 2026-09-29

already masked by shim       3 values in 1 session
model output                 2 EMAIL (written by the model; it may repeat values it was given)

scanned 3 sessions, 5 KB, in 0.0 s. Nothing was changed, written or sent.
```

| Option | What it does |
| --- | --- |
| `--since YYYY-MM-DD` | Skips records before that day. |
| `--project PATH` | Only sessions run in `PATH` or a folder below it. |
| `--json` | The same counts per entity, way in, project and session. |
| `--purge` | Deletes the sessions that sent something, after a typed confirmation. |

It reads the prompts you typed or queued, tool results (a failed call is
`failed <tool>`), files attached with `@`, edited-file snippets, and what the
model wrote (`text` and `thinking`), each counted under the way it came in.
Model output is its own line and never added to the rest. Placeholders shim
already wrote into tool results are counted as `already masked by shim`.
Sub-agent transcripts count under their session; the task prompt a parent agent
wrote into one, compaction summaries and the client's own state records are
skipped. A line over 8 MB or not JSON, and a text the detector cannot analyse,
are counted as skipped and said so. It looks for the types your settings enable
and your custom patterns; a per-tool `[entities]` rule does not apply.

Exits `0` when nothing reached the model, `1` when something did, `2` when the
history is missing or unreadable. `--json` uses the same codes; for a missing
or unreadable history it writes an object with `"status": "error"` and the
reason in `error`.

`--json` carries `sessions`, `projects`, `first`, `last`, `reached` (per
entity: `total`, `sessions`, `doors`), `by_project` (`counts`, `last`),
`by_session` (`project`, `last`, `reached`, `model_output`, `masked`), `masked`
(`values`, `sessions`), `model_output`, and `scanned` (`sessions`, `bytes`,
`seconds`, `skipped_lines`, `uninspected_texts`).

**`--purge`** needs a terminal and cannot be combined with `--json`. It prints
the report, lists the sessions with at least one value under "reached the
model", and asks you to type `delete N`; anything else deletes nothing. It
first removes the listed sessions' lines from `history.jsonl`; if that file
cannot be rewritten, or changes meanwhile, nothing is deleted. Then, for each
session, it deletes the folder of the same name beside the transcript
(sub-agent transcripts and whatever else Claude Code keeps there) and last the
transcript, so a session whose folder cannot be fully deleted keeps its
transcript and a later run finds it again. A session whose files changed during
the run is skipped and named, a link is removed as a link with its target left
alone, and the exit code stays what it was: deleting the local copy does not
change what reached the model provider. Claude Code keeps other files per
session that `--purge` leaves, such as its backups of the files the agent edited
in `file-history/`. Close Claude Code first.

## Changing what is detected

### `shim config`

With no arguments, prints a table of every entity type and its state, the
ledger and diet settings, and the file it all lives in:

```console
$ shim config
Current detection: 12/13 enabled
 Entity           Status
 ───────────────────────
 EMAIL            ON
 ...
Ledger: off    Diet: json
PASS File: /home/you/.config/shim/config.toml
```

**Turning entity types on and off.** Every type is on by default except
`TR_LICENSE_PLATE`, Turkish licence plates, which coding traffic imitates too
often (`[12 GET 200]`, `01 OCT 26`) to look for unasked.

```console
shim config --disable PHONE --yes
shim config --enable PHONE --yes
shim config --only SECRET --only DB_URI --yes   # exactly these two, nothing else
```

The types are `EMAIL`, `PHONE`, `CREDIT_CARD`, `IBAN`, `IP_ADDRESS`,
`MAC_ADDRESS`, `US_SSN`, `TR_NATIONAL_ID`, `TR_VKN`, `TR_LICENSE_PLATE`,
`SECRET`, `DB_URI`, `CUSTOM`. Checksums are verified where they exist, so a
mistyped IBAN or card number is not reported.

```console
shim config --enable TR_LICENSE_PLATE --yes   # look for Turkish plates too
```

A plate is uppercase, or lowercase right after `plaka` or `plate`. HTTP methods
and log words (`GET`, `PUT`, `ERR`, `CPU`, `PID`, `ID`, `RC`, `OK`) are plate
letters only right after `plaka` or `plate`; months, units and currencies never
are. `line 42 E 1234` still reads as a
plate.

**Your own terms.**

```console
shim config --custom 'PROJECT_CODENAME=\bATLAS-[0-9]{4}\b' --yes
shim config --custom-literal 'INTERNAL_HOST=db-core-01' --yes
shim config --remove-custom PROJECT_CODENAME --yes
```

`--custom` takes a regular expression, `--custom-literal` takes text to match
exactly — use the literal form when the value contains regex characters. Names
are `UPPER_CASE` letters, digits and underscores, up to 32 characters. Adding
a pattern turns on `CUSTOM` unless the same command disables it. A name you
already use is replaced, pattern or literal, without a warning; the preview
lists names only.

A pattern that backtracks badly is refused before it is saved:

```
FAIL pattern BAD backtracks on repeated input; simplify it
```

Matches appear as `<CUSTOM_1>`, `<CUSTOM_2>` in the text and are named in the
summary's `custom` line.

**Keeping the last few digits.** Off by default.

```console
shim config --reveal IBAN=4 --yes      # <IBAN_1:1326> instead of <IBAN_1>
shim config --no-reveal IBAN --yes
```

Only `IBAN`, `CREDIT_CARD` and `PHONE` can reveal a tail, at most 4 digits.
Asking for one on any other type is refused — `SECRET` has no meaningful tail
and a partial one would be worth guessing at.

**Other switches.**

```console
shim config --ledger --yes      # keep records past the session; off by default
shim config --no-ledger --yes
shim config --diet --yes        # shrink tool results losslessly; on by default
shim config --no-diet --yes
shim config --reset --yes       # back to defaults; discards every setting
```

`--reset` discards everything in the file, including custom patterns,
`[reveal]` and hand-written `[mode]` and `[entities]`. It fixes a file that
does not parse, not one that is refused: shim will not write over a refused
file either, so that needs the `chmod` under [The settings
file](#the-settings-file). Every change `shim config` saves rewrites
`config.toml` without its comments; your `[mode]` and `[entities]` stay.

## Checking text directly

These four do not touch any client. They are for trying shim out, and for
using it in a pipeline.

### `shim scan`

Reads UTF-8 on stdin and reports what it found, changing nothing.

```console
$ printf 'contact ops@example.com' | shim scan
WARN Sensitive data found (EMAIL: 1).
$ echo $?
1

$ printf 'nothing here' | shim scan
PASS No supported sensitive data found.
```

Exit `1` means it found something and `0` that it found nothing. That makes it
usable as a gate. `--json` writes `"status": "findings"` or `"safe"` and
`counts`, one entry per type, with the same exit codes:

```console
shim scan < config.env && echo "clean"
```

If part of stdin cannot be scanned, `scan` and `redact` print
`Unable to process stdin.` and exit `1`, as they do for input that cannot be
read at all and while shim's settings file is refused or invalid.

### `shim redact`

Same input, but writes the rewritten text to stdout.

```console
$ printf 'key AKIAIOSFODNN7EXAMPLE' | shim redact
key <SECRET_1>
```

shim adds one newline to the end of the output, whatever the input ended with,
so a file that already ends with one comes back from `shim redact < file` with
a blank line at the end.

Exits `0`, or `1` with `Unable to process stdin.` when stdin cannot be read or
scanned in full, or the settings file is refused or invalid.

### `shim keys <file>…`

Lists the variables a `.env` or INI file defines: each name, whether it is set,
and what shim would call its value. Never the value.

```console
$ shim keys .env
.env
  APP_ENV                 set
  DATABASE_URL            set   DB_URI
  AWS_ACCESS_KEY_ID       set   SECRET
  AWS_SECRET_ACCESS_KEY   set   SECRET
  SLACK_BOT_TOKEN         set   SECRET
  LEDGER_API_TOKEN        set   SECRET
  STRIPE_SECRET_KEY       set   SECRET
  SUPPORT_EMAIL           set   EMAIL
  MAX_TOKENS              set
  TOKEN_TTL               set
```

`set` is a non-empty value, `empty` is `KEY=` or `KEY=""`, and `ref` is a value
that is exactly one reference to another variable (`${OTHER_VAR}`), shown
because that text is a name. The third column is the entity the detector
assigns to the value, read with its key, so `DB_PASSWORD=Synthetic-pass-0000`
is a `SECRET` and `REDIS_URL` without credentials is nothing. A value made only
of digits under such a key stays blank, as the detector leaves it. `not
inspected` means the detector could not read that value, for example because
shim's own settings file is refused or invalid, which turns every value into
`not inspected`; `shim config` prints the reason.

Values are read the way dotenv loaders read them, so that no part of one can
show up as a name: `KEY=value`, `export KEY=value`, spaces around `=`; double,
single and backtick quotes, each of which may run over several lines and counts
once, with `\"` kept inside double quotes; a `#` that starts a line, or follows
whitespace after an unquoted value, as a comment, while `KEY=#value` is a value;
blank lines; and INI section headers such as `[default]`, printed as a
sub-heading, so `~/.aws/credentials` reads correctly. In an INI file a line
indented deeper than its key continues that key's value, as Python's
`configparser` reads it, so the tab-indented keys of `~/.gitconfig` are listed
one by one, and a section header goes through the detector before it is
printed, so `[url "https://<SECRET_1>@github.com/"]` keeps a token out of the
report. A private key, from its `-----BEGIN` line to its `-----END` line, is
never read line by line: after `KEY=`, quoted in any way or not at all, it is
that variable's value; on a line of its own it is one `unparsed line N`, so a
`.pem` file reads as one unparsed line per key or certificate (blocks glued onto
one line read as one); in a comment it is skipped with the key lines under it.
A key runs over the lines that look like key material (base64, armor headers,
blank lines) up to its `-----END` line, so a key with no `-----END` line stops at
the first ordinary line, and a line that only mentions `-----BEGIN` hides
nothing. A line that is only key material, such as a WireGuard or Fernet key, is
an unparsed line. Lines end only at `\n`, `\r\n` or `\r`. Only `${NAME}` in an unquoted or double-quoted value is a
`ref`; `$name` or a single-quoted `'${NAME}'` is a value. A line that is none of
these, a quote that never closes, or a header with control characters is
reported as `unparsed line N` and never echoed; reading goes on at the next
line.

`--json` writes `files`, one entry per path with `path` and `variables`: one row
per variable or unparsed line, with `name` (`null` for an unparsed line),
`state` (`set`, `empty`, `ref` or `unparsed`), `entity`, `line`, `section` and
`reference` (the `ref` text, otherwise `null`).

Exits `0` whatever the file holds, because the answer is the list. A path that
is missing, not a regular file, larger than 1 MB or not UTF-8 exits `2` with
one sentence and nothing about its contents. Nothing is written.

### `shim demo <client>`

Runs the detector on a synthetic sentence and shows what it finds and how it
would be masked. The output is the same for every client: it does not show
what your client's hook does with a finding — Codex only reports a prompt, and
a Claude Code prompt is reported, not changed, by default. It installs
nothing, touches no hook and records nothing, so it never appears in `shim
report`. Nothing is read from your machine, your settings included: the input
is fabricated. `<client>` is `claude`, `codex` or `copilot`; VS Code has no
target.

```console
$ shim demo claude
PASS Synthetic local demo detected sensitive data.
Send the synthetic report to <EMAIL_1> using token=<SECRET_1>
```

## The settings file

`~/.config/shim/config.toml`, or `$XDG_CONFIG_HOME/shim/config.toml`. Every key
is optional. `shim config` writes this file for you; edit it by hand when you
want per-tool rules, which the flags do not cover.

The file does not exist until something writes it, though `shim config` prints
its path either way. If it does not exist yet, create it with `shim config
--reset --yes`, which makes the folder `0700` and the file `0600` whatever your
umask, then edit it.

**shim checks the file before reading it**, and refuses it if it is a symlink,
a hard link, not a regular file, owned by someone else, writable by another
user, larger than the inspection limit, or changed while being read. The
message names which check refused it:

```
FAIL Settings at ~/.config/shim/config.toml were refused: target must not be
a symlink.
```

For the ownership and writability cases it also says why: anything that can
rewrite your settings can turn detection off. `shim config` creates the
directory `0700` and the file `0600`. If you point `SHIM_CONFIG` at a shared
location — `/tmp`, a group-writable mount — shim will refuse it.

On Linux, an editor running under umask `002` creates a group-writable file or
folder, which shim refuses in the same way. The fix is `chmod 700
~/.config/shim && chmod 600 ~/.config/shim/config.toml`; `shim config --reset
--yes` does not repair permissions. While the file is refused or invalid, every
prompt is withheld, `shim scan` and `shim redact` print
`Unable to process stdin.`, and `shim keys` shows `not inspected`; `shim config`
prints the reason.

```toml
# Which types to look for. Default: all of them.
enabled_entities = ["EMAIL", "SECRET", "DB_URI", "IBAN", "CUSTOM"]

# Keep records past the end of a session, for 30 days. Default: false.
ledger = false

# Shrink tool results losslessly before the model sees them.
# true, false, or a list naming individual transforms: "json", "whitespace".
diet = true

# Tell the model when a Claude Code tool result reads as instructions.
# "report" (the default) only counts them for the session summary.
markers = "note"

# Keep the last N digits. IBAN, CREDIT_CARD and PHONE only, at most 4.
[reveal]
IBAN = 4

# Your own terms. Names are UPPER_CASE, digits and underscores.
[[custom]]
name = "PROJECT_CODENAME"
pattern = '\bATLAS-[0-9]{4}\b'

# What to do, per direction, per event, or per tool. The first match wins,
# most specific first: tool name, then event name, then direction.
[mode]
user-prompt = "warn"        # report it, send it anyway. The default.
inbound = "enforce"         # mask it before the model sees it. The default.
model-output = "observe"    # count it, change nothing. The default.
Bash = "enforce"            # by tool name
default = "warn"            # fallback for anything unlisted

# Narrow which types apply to one tool or event.
[entities]
Bash = ["SECRET", "DB_URI"]
```

**Modes** are `observe` (count it), `warn` (say so, change nothing) and
`enforce` (mask or block); `model-output` accepts only `observe`.
**Directions** are `user-prompt`, `inbound`, `outbound`, `local-write`,
`executable-text` and `model-output`. Keys use a hyphen: `user-prompt`, not
`user_prompt`.

Neither `shim config` nor `shim doctor` shows `[mode]` or per-tool
`[entities]`, and a `[mode]` key that names no direction, event or tool, such
as a misspelled one, is ignored without a warning. So check an edit by testing
it: with `user-prompt = "enforce"`, a prompt holding `ops@example.com` must come
back `shim blocked this prompt: EMAIL (1).` in Claude Code, Codex or VS Code. (GitHub Copilot CLI
rewrites the prompt under `warn` and `enforce` alike.) An invalid value such as
`"block"` makes shim withhold every prompt (`shim could not inspect this
prompt, so it was withheld.`) until it is fixed.

A per-tool `[entities]` list replaces the full set for that tool: types left
out are neither masked nor reported in that tool's events and reach the model
unchanged.

The defaults are deliberate: your prompt is `warn`, because shim reports what
you typed rather than rewriting it under you; tool results are `enforce`,
because that is content you did not write and did not read; what the model
wrote back is `observe`, because the client has already shown it.

In VS Code, which cannot mask, refusing a tool call before it runs is
`outbound = "enforce"`, and it stays off until you set it. `inbound` covers
results, which VS Code lets shim report but not change, so `inbound =
"enforce"`, already the default, refuses nothing there.

## Environment variables

| Variable | What it does |
| --- | --- |
| `SHIM_CONFIG` | Use this settings file instead of the default path. |
| `XDG_CONFIG_HOME` | Where `shim/config.toml` lives. |
| `XDG_STATE_HOME` | Where the ledger lives. |
| `TMPDIR` | Where session records and withheld prompts are written. |
| `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `COPILOT_HOME` | Where each client keeps its settings; shim follows them, and `shim audit` reads Claude Code's history there. |

## Where shim keeps things

| What | Where |
| --- | --- |
| Settings | `~/.config/shim/config.toml` |
| Ledger, when enabled | `~/.local/state/shim/ledger-YYYY-MM.jsonl`, or under `$XDG_STATE_HOME/shim` |
| Session records | a private directory under your temp dir, `$TMPDIR/shim-session-<uid>`. Claude Code's `SessionEnd` deletes a session's record; Codex, GitHub Copilot CLI and VS Code send no session-end event, so theirs stay until the operating system clears the temp dir |
| Withheld prompts | a private file under your temp dir, `$TMPDIR/shim-redacted-*.txt`, named in the message. Claude Code's `SessionEnd` deletes those older than 24 hours |

Session records and the ledger hold entity **names and counts**, tool names and
timestamps. They never hold the values that were found. What is recorded, and
what is not, is spelled out in [privacy.md](privacy.md).

## See also

- [README](../README.md) — install and the guided tour
- [privacy.md](privacy.md) — what is recorded, what leaves the machine, and the
  limits of a best-effort guard
- [compatibility.md](compatibility.md) — client versions, what was verified
  live, and what was not
- [architecture.md](architecture.md) — how the hook and the proxy are built
