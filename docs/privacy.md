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
- Files referenced with `@` bypass hooks entirely, so nothing here sees them.

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
and connection strings to them that carry no credentials. Private ranges, real
hosts, and any URI with a `user:password@` are still detected. This is a
precision choice with a cost attached — a `10.x` address that really is
internal topology is still masked, and the exemption is written narrowly so it
can never be a way to smuggle a credential past.

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

### When shim cannot inspect something

Some payloads cannot be scanned: a tool result past the size bound, or an
analysis that fails. What happens next depends on which side it is, and the
asymmetry is deliberate.

On a **prompt**, shim fails closed. The prompt is withheld and the message
names `shim doctor`, because the usual cause is a settings file that will not
parse, and that blocks every prompt of the session until it is fixed.

On a **tool event**, uninspectable content passes through unchanged. Validated
redactions in independently rewritable sibling fields are preserved according
to policy. The client and session summary explicitly report incomplete
inspection, with bounded reason codes and skipped field or subtree counts.
Commands and local writes are never rewritten. If no inspection is possible,
the event passes through unchanged and unmasked with a visible warning; prompt
errors still fail closed.

## What is changed on the way in

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

What survives one request is a count and a size: bytes per section, entity
counts by type and by section, a token count from the provider, the model name
and the request path. **No request or response body is ever written to disk**, and
`tests/watch/test_proxy.py` asserts it by sending a unique marker through the
proxy and then searching every file written anywhere beneath the temporary root
for it.

The proxy binds to loopback only. It is forwarding a live credential, and
binding to anything reachable would hand that credential to the network. It
lives for the length of one `shim watch` command, edits no shell profile, and
changes no setting; the client is given a base URL in its own environment and
nothing else.

Nothing is transmitted anywhere except to the provider the client was already
talking to. There is still no telemetry and no account.
