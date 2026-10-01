# Command reference

Every command shim has, what it prints, and what it exits with. The README is
the tour, this is the map, and the [cookbook](cookbook.md) is the recipes.

Everything here runs locally. No command in this document sends anything
anywhere, with one exception that is marked as such: `shim watch` forwards your
client's traffic to the provider your client was already going to talk to.

## Contents

- [Conventions](#conventions) — flags and exit codes shared by every command
- [Setting up](#setting-up) — `install`, `status`, `doctor`, `revert`, `update`
- [Seeing what happened](#seeing-what-happened) — `report`, `ledger`, `watch`, `audit`
- [Changing what is detected](#changing-what-is-detected) — `config`
- [Checking text directly](#checking-text-directly) — `scan`, `redact`, `keys`, `demo`
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

`scan` exits `1` when it found something and `0` when it found nothing, the
reverse of grep, so `shim scan < file && echo "clean"` works in CI. `redact`
exits `0`, because its answer is the rewritten text, unless stdin cannot be read
or scanned in full: then both print `Unable to process stdin.` and exit `1`.

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
WARN Would add 6 hook entries (UserPromptSubmit, PostToolUse,
PostToolUseFailure, PreToolUse, SessionEnd, Stop), each running
/usr/bin/python3 -m shim_cli.hook claude. Nothing else in the file changes.
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

A healthy install ends with a coverage table and a warning that hook activation
is client UI state shim cannot read, plus a second one when your client is newer
than the version shim was tested against. **Both are normal and doctor exits
`0`.** A `FAIL` exits `2`.

The coverage line counts the events whose hook is in the client's settings
file, and the table's `Installed` column reads the same file:
`PASS Coverage: 6 of 6 events installed.` once shim is installed, and
`WARN Coverage: 0 of 6 events installed; run shim install claude.` before. An
install made by 1.0.2 or earlier reads `5 of 6`, because `PostToolUseFailure`
is new; `shim install claude` adds it and leaves the other five alone. A
hook still in the 0.2.0 shape does not count, because 1.0 does not run it:
doctor reports it as `FAIL` with `run shim install <client>`. The coverage
`WARN` on its own exits `0`.

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
  response  1 EMAIL in model text; 1 EMAIL in thinking
            written by the model; it may repeat values it was given
  compare   EMAIL   3 in request, 2 in response (text, thinking)
            DB_URI  3 in request, 0 in response
  spend     ~$0.69  (approximate, 2026-08-30 prices)
  largest   one request was 112,384 bytes, system 61% of it
  nothing was modified, and no request body was written to disk
```

`(exact)` means the provider reported that number. `(approximate)` means shim
attributed it by byte share, and the `~` is there to keep you honest about it.
If you sign in with a subscription, `spend` is what the same traffic would cost
on an API key, not a bill, and the line says so. When some requests arrived
while both inspection slots were busy, the section header adds `2 of 4 requests
measured` and the response line gives the same reason as the inspection line.

`--json` writes the same report as one object. Three of its fields say what a
figure covers:

| Field | Where | Values |
| --- | --- | --- |
| `spend_basis` | top level | `"api-key"`, `"subscription"`, `"mixed"`, or `"unknown"` when a priced request sent neither auth header or nothing was priced |
| `auth_route` | each of `exchanges` | `"api-key"` (an `x-api-key` header), `"subscription"` (`authorization` and no `x-api-key`), `""` (neither) |
| `response_scan_reason` | each of `exchanges` | `""` when the response was scanned; the request's `incomplete_reason` (`"slots busy"`, `"body too large"`, `"not JSON"`, `"too many fields"`) when it was not measured; otherwise `"unavailable"` or `"partial"` |

**Claude Code only.** Codex is refused with a reason: it reads its endpoint
from its own configuration, so the proxy would be bypassed and the session
measured as empty. The Codex prompt hook is unaffected. Copilot is out of scope
because a custom endpoint there removes GitHub authentication.

Refuses to start if `ANTHROPIC_BASE_URL` is already set, and tells you the two
ways forward.

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
are counted as skipped and said so.

Exits `0` when nothing reached the model, `1` when something did, `2` when the
history is missing or unreadable.

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
are `UPPER_CASE` letters, digits and underscores, up to 32 characters. Adding
a pattern turns on `CUSTOM` unless the same command disables it.

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
usable as a gate:

```console
shim scan < config.env && echo "clean"
```

If part of stdin cannot be scanned, `scan` and `redact` print
`Unable to process stdin.` and exit `1`, as they do for input that cannot be
read at all.

### `shim redact`

Same input, but writes the rewritten text to stdout.

```console
$ printf 'key AKIAIOSFODNN7EXAMPLE' | shim redact
key <SECRET_1>
```

Exits `0`, or `1` with `Unable to process stdin.` when stdin cannot be read or
scanned in full.

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
shim's own settings cannot be read.

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

```toml
# Which types to look for. Default: all of them.
enabled_entities = ["EMAIL", "SECRET", "DB_URI", "IBAN", "CUSTOM"]

# Keep records past the end of a session, for 30 days. Default: false.
ledger = false

# Shrink tool results losslessly before the model sees them.
# true, false, or a list naming individual transforms: "json", "whitespace".
diet = true

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
`enforce` (mask or block). **Directions** are `user-prompt`, `inbound`,
`outbound`, `local-write`, `executable-text` and `model-output`.

The defaults are deliberate: your prompt is `warn`, because shim reports what
you typed rather than rewriting it under you; tool results are `enforce`,
because that is content you did not write and did not read; what the model
wrote back is `observe`, because the client has already shown it.

## Environment variables

| Variable | What it does |
| --- | --- |
| `SHIM_CONFIG` | Use this settings file instead of the default path. |
| `XDG_CONFIG_HOME` | Where `shim/config.toml` lives. |
| `XDG_STATE_HOME` | Where the ledger lives. |
| `TMPDIR` | Where session records and withheld prompts are written. |
| `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `COPILOT_HOME` | Where each client keeps its settings; shim follows them. |

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
