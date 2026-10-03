# Privacy and trust boundary

## What this does and does not prevent

**With the default configuration, shim does not prevent a secret typed
into a Codex or Claude Code prompt from reaching the model.** It detects the
value, reports what it found, and lets the prompt through. Those clients offer
no prompt-rewrite field, so the only available alternative is refusing the
sentence the user just typed. That is the most disruptive thing this product
can do, and it is opt-in (`user-prompt = "enforce"`). Copilot is different:
its verified `userPromptTransformed` event replaces the model-facing prompt
with the typed redaction by default, although the original can remain visible
in Copilot's timeline.

At Claude's verified `PostToolUse` event, the default configuration prevents
detected local data in eligible tool results from entering model context. A
file read, grep, command output, or MCP response is masked in place before the
model sees it. Codex and Copilot currently install no tool-event adapter.

Three things follow, and all three are limits rather than features:

- Masking an outbound tool argument is **egress control**, not model
  protection. The model produced that argument, so it has already seen the
  value; masking stops it leaving the machine.
- `Bash` commands and `Write`/`Edit` content are **never rewritten**. Editing a
  command changes what runs, and editing a write payload puts a placeholder
  into a real file. Both are detected and can be warned about or denied.
- Files attached with `@` are inlined by Claude Code after the prompt hook
  runs, so they cannot be masked. The prompt hook reads them first and warns
  before they are sent, or stops the prompt under `enforce`.

