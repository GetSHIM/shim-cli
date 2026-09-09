# Command reference

Every command shim has, what it prints, and what it exits with. The README is
the tour; this is the map.

Everything here runs locally. No command in this document sends anything
anywhere, with one exception that is marked as such: `shim watch` forwards your
client's traffic to the provider your client was already going to talk to.

## Contents

- [Conventions](#conventions) — flags and exit codes shared by every command
- [Setting up](#setting-up) — `install`, `status`, `doctor`, `revert`, `update`
- [Seeing what happened](#seeing-what-happened) — `report`, `ledger`, `watch`
- [Changing what is detected](#changing-what-is-detected) — `config`
- [Checking text directly](#checking-text-directly) — `scan`, `redact`, `demo`
- [The settings file](#the-settings-file) — every key in `config.toml`
- [Environment variables](#environment-variables)
- [Where shim keeps things](#where-shim-keeps-things)

## Conventions

**`--json`** is available on most commands and writes one JSON object to
stdout instead of the human table. Use it in scripts; the text layout is not a
stable interface and the JSON is.

**`--yes`** skips the confirmation prompt on the commands that change a file
(`install`, `revert`, `config`). Without it you are shown what will happen and
asked.

**`--help`** works on every command, and `shim help` is the same as
`shim --help`. Each command's help repeats its own options, so this document is
the thing to read when you want the whole surface at once.

**Exit codes** are the same everywhere:

| Code | Meaning |
| --- | --- |
| `0` | Fine. For `doctor`, this includes warnings — a healthy install prints two. |
| `1` | Nothing to show, or a finding. `report` with no session, `status` when no hook is installed, `scan` when it found something. |
| `2` | Refused. Bad input, an unsafe or unreadable file, a client shim cannot support. |

`scan` follows grep: exit `1` means *it found something*, which is what makes
`shim scan < file || echo "clean"` work in CI. `redact` always exits `0`,
because its answer is the rewritten text.

## Setting up

### `shim install <client>`

Writes shim's hook into the client's own settings file. Clients are `claude`,
`codex`, `copilot`.

```console
shim install claude              # shows the change, asks, then writes
shim install claude --yes        # writes without asking
shim install claude --dry-run    # shows the change and exits
```

`--dry-run` prints a sentence and then the exact JSON fragment:

```
WARN Would add 5 hook entries (UserPromptSubmit, PostToolUse, PreToolUse,
SessionEnd, Stop), each running /usr/bin/python3 -m shim_cli.hook claude.
Nothing else in the file changes.
```

Installing is additive and surgical: your other hooks stay, and shim appends
itself last. If a 0.2.0-shaped fragment is present it is replaced rather than
duplicated, and the output says so.

**Codex needs one extra step.** From 0.151.0 Codex will not run a hook until
you trust it, and it does so silently — no warning, no transcript line. After
installing, open Codex and accept the shim hook. `shim install codex` reminds
you; `shim doctor codex` cannot verify it, because that record lives in the
client where shim cannot read it.

If you installed the marketplace plugin instead, you do not need this command
at all. Running both puts two hook paths on disk and `shim doctor` will
say so.

### `shim status <client>`

One line: is shim's hook in that client's settings file?

Exits `0` when installed, `1` when not. `--json` gives `{"state": "installed"}`
or `"not_installed"`, which is the form to use in a script.

### `shim doctor <client>`

The command to run when something looks wrong. It checks the client version
against what was tested, whether the hook group is present and exactly once,
whether anything from 0.2.0 is still on disk, whether your settings parse,
whether session records can be written, which interpreter will actually run,
and which events are covered.

```console
shim doctor claude
```

A healthy install ends with a coverage table and two warnings — that your
client is newer than the version shim was tested against, and that hook
activation is client UI state shim cannot read. **Both are normal and doctor
exits `0`.** A `FAIL` exits `2`.

Every `FAIL` names the command that fixes it. A malformed settings file, for
example, gives you the path, the parser's message with its line number, and
`shim config --reset`.

### `shim revert <client>`

Removes shim's hook group and leaves everything else in the file alone —
other hooks, permissions, unrelated keys. For Copilot it also deletes the hook
file, which belongs to shim alone.

```console
shim revert claude --yes
```

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
  model     1 EMAIL in its replies (model-generated content, not leaks)
  overhead  102 ms median, 120 ms p95
```

`masked` is what the model did not see. `warned` is what shim found in your
prompt and left alone — prompts are reported, not rewritten. `model` counts
what the model itself wrote back; it is recall, not a leak, and is labelled
that way.

Exits `1` when there is no session to show.

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

The report prints when the client exits:

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
  compare   EMAIL   3 in request, 2 in response (text, thinking)
  spend     ~$0.69  (approximate, 2026-08-30 prices)
  nothing was modified, and no request body was written to disk
```

`(exact)` means the provider reported that number. `(approximate)` means shim
attributed it by byte share, and the `~` is there to keep you honest about it.
If you sign in with a subscription, `spend` is what the same traffic would cost
on an API key, not a bill.

**Claude Code only.** Codex is refused with a reason: it reads its endpoint
from its own configuration, so the proxy would be bypassed and the session
measured as empty. The Codex prompt hook is unaffected. Copilot is out of scope
because a custom endpoint there removes GitHub authentication.

Refuses to start if `ANTHROPIC_BASE_URL` is already set, and tells you the two
ways forward.

## Changing what is detected

### `shim config`

With no arguments, prints a table of every entity type and its state, the
ledger and diet settings, and the file it all lives in:

```console
$ shim config
Current detection: 12/12 enabled
 Entity           Status
 ───────────────────────
 EMAIL            ON
 ...
Ledger: off    Diet: json
PASS File: /home/you/.config/shim/config.toml
```

**Turning entity types on and off.** Every type is on by default.

```console
shim config --disable PHONE --yes
shim config --enable PHONE --yes
shim config --only SECRET --only DB_URI --yes   # exactly these two, nothing else
```

The types are `EMAIL`, `PHONE`, `CREDIT_CARD`, `IBAN`, `IP_ADDRESS`,
`MAC_ADDRESS`, `US_SSN`, `TR_NATIONAL_ID`, `TR_VKN`, `SECRET`, `DB_URI`,
`CUSTOM`. Checksums are verified where they exist, so a mistyped IBAN or card
number is not reported.

**Your own terms.**

```console
shim config --custom 'PROJECT_CODENAME=\bATLAS-[0-9]{4}\b' --yes
shim config --custom-literal 'INTERNAL_HOST=db-core-01' --yes
shim config --remove-custom PROJECT_CODENAME --yes
```

`--custom` takes a regular expression, `--custom-literal` takes text to match
exactly — use the literal form when the value contains regex characters. Names
are `UPPER_CASE` letters, digits and underscores, up to 32 characters.

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
shim config --reset --yes       # back to defaults; the fix for a broken file
```

## Checking text directly

These three do not touch any client. They are for trying shim out, and for
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

Exit `1` means it found something. That makes it usable as a gate:

```console
shim scan < config.env || echo "clean"
```

### `shim redact`

Same input, but writes the rewritten text to stdout.

```console
$ printf 'key AKIAIOSFODNN7EXAMPLE' | shim redact
key <SECRET_1>
```

Always exits `0`.

### `shim demo <client>`

Runs a synthetic detector check for that client and prints what a hook would
do. Nothing is installed and nothing is read from your machine — the input is
fabricated. Use it to see the shape of the output before installing.

```console
$ shim demo claude
PASS Synthetic local demo detected sensitive data.
Send the synthetic report to <EMAIL_1> using token=<SECRET_1>
```

## The settings file

`~/.config/shim/config.toml`, or `$XDG_CONFIG_HOME/shim/config.toml`. Every key
is optional. `shim config` writes this file for you; edit it by hand when you
want per-tool rules, which the flags do not cover.

**The file and its directory must be private.** shim refuses to read settings
from a world-writable directory, because anything that can rewrite your
settings can turn detection off. `shim config` creates the directory `0700` and
the file `0600`. If you point `SHIM_CONFIG` somewhere else — `/tmp`, a shared
mount — shim will refuse it.

```toml
# Which types to look for. Default: all of them.
enabled_entities = ["EMAIL", "SECRET", "DB_URI"]

# Keep records past the end of a session, for 30 days. Default: false.
ledger = false

# Shrink tool results losslessly before the model sees them.
# true, false, or a list naming individual transforms: "json", "whitespace".
diet = true

# Keep the last N digits. IBAN, CREDIT_CARD and PHONE only, at most 4.
[reveal]
IBAN = 4

# Your own terms. Names are UPPER_CASE, digits and underscores.
[custom]
PROJECT_CODENAME = '\bATLAS-[0-9]{4}\b'

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
`enforce` (mask or block). **Directions** are `user-prompt`, `inbound`,
`outbound`, `local-write`, `executable-text` and `model-output`.

The defaults are deliberate: your prompt is `warn`, because shim reports what
you typed rather than rewriting it under you; tool results are `enforce`,
because that is content you did not write and did not read; what the model
wrote back is `observe`, because it is not a leak.

## Environment variables

| Variable | What it does |
| --- | --- |
| `SHIM_CONFIG` | Use this settings file instead of the default path. |
| `XDG_CONFIG_HOME` | Where `shim/config.toml` lives. |
| `XDG_STATE_HOME` | Where the ledger lives. |
| `SHIM_GUARD_STATE_DIR` | Pin the ledger directory outright. |
| `SHIM_GUARD_SESSION_DIR` | Pin the session spool directory. |
| `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `COPILOT_HOME` | Where each client keeps its settings; shim follows them. |

`SHIM_GUARD_CONFIG` is the 0.2.0 name for `SHIM_CONFIG` and still works. It is
removed in 0.5.0.

## Where shim keeps things

| What | Where |
| --- | --- |
| Settings | `~/.config/shim/config.toml` |
| Ledger, when enabled | `~/.local/state/shim/ledger-YYYY-MM.jsonl` |
| Session records | a private directory under your temp dir, cleared on reboot |
| Withheld prompts | a private file under your temp dir, named in the message |

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
