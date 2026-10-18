# Architecture

shim-cli is one Python distribution and one domain-sliced modular monolith.
It has two independent paths between a coding agent and its model:

- The synchronous **hook** receives native client events, applies local policy,
  and can report, block, or rewrite only where that client has a verified native
  response shape. The hook and detector make no network connection.
- Opt-in **`shim watch`** runs a loopback forwarding proxy for one command. It
  sends the client's existing requests only to that client's configured
  provider, forwards bytes unchanged, and measures traffic the hooks cannot see.

There is no daemon or remote shim service. Hooks may write a private session
spool, and the user may opt into a retained ledger; those stores are described
in [Privacy](privacy.md).

## Runtime flow

```text
shim config -> guarded TOML -> Policy
shim scan/redact (stdin) -> detector -> counts or typed redaction
shim install/status/doctor/revert -> client settings + guarded file boundary
shim report / shim ledger purge -> session-owned records
shim audit -> Claude Code transcripts -> detector -> counts; --purge deletes sessions
shim keys <file> -> .env/INI parser -> detector -> names, states, entity types

prompt event -> client codec (+ Claude @ files read from disk) -> Policy -> detector
             -> allow/report | Copilot rewrite | native block + 0600 suggestion

verified tool event -> Claude or VS Code adapter -> shared event pipeline
                    -> direction (client, tool) -> self-protection
                    -> Policy -> detector, diet, markers
                    -> client response + session Record
                       (not inspected in full: deny, withhold or report)

shim watch -- <client> -> loopback proxy -> configured provider, bytes unchanged
                       -> in-memory sizes, counts, usage, and report
```

A payload's direction determines what may be done to it. `policy.py` owns that
classification and the modes and actions built on it:

- user prompts are reported by default; Codex and Claude can block under
  `enforce`, while Copilot can replace the model-facing prompt;
- the arguments of tools known to send them over the network (`WebFetch`,
  `WebSearch`, VS Code `fetch_webpage`) may be masked before leaving the
  machine; an MCP tool's or an unknown tool's arguments are never rewritten,
  because their destination is unknown, and `enforce` refuses such a call
  instead;
- inbound results may be masked before entering model context;
- local writes and executable command text are never rewritten, because doing
  so would change a file or command the user intended;
- a local tool's input (`Read`, `Grep`, `Glob`, a sub-agent's task) is never
  rewritten or refused, because it names something on this machine and
  rewriting it only breaks the call.

The tool names behind each direction are per client, in `policy.TOOLS`; a name
not there is treated as an unknown tool.

## Ownership and dependencies

| Owner | Responsibility |
| --- | --- |
| `guard/` | Entity catalog, normalization, recognizers, findings, spans, and typed redaction. |
| `policy.py` | Directions, modes, actions, defaults, `Policy`, per-client tool classification (`TOOLS`), action selection, and reading which `shim` subcommand a shell command runs. |
| `config.py` | Config path, TOML parsing/rendering, validation at file ingress, and loading a `Policy`. |
| `events/` | Deterministic tool-payload traversal, context diet (`html_text` turns a whole HTML page into text when `html` is on), injection markers, and the shared tool-event pipeline. |
| `clients/<client>/` | Native prompt codecs, verified tool codecs, settings fragments, capabilities, and coexistence rules for that client. |
| `session/` | `Record`, timestamps, best-effort `remember()`, spool, ledger, cleanup, and summaries. |
| `settings_files/` | Guarded inspection, pure change planning, parent creation, revalidation, and atomic publication. |
| `watch/` | Proxy forwarding, wire measurement, and watch report values. |
| `hook.py` | Deadline, bounded stdin, envelope dispatch, fail-closed answers for events it cannot inspect (including fixed fallbacks for when the adapters cannot load), which files a write may not change, temporary prompt-file lifecycle, and stdout. |
| `cli/` | Command composition and human or JSON presentation, including findings in the gateway's schema v1 (`cli/findings.py`). |

The dependency direction is deliberately small:

```text
cli  ------> config, clients, session, settings_files, watch, guard,
             events/diet, events/injection (names only)
hook ------> config, clients, events, session, guard
config ----> policy, guard/entities, settings_files, events/diet
clients ---> policy, events, session, settings_files
events ----> policy, guard, session/record
watch -----> events/payload
```

`guard`, `policy`, `session`, `settings_files`, and `watch` do not depend on
the CLI or hook. `watch` reuses the hook's bounded string traversal and nothing
else from it. `guard` does not import configuration, `session` does not
import events, events do not import clients, and the hook does not import
`watch`. `tests/contracts/test_import_hygiene.py` enforces these boundaries.
Composition stays explicit in `hook.py` and the CLI; there is no dependency
container, dynamic adapter registry, or architecture framework.

## Client protocols

Each client directory owns its native protocol. Prompt hooks are supported for
all three clients:

| Client | Installed prompt event | Verified installed tool events |
| --- | --- | --- |
| Claude Code | `UserPromptSubmit` | `PreToolUse`, `PostToolUse`, `PostToolUseFailure` (report only) |
| Codex CLI | `UserPromptSubmit` | None |
| GitHub Copilot CLI | `userPromptTransformed` | None |