A formatted phone number (`+90 532 123 45 67`, `(555) 123-4567`,
`0212 555 12 34`) is masked. A bare run of digits is a phone number only when it
is Turkish-shaped (`5321234567`, `05321234567`, `905321234567`, `02125551234`)
or when a cue such as `tel`, `phone`, `gsm`, `cep` or `no` sits within 16
characters before it; cues are Turkish and English only. A decimal is never a
phone number. A bare timestamp or id is left as it was and counted on the
summary's `warned` line. Bare numbers in other national
formats without a cue are not detected, for example a CSV column of US numbers
whose header is on another line. A [custom pattern](commands.md#shim-config)
covers that case.

## Data flow

```text
client prompt -> trusted shim hook -> in-memory offline detector
             <- empty success | default Codex/Claude report
                              | Copilot model-facing typed rewrite
                              | enforce-mode native stop + 0600 typed redaction
```

The hook reads the submitted-prompt fields needed for the native contract. It
does not send prompt data to shim and does not create a replacement map. It
does keep a record of its own decisions, described under **What is recorded**
below; that record holds entity names and counts and never the values.
Safe input produces exactly empty stdout and stderr. For a supported Copilot
finding, the hook returns the typed redaction as the model-facing replacement
and writes no file. For Codex and Claude Code, the default `warn` mode reports
the finding and creates no suggestion. Under `user-prompt = "enforce"`, the
hook writes one typed redaction to a `0600` file in the operating system's
temporary directory and returns a tested native block containing its absolute
path in a ready-to-copy instruction to use the file contents as the prompt. A
handled hook error returns the client's generic fail-closed response and leaves
no suggestion file. The raw prompt and detected raw values are not written by
shim.

The temporary redaction remains until the user deletes it, the operating
system cleans temporary storage, or a registered `SessionEnd` hook sweeps SHIM
suggestions older than 24 hours. Claude installs `SessionEnd`; Codex and
Copilot do not. The file can still contain sensitive content the detector
missed, so users must review it before resubmission and delete it when finished
rather than relying on a later sweep.

Users may enable or disable public entity types with `shim config`. The default
preset enables all supported types. The local settings file contains policy
labels and booleans, never prompt-derived data. An invalid or unsafe settings
file causes the hook to return its generic fail-closed response rather than
silently ignoring the policy.

Where a checksum exists it is verified, so a mistyped IBAN or Turkish national
ID is not reported. Detection also stays deliberately quiet on values that name
nobody: the loopback and unspecified addresses (`127.0.0.1`, `0.0.0.0`, `::1`)
and connection strings that carry no credentials, wherever they point. Private
and public IP addresses are still detected. A connection string is masked over
its user and password only, as one placeholder:
`postgresql://<DB_URI_1>@db-prod.kasa.internal:5432/kasa` keeps the host the
agent needs, and a password-like parameter in its query string (`?password=…`)
is a `SECRET` at any length. A connection string's user-info is left alone only
when it holds no credential at all: references such as
`${POSTGRES_USER}:${POSTGRES_PASSWORD}` or `{user}:{password}`, or elision
marks such as `…` and `***`. The user and
password in any `http`, `https`, `ftp`, `ws` or `wss` URL are a `SECRET` too,
so a Sentry DSN reads `https://<SECRET_1>@o123456.ingest.sentry.io/1234567`; a
plain address and a `mailto:` link stay `EMAIL`. This is a precision choice
with a cost attached — a `10.x` address that really is internal topology is
still masked, and a team that wants internal host names hidden adds a [custom
pattern](commands.md#shim-config). The exemptions are written narrowly, so that
they drop noise rather than credentials.

A secret is found by its key name or by its vendor prefix. A key name counts
when a secret word is one of its segments, in any case: `DB_PASSWORD`,
`AWS_SECRET_ACCESS_KEY`, `LEDGER_API_TOKEN`, `client.secret`, `x-api-key`,
`clientSecret`; `MAX_TOKENS`, `tokenizer` and `SORT_KEY` are not secrets. The
prefixes are AWS, GitHub (classic and fine-grained), GitLab, Slack, Google, npm,
Hugging Face, Stripe, OpenAI and SendGrid, plus JSON web tokens, private key
blocks, Slack and Discord webhook URLs, Azure storage `AccountKey` values and
HTTP `Authorization` headers. A
value is not a secret when it only refers to another variable (`${DB_PASS}`,
`os.environ[…]`, `process.env.X`), is a placeholder shim already wrote, or is a
type name (`string`). For a key that is more than the bare word, such as
`TOKEN_TTL` or `DB_PASSWORD_FILE`, a value made only of digits, an `http(s)`
URL or a file path is not a secret either, so configuration stays quiet. Code
that only names a secret stays quiet the same way: under such a key nothing is
a secret when the key's last part is `_id`, `_ids`, `_type`, `_name`,
`_header`, `_count`, `_limit`, `_regex` or `_service` (`token_type`,
`api_key_header`, `tokenService`), and neither is an unquoted value of letters
that reads as a call or lookup, closed or continued by a quote, comma or `]`
(`tokenizer.encode(text)`, `self.auth_token`, `settings["DB_PASSWORD"]`), or,
after a `:`, as a type name that is `String`, or that repeats a word of its key
and ends like code (`apiKey: String`, `authToken: AuthToken;`). A value with a
digit in it, or longer than 1 KB, is never read as code, and a YAML password
such as `adminPassword: BlueHarbor` or `REDIS_PASSWORD: RedisPassword` is
masked. Three things stay undetected: a secret pasted with no key name and no
known prefix; a value made only of digits under such a key, which is the cost
of keeping `TOKEN_TTL=86400` quiet; and an unquoted password of letters that
reads as code, such as `SMTP_PASSWORD=correct.horse.battery`, which is the cost
of keeping code quiet.

## What is recorded

shim keeps a record of what it did, so that a tool which is silent when it
succeeds can still show its work. No entry holds prompt text, tool input or
response bodies, command strings, or detected values. Records keep decisions,
counts, byte measurements, bounded labels, and a scrubbed file path or URL
because those are what make the summary useful. A target is run through the
detector first, so a secret inside a path is masked there too. A shell command
is never kept at all — the probe corpus contains one carrying a live
credential.

One entry per decision:

```json
{"ts": "2026-08-29T14:51:06Z", "session_id": "…", "client": "claude",
 "event": "PostToolUse", "tool_name": "Read", "target": "/work/service/.env",
 "direction": "inbound", "mode": "enforce", "action": "mask",
 "entities": {"SECRET": 2}, "latency_ms": 7, "in_bytes": 812, "out_bytes": 806}
```

The client-supplied session identifier is never stored verbatim. Records and
spool filenames use the same bounded, SHA-256-derived session key.

### While a session is open

Hooks are separate processes, so the record cannot live in memory across
events. It is a file per session under the operating system's temporary
directory, in a directory owned by you and readable by nobody else (`0700`,
with `0600` files). shim refuses to use that directory if it finds it readable
by other users, and `shim doctor` reports when that has happened — recording
never breaks the guard, so without that check the failure would be silent.

The spool is capped at 1 MB; past that the summary undercounts and says so.
Claude installs `Stop` and `SessionEnd`: `Stop` renders only records not shown
in an earlier turn, and `SessionEnd` deletes that session's `.jsonl` spool and
summary marker. Codex and Copilot currently install prompt events only, so they
provide no lifecycle event that deletes a spool; those files remain in OS
temporary storage until the operating system removes them.

`shim report` prints the most recent session's summary. Claude's `Stop` hook
shows the same summary inside that client at the end of a turn where something
changed.

`shim report` reads the most recently active spool. If no spool remains and the
ledger below has entries, it falls back to the newest retained session and says
that is where the numbers came from.

### Past the end of a session — off by default

`shim config --ledger` opts in to keeping the same records after the session
ends. It is off unless you turn it on, and `shim config --no-ledger` turns it
back off. Files live under `$XDG_STATE_HOME/shim` (or `~/.local/state/shim`),
one per month, `0600`, capped at 5 MB each. A 0.2.0 install kept them under
`shim-guard/`; those files are read and moved once, by the first `shim report`,
`shim ledger purge` or `shim install` after the upgrade.

Retention uses whole months. A month becomes eligible for deletion 30 days
after its end, and the next ledger write prunes every eligible file. An entry
therefore has at least 30 days before eligibility, but an inactive ledger can
remain past that point because there is no background process. Age comes from
the file name, not its modification time, so restoring a backup or touching a
file does not reset eligibility.

Turning the ledger off stops new records but does not delete existing files.
`shim ledger purge` deletes every ledger file immediately.

These records are never transmitted. The hook and detector add no network
destination, account, API key, or telemetry. The opt-in `shim watch` proxy
forwards only to the provider the client already uses.

### The last few digits, if you ask for them

`[reveal]` is off by default. Turned on for `IBAN`, `CREDIT_CARD` or `PHONE`,
the placeholder becomes `<IBAN_1:1326>` and **those digits reach the model**,
because that is the point: they say which of three accounts a line refers to.
Nothing else changes — the same spans are found, the same counts are recorded,
and the value itself is still replaced.

Four digits of a mobile number identify a person within a small team, so
`PHONE` is the least conservative of the three; it is allowed, off by default,
and worth a deliberate decision. Session records never quote a placeholder
today, so a revealed tail does not reach the record or the ledger; if that ever
changes, this sentence is the boundary it would cross.

### Patterns you named yourself

A `[[custom]]` entry is configuration, not prompt-derived data, so its **name**
appears where an entity type does: in the session summary, in the session
record, and in the opt-in ledger. The text that matched it never does, exactly
as with a built-in type, and the placeholder is `<CUSTOM_n>` so the name does
not reach the model either. Names are bounded to 32 characters and there are at
most 32 of them.

### Files you attach with `@`

In Claude Code, `@path` in a prompt attaches that file. The client inlines it
while it builds the request, after the prompt hook has run, so no hook can
change it. The prompt hook therefore reads the files a prompt names itself, on
this machine and the way Claude Code resolves them: relative to the session's
folder, with `~` expanded, `@"a name with spaces"` quoted, and `#L10-20`
meaning only those lines. It reads regular text files only: a folder, a device
or a binary file is skipped, and so is a file over 256 KiB, which Claude Code
2.1.286 does not attach. Text that is not valid UTF-8 is read as the client
reads it, with the invalid bytes replaced. At most 8 files and 1 MB in total are
read per prompt. A file past that, a file still unread 15 seconds into the hook,
or one the detector cannot analyse is reported as not inspected, and it never
withholds the prompt. What it keeps is
what it keeps for a prompt: entity names, counts and the scrubbed name of the
file. The file's text is scanned in memory and stored nowhere, and shim still
never reads the session transcript (`transcript_path`).

### What the model wrote back

At `Stop`, Claude Code hands the hook the final assistant text of the turn.
shim counts the entities in it and keeps the counts; the text is scanned in
memory and stored nowhere, exactly as a prompt is. Nothing is changed, because
the client has already shown it — the `model-output` direction can only
observe, and a settings file that asks it to warn or enforce is refused. Text
beyond the detector's 100,000-character limit is not scanned and the record
says `truncated` rather than reporting a short count as a whole one. Only the
turn's last text block reaches the hook, so anything the model said before a
tool call in the same turn is not counted. The summary's `model` line calls
these values written by the model, not invented: it may repeat a value it was
given, from a file you attached for example.

### When shim cannot inspect something

Some payloads cannot be scanned: a tool result past the size bound, or an
analysis that fails. What happens next depends on which side it is, and the
asymmetry is deliberate.

On a **prompt**, shim fails closed. The prompt is withheld and the message
names `shim doctor`, because the usual cause is a settings file that will not
parse, and that blocks every prompt of the session until it is fixed. A
prompt longer than 100,000 characters is scanned in pieces, and one piece that
cannot be scanned withholds the whole prompt. A file attached with `@` is the
exception: it is reported as not inspected, and the prompt goes through.

On a **tool event**, uninspectable content passes through unchanged. Validated
redactions in independently rewritable sibling fields are preserved according
to policy. The client and session summary explicitly report incomplete
inspection, with bounded reason codes and skipped field or subtree counts.
Commands and local writes are never rewritten. If no inspection is possible,
the event passes through unchanged and unmasked with a visible warning; prompt
errors still fail closed.

**The output of a failed tool call cannot be masked.** Claude Code delivers
it, including everything the command printed before it failed, to
`PostToolUseFailure`, and ignores a replacement there: a returned
`updatedToolOutput` was not used (Claude Code 2.1.278 and 2.1.284), while
`additionalContext` reached the model (2.1.284 and 2.1.286). So shim inspects
it, shows you `shim: found DB_URI (1), SECRET (4) in a failed Bash. Claude Code
does not let this output be masked; the model has these values.`, tells the
model not to repeat those values in replies, files or commands, and counts them
on the summary's `unmasked` line. `cat .env && cat missing-file` exits 1 and
the model still reads `.env` as it is; the record keeps entity names and counts,
never the output or the command.

**A large field is scanned in pieces, and the seams are the residual risk.**
The detector works on at most 100,000 characters at a time. A longer field is
cut at the last newline before each boundary and each piece scanned separately,
with placeholder numbering continuing across them, so a 400 KB file read comes
back masked rather than passing through whole. A single line longer than the
limit is cut where it must be, and the next piece starts 4,096 characters
earlier, so a value on one line up to that length is read whole wherever the
cut falls. A value written across a line break — a PEM block, a wrapped key —
can still fall in a seam and go unreported. A piece that grows past the
detector's limit when it is normalized, as Korean text does, is cut in half and
scanned again, down to pieces of 25,000 characters. If one piece fails, the
others are still masked and the summary counts the event as partially
inspected. When the hook's own 25-second deadline runs out in the middle of a
field, that field and every later one pass through unmasked, and the summary
says the event was only partly inspected.

A field so large that the whole event exceeds shim's 1 MB input bound is not
scanned at all. It passes through unchanged and is now counted in the session
summary by tool and file, so a skipped read is visible rather than absent.

## What is changed on the way in

When Claude Code's `PostToolUse` result is masked, the model is told so in the
same hook output, beside the masked result and never on your screen:

```text
shim: masked EMAIL (1), SECRET (1) in Read. Placeholders such as <EMAIL_1> stand for real values in the source; the source does not contain placeholders.
```

It carries entity names, their counts and the tool name, nothing else. Codex
and Copilot do not mask tool results, and a masked tool argument gets no such
sentence.

At Claude's verified result event, shim can also compact tool results so they
take less of the model's context. Every transform is deterministic and
idempotent, because the provider's prompt cache only hits if the history is
byte-identical on every request, so a transform that drifts costs money instead
of saving it.

Two transforms ship. Only the lossless `json` transform is enabled by default.
`whitespace` is opt in because it strips trailing spaces and tabs from every
line, removing a Markdown hard line break since that break *is* two trailing
spaces. It never changes a line count.

JSON is compacted by a lexer rather than by parsing and re-serialising:
re-serialising rewrites number literals (`1.10` becomes `1.1`, a long decimal
loses digits to a float) and drops duplicate keys, which changes what the model
reads. Only the whitespace between tokens is removed. Line-numbered results
keep their line count, so blank lines are never collapsed. Nothing is ever
truncated or summarised.

Diet applies to tool **results** only — never a tool's arguments, never a local
write, and never under `mode = "observe"`. It also stops at any result that is
a *view of a file*: the model reproduces those bytes to edit the file, and
`Edit` matches `old_string` against what is on disk rather than against what
the model was shown, so reshaping a file on the way in makes the next edit of
it miss. `shim config --no-diet` turns it off. To opt in to whitespace while
keeping JSON, set `diet = ["json", "whitespace"]`.

shim also flags text in a result that reads as an instruction to the model.
Invisible-character detection covers the zero-width space, the invisible
operators, the bidi *overrides* behind Trojan Source, and the Unicode tag block
used to smuggle whole instructions past a human reviewer. It deliberately
ignores characters that render as nothing but have ordinary uses — the byte
order mark, the zero-width joiner inside emoji, the zero-width non-joiner that
Persian and Hindi orthography require, and the plain bidi marks — because a
marker the user learns to ignore protects nobody.

Those markers are **reported and never acted upon**, and they are not entities:
no setting can turn one into a rewrite, because rewriting a result for looking
imperative would corrupt legitimate content — a code review, a style guide, or
documentation about prompt injection.

## Outside shim's boundary

The host client receives the raw prompt. Matching hooks can start concurrently,
so shim cannot stop another matching hook from receiving it. Clients,
operating-system tools, plugins, and providers can retain logs, transcripts,
telemetry, caches, or history independently of shim.

Under `enforce`, Claude Code 2.1.286 still wrote a blocked prompt's text to its
own transcript and gave the session a title derived from it; the content of a
file attached to that prompt was not written.

Copilot's `userPromptTransformed` replacement changes what is sent to the model
and stored in session history, but the original prompt can remain visible in
Copilot's timeline.

Some clients require review and trust for non-managed hooks. A hook can be
disabled, untrusted after a change, missing, unable to start, crash, or time
out; those outcomes are client-controlled and may fail open. shim does not
promise detection of every value, inspection of automatic context, or secure
erasure of Python process memory. Only events listed by `shim doctor <client>`
are inspected; anything reaching the model by another route is outside that
list.

Installer checks detect unsafe paths and observed drift, but they are not an
isolation boundary against a malicious process already running as the same OS
user. Such a process has equivalent authority over user-scoped client settings
and can race POSIX pathname operations despite advisory locking.

Review every temporary redaction before resubmission. The detector can miss
sensitive content.


## `shim watch`

The proxy sees the whole wire body — the system prompt, the tools array, the
full message history and every file the client inlined for an `@` reference.
None of it is kept.

Every text field of that body is offered to the detector: message content in
either form, the text inside a tool result, the arguments of a tool call, the
system prompt and the tool definitions. Two kinds of field are skipped because
they are opaque rather than prose — the `data` of a base64 attachment and a
thinking block's `signature`. Findings are attributed to the section they came
from, so the summary can say what was in your prompt separately from what the
client's own scaffolding carried.

The tools and system sections repeat verbatim on every request of a session, so
their result is remembered for the length of the run: the SHA-256 of the
section and the entity counts it produced, at most sixteen of them, oldest
evicted first. The remembered value is a hash and a tally; the text that
produced it is not kept.

The response is scanned too, and on the same terms. Its text and `thinking`
blocks are held in memory for the length of one response, up to 1 MB, scanned
only after the last byte has been relayed to the client, and discarded before
the request returns. `thinking` is counted separately from the answer, because
a value the model reasoned about is not the same fact as one it wrote down.
The arguments of a tool call are not counted here; the hook already scans them
at `PreToolUse`. Findings on this side are counted, not judged: the model wrote
them, so they are not called a leak.

What survives one request is a count and a size: bytes per section, entity
counts by type and by section, response counts by kind, the provider's stop
reason, a token count from the provider, the model name and the request path. **No request or response body is ever written to disk**, and
`tests/watch/test_proxy.py` asserts it by sending a unique marker through the
proxy and then searching every file written anywhere beneath the temporary root
for it.

To label the `spend` line, the proxy reads the *names* of the request headers
it already forwards: `x-api-key` means an API key, `authorization` without it
means a subscription. It stores the route it concluded and neither header's
value; `tests/watch/test_proxy.py` checks that no string kept on the exchange
equals either value.

The proxy binds to loopback only. It is forwarding a live credential, and
binding to anything reachable would hand that credential to the network. It
lives for the length of one `shim watch` command, edits no shell profile, and
changes no setting; the client is given a base URL in its own environment and
nothing else.

Nothing is transmitted anywhere except to the provider the client was already
talking to. There is still no telemetry and no account.
