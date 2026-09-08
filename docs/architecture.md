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

prompt event -> client codec -> Policy -> detector
             -> allow/report | Copilot rewrite | native block + 0600 suggestion

verified Claude tool event -> Claude adapter -> shared event pipeline
                           -> Policy -> detector, diet, markers
                           -> Claude response + session Record

shim watch -- <client> -> loopback proxy -> configured provider, bytes unchanged
                       -> in-memory sizes, counts, usage, and report
```

A payload's direction determines what may be done to it. `policy.py` owns that
classification and the modes and actions built on it:

- user prompts are reported by default; Codex and Claude can block under
  `enforce`, while Copilot can replace the model-facing prompt;
- structured outbound arguments may be masked before leaving the machine;
- inbound results may be masked before entering model context;
- local writes and executable command text are never rewritten, because doing
  so would change a file or command the user intended.

## Ownership and dependencies

| Owner | Responsibility |
| --- | --- |
| `guard/` | Entity catalog, normalization, recognizers, findings, spans, and typed redaction. |
| `policy.py` | Directions, modes, actions, defaults, `Policy`, classification, and action selection. |
| `config.py` | Config path, TOML parsing/rendering, validation at file ingress, and loading a `Policy`. |
| `events/` | Deterministic tool-payload traversal, context diet, injection markers, and the shared tool-event pipeline. |
| `clients/<client>/` | Native prompt codecs, verified tool codecs, settings fragments, capabilities, and coexistence rules for that client. |
| `session/` | `Record`, timestamps, best-effort `remember()`, spool, ledger, cleanup, and summaries. |
| `settings_files/` | Guarded inspection, pure change planning, parent creation, revalidation, and atomic publication. |
| `watch/` | Proxy forwarding, wire measurement, and watch report values. |
| `hook.py` | Deadline, bounded stdin, envelope dispatch, visible failure behavior, temporary prompt-file lifecycle, and stdout. |
| `cli/` | Command composition and human or JSON presentation. |

The dependency direction is deliberately small:

```text
cli  ------> config, clients, session, settings_files, watch, guard
hook ------> config, clients, events, session, guard
config ----> policy, guard/entities, settings_files, events/diet
clients ---> policy, events, session, settings_files
events ----> policy, guard, session/record
```

`guard`, `policy`, `session`, `settings_files`, and `watch` do not depend on
the CLI or hook. `guard` does not import configuration, `session` does not
import events, events do not import clients, and the hook does not import
`watch`. `tests/contracts/test_import_hygiene.py` enforces these boundaries.
Composition stays explicit in `hook.py` and the CLI; there is no dependency
container, dynamic adapter registry, or architecture framework.

## Client protocols

Each client directory owns its native protocol. Prompt hooks are supported for
all three clients:

| Client | Installed prompt event | Verified installed tool events |
| --- | --- | --- |
| Claude Code | `UserPromptSubmit` | `PreToolUse`, `PostToolUse` |
| Codex CLI | `UserPromptSubmit` | None |
| GitHub Copilot CLI | `userPromptTransformed` | None |

Claude's verified event list and encoders live together in
`clients/claude/tool_events.py`; its settings also install `Stop` for summaries
and `SessionEnd` for cleanup. Codex and Copilot have prompt codecs and settings
only. A new tool adapter requires a live protocol probe, a synthetic fixture,
and a verified mutation or report channel; it is not enabled by a flag.

Codex installation leaves inline `config.toml` hooks untouched. Claude
installation changes only shim's exact groups in user `settings.json`.
Copilot owns its dedicated `hooks/shim.json` and retains an empty
versioned document on revert. All clients preserve unrelated settings;
malformed, ambiguous, unsafe, or concurrently changed files require manual
action.

## Detector and event processing

The detector is deterministic, bounded, offline, and independent of CLI,
configuration, clients, events, sessions, and watch. Recognizers and checksums
live in `guard/recognizers.py`; the public suffix table is compiled into
`guard/suffixes.py`. `phonenumbers` is the one third-party module allowed on
the hook path. No detector input or lookup is sent over the network.

`events/payload.inspect` walks a tool payload once. Masking and context diet
may rewrite eligible leaves; injection markers only report and can never reach
a replacement. Diet runs only on rewrite-capable inbound results outside
`observe`, and never on a view of a local file whose exact bytes a later edit
may need. Lossless JSON compaction is the default; trailing-whitespace removal
is available only through explicit configuration because it can change
Markdown hard breaks.

The detector contracts are `guard-v2.json`, `guard-tools-v1.json`, and
`parity-v1.json`. The last is generated migration evidence and is never
regenerated to make a test pass; intentional differences are recorded in
`DELIBERATE_DIVERGENCES`. Detailed evidence belongs in
[Compatibility](compatibility.md), not in this module map.

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