Claude's verified event list and encoders live together in
`clients/claude/tool_events.py`; its settings also install `Stop` for summaries
and `SessionEnd` for cleanup. A masked `PostToolUse` result is emitted as
`updatedToolOutput` with `additionalContext` beside it, the one-line summary of
what was masked for the model, and a reshaped one says so there too; a masked
`PreToolUse` input carries a `systemMessage` for the user, because the call
runs with input the model did not write. An event the pipeline could not read
in full is answered with the `uninspected` encoding: a call is denied, an MCP
result is replaced whole, and a built-in tool's result keeps the shape of its
output schema (keys, one item per list, zeroed numbers, the words under the
schema's enum keys in `_KIND_KEYS`) with every other string replaced, since
Claude Code 2.1.286 sends the original when a built-in replacement does not
match that schema. `PostToolUseFailure` is report-only: the
client ignores a replacement there, so the pipeline caps its action at a report,
and its encoder tells the user and, through `additionalContext`, the model; the
summary counts it as `unmasked`. Codex and Copilot have prompt codecs and
settings only. A new tool adapter requires a live protocol probe, a synthetic fixture,
and a verified mutation or report channel; it is not enabled by a flag.

Codex installation leaves inline `config.toml` hooks untouched. A hook is
shim's when its handler is exactly what a release wrote, whatever the
interpreter (`clients/hook_settings.py`); installation leaves one current shim
group per event, replacing any other installation's, and revert removes every
shim group, while a hook that merely wraps shim's command is the user's.
Copilot owns its dedicated `hooks/shim.json` and retains an empty
versioned document on revert. All clients preserve unrelated settings;
malformed, unsafe, or concurrently changed files, and a group where shim's
handler sits beside another hook, require manual action. Install and status run
the exact registered command on a safe and a sensitive fixture first.

## Detector and event processing

The detector is deterministic, bounded, offline, and independent of CLI,
configuration, clients, events, sessions, and watch. Recognizers and checksums
live in `guard/recognizers.py`; the public suffix table is compiled into
`guard/suffixes.py`. `phonenumbers` is the one third-party module allowed on
the hook path. No detector input or lookup is sent over the network.

`events/payload.inspect` walks a tool payload once, to a depth of 64 and with
no field count, the 1 MB input bound being what bounds the work. Masking and
context diet may rewrite eligible leaves; injection markers only report and can
never reach a replacement. If any leaf was not read, because of depth, size,
the deadline or a failed analysis, the inspection hands on no rewritten value
at all, and the event is denied, withheld or reported as not inspected. Before
every detector call the tool path compares the clock with the deadline `main`
set before it read stdin, rather than relying on the alarm the detector
re-arms. Diet runs only on rewrite-capable inbound results of network and MCP
tools outside `observe`: never on a local tool's result or a command's output,
whose exact bytes a later edit may need. Lossless JSON compaction is the default;
trailing-whitespace removal is available only through explicit configuration
because it can change Markdown hard breaks, and so is HTML-to-text, which drops
markup; its parser, stdlib `html.parser`, is imported only when `html` is
enabled.

Twelve entity types are built in; `TR_LICENSE_PLATE` is opt-in
(`OPT_IN_TYPES` in `guard/entities.py`), so the default set leaves it out and
every default that is not the user's own settings uses `DEFAULT_ENTITIES`.
The thirteenth, `CUSTOM`, is whatever the user
named in `[[custom]]`: `guard/entities.py` compiles those entries, the hook and
the proxy pass the compiled tuple to `evaluate`, and a `Finding` carries the
pattern's name as its `label` so the report can say which one matched while the
placeholder stays `<CUSTOM_n>`. A built-in type outranks it wherever the two
overlap. A pattern is checked for catastrophic backtracking when `shim config`
writes it and again by `shim doctor`, never on the hook path: `re` has no
per-match timeout and the hook is what a user is waiting on.

Substitution is the one place a policy setting changes the output text.
`[reveal]` keeps the last one to four digits of an `IBAN`, `CREDIT_CARD` or
`PHONE` span, separators skipped, as `<TYPE_n:tail>`; `guard/entities.py`
holds the single definition of what a placeholder looks like in either form,
and the secret recognizer uses it to leave a value shim already masked alone.
Detection, counting and spans are untouched, so with no table the output is
byte-identical to a build without the feature.

The detector contracts are `guard-v2.json`, `guard-tools-v1.json`,
`custom-v1.json`, `reveal-v1.json`, and `parity-v1.json`. The last is generated migration evidence and is never
regenerated to make a test pass; intentional differences are recorded in
`DELIBERATE_DIVERGENCES`. Detailed evidence belongs in
[Compatibility](compatibility.md), not in this module map.

## Directions

`policy.py` classifies every inspected thing into one of seven directions:
`user-prompt`, `outbound`, `inbound`, `local-write`, `executable-text`,
`local-tool` and `model-output`. The first six come from a hook event, the
client and a tool name. The first five can be masked, blocked or reported
according to their mode; `local-tool` can only be reported, so `decide`
returns `report` for it under both `warn` and `enforce`.

`model-output` is different, and deliberately so. It is the final assistant
text of a turn, handed to the hook by Claude Code at `Stop`; the client has
already shown it, so nothing can be done about it. Its only mode is `observe`,
`decide` refuses `warn` and `enforce` with an error rather than accepting a
mode it could not honour, and `config.py` rejects any other value for it — or
for `Stop` as a per-event override — through the malformed-settings path.
Because acting is impossible, observing has to mean counting: `decide` returns
`report` where every other direction returns `allow`, so the count reaches the
summary. The text itself is never stored, and only the last text block of the
turn is visible (see [Compatibility](compatibility.md)).

## Self-protection

Before a tool call is inspected, `events/pipeline.py` refuses, in every mode, a
shell command that runs `shim` with `config`, `revert` or `uninstall`
(`policy.shim_control` reads the command's quoting, separators, subshells and
here-documents to find the program each simple command runs) and a write tool
whose path `hook._protected` resolves to shim's settings, a client settings
file that holds shim's hooks, or a file in the plugin folder running the hook.
The refusal names only the file or the subcommand, and the record notes
`self-protection`. The routes it does not see, the shell writing a file among
them, are listed in [Privacy](privacy.md#an-agent-turning-shim-off).

## Session records

`session.record` owns the bounded persisted schema and the best-effort
`remember()` effect. Records contain entity names and counts, byte counts,
actions, bounded labels, and a detector-scrubbed target path or URL. They never
contain a finding value, replacement value, prompt, response body, or shell
command. Storage failure cannot disable masking or blocking.

`session/_files.py` owns private descriptor validation, bounded reads, and
non-blocking locked appends shared by the spool and ledger.

The spool uses a hashed session filename in a private OS-temporary directory.
Claude's `Stop` renders only unseen records and `SessionEnd` removes that
session's spool. The ledger is a separate, explicit opt-in store. Exact
permissions, caps, cleanup triggers, and retention are in
[Privacy](privacy.md#what-is-recorded).

## `shim watch`

`watch/proxy.py` forwards, `watch/measure.py` observes a copy, and
`watch/report.py` renders. This is the distribution's sole network path. It
binds to loopback, starts before the client, edits no setting or shell profile,
originates no provider request, rewrites no byte, and never retries a `POST`.
If the proxy cannot start, the client is not launched.

Measurement stays beside the forwarding path: accepted request bodies stream
upstream in bounded chunks, and request detection runs after response relay.
Two non-blocking inspection slots bound retained request copies to 8 MB each;
there is no measurement queue. Saturation and shutdown leave explicit incomplete
measurements. A bounded incremental reader handles SSE and JSON usage, reporting
known, partial, or unavailable usage independently of request inspection.
Request and response bodies are not written to disk. Attribution across tools,
system, and messages is inferred from byte share and marked approximate.

Detection walks the request with `events/payload.py`, the same bounded traversal
the hook uses, so every text leaf is offered to the detector: message content in
either form, tool results, tool call arguments, the system prompt and the tool
definitions. Base64 attachment data and a `thinking` block's `signature` are skipped (a
field named `signature` anywhere else is scanned), and a request past a limit,
or with a text leaf the detector refused or analysed only in part, is reported
as unmeasured rather than partly counted. A request still being measured when
the report is written says so (`measurement unfinished`). The
detector runs with the settings file's entity types and custom patterns, loaded
once before the proxy starts, as `scan` and `audit` do; an unreadable file
refuses to start.

The proxy passes its own limits to that traversal. The hook's defaults bound a
user waiting on a synchronous subprocess; measurement runs after the response
has been relayed, on a body already capped at 8 MB: 50,000 leaves, 8,000,000
characters and 200 levels. One measured Claude Code request held 4,402 text
leaves, 256,517 characters and nested 35 levels deep inside an MCP tool's
recursive JSON schema, against the hook's limits then of 2,000 leaves, 200,000
characters and 24 levels, which would have reported almost every real request
as unmeasured. The hook now has no leaf count, 1,000,000 characters and 64
levels.

Findings are held per section, and the `tools` and `system` results are memoised
per session by content hash, sixteen entries, first in first out.

The same reader accumulates the response. Anthropic `content_block_delta`
payloads are joined per block index — `text_delta` into `text`, `thinking_delta`
into `thinking`, `input_json_delta` ignored because the hook already scans tool
arguments at `PreToolUse` — and a plain JSON body yields its content blocks
directly. Accumulation is capped at 1 MB per exchange. The detector runs only
after the terminating chunk has been written and flushed, and only for an
exchange holding a measurement slot, so the client never waits on it; the text
is dropped before the handler returns. The report keeps the two directions on
separate lines and never sums them.

The usage reader also keeps the provider's stop reason, in whichever shape it
arrives: an Anthropic `delta.stop_reason`, a Responses `status: incomplete`
with its `incomplete_details.reason`, or a chat-completions `finish_reason`.
The first one on the wire wins. Only the three that mean the output limit was
reached produce a line; every reason reaches the JSON totals.
