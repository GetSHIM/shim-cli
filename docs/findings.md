# Findings

`shim doctor --json`, `shim report --json` and `shim watch --json` each carry a
`findings` list beside their usual keys. A finding is one problem, in the shape
shim's gateway uses for its own: [finding schema v1](schemas/finding-v1.json),
copied unchanged from the gateway repository. The list is empty when nothing
fires, and every other key of the three commands keeps its value.

What a finding holds:

| Field | In shim-cli |
| --- | --- |
| `rule_id` | `cli.doctor.<code>`, `cli.session.<rule>` or `cli.watch.<rule>` |
| `id` | The same on every run for the same rule and subject, so a script can tell a new problem from one it has seen |
| `severity` | `informational`, `low`, `medium` or `high` |
| `subject` | `{"kind": "app", "id": "claude"}`, or `{"kind": "model", "id": ...}` for a watch finding about one model; a model the price table does not name is `other` |
| `window` | When it was seen, in UTC: the run time for doctor, the first and last record for report, the watch session for watch |
| `evidence` | Up to 20 references: `doctor:<check>`, `report:record:<n>` (the n-th record of the session) or `watch:exchange:<n>` (the n-th request) |
| `measurements` | The numbers the finding is about |
| `impact` | `requests`, `tokens`, `usd` (a decimal string) and `risk_class` (`privacy`, `cost`, `reliability` or `quality`) |
| `remediation` | `text` in English and Turkish; `action` is `{"kind": "cli.command", "params": {"command": ...}}` when the fix is one shim command, else `null`; `reversible`; `mode` is always `suggest`, because shim-cli never applies a fix itself |
| `playbook` | This page or the error-code table, with an anchor |

**Recomputing an id.** `id` is the UUID version 5 of
`<rule_id>|<subject.kind>|<subject.id>` in the namespace
`753f9b9f-aab5-47d4-9ca6-bb5893974601`. The doctor finding for a missing Claude
Code hook is always the same:

```python
import uuid

namespace = uuid.UUID("753f9b9f-aab5-47d4-9ca6-bb5893974601")
uuid.uuid5(namespace, "cli.doctor.hook_not_installed|app|claude")
# UUID('19a357c5-2de9-5f0f-9fbe-780ca24ca24f')
```

Every sentence in a finding comes from a fixed template filled with the
client's name and the finding's own numbers. No path, value, command line, tool
name or check detail reaches one: the session record behind a `report:record`
reference names the file, and the doctor check behind a `doctor:` reference
carries the detail.

**Doctor findings.** `shim doctor <client> --json` raises one finding per check that is not `PASS`,
with `rule_id` `cli.doctor.` and the check's code in lower case, such as
`cli.doctor.hook_not_installed`. A `FAIL` is `high` and a `WARN` is `low`,
except the two warnings a healthy install prints, `hook_activation_unverified`
and `plugin_not_discoverable`, which are `informational`. What each code means
and how to clear it is the [error code table](commands.md#error-codes). A check
that leaves traffic uninspected (the hook not installed, events missing, hooks
off in Codex, settings invalid or refused, the runner or resolution failing,
duplicate hooks, detection turned off) has `risk_class` `privacy`; the rest
`reliability`.

## `cli.session.unmasked_values`

**Checks:** at least one tool call in the session failed and printed a value
shim found. Claude Code does not let a hook change the output of a failed call,
so the model saw it; the session summary counts it as `unmasked`.

**Why it matters:** the value reached the model provider. `measurements`
counts the events and each entity type (`entities_secret`, `entities_email`).

**What to do:** treat a credential it printed as exposed and rotate it. Point
the agent at `shim keys` instead of `cat` for `.env` files, so a command that
fails cannot print a value; the [cookbook](cookbook.md#let-the-agent-see-the-names-not-the-values)
shows how.

**How it goes away:** a new session with no failed call that prints a value.
Rotating a credential cannot be undone, so `remediation.reversible` is `false`.

## `cli.session.not_inspected`

**Checks:** shim let at least one event through without inspecting it, such as
a file attached with `@` past the limits, or inspected one only in part.

**Why it matters:** whatever was in it reached the model unchecked.

**What to do:** `shim report` names the tool or file; `shim doctor <client>`
checks the install. Large output is better written to a file the agent reads
in parts.

**How it goes away:** a session in which every event is inspected in full.

## `cli.session.injection_markers`

**Checks:** a tool result held text that reads as instructions to the model,
such as "ignore all previous instructions" or invisible characters.
`measurements` counts each marker id (`markers_instruction_override`,
`markers_hidden_text`).

**Why it matters:** a document or command output may be steering the agent.
shim reports it and never changes the result.

**What to do:** read the files `shim report` names and remove instructions you
did not write. To have the model told as well, set `markers = "note"` in the
settings file.

**How it goes away:** a session whose tool results carry no marker.

## `cli.watch.cache_rewrite`

**Checks:** a request wrote again a prefix the previous request of the same
model and conversation had cached, the `cache` line of `shim watch`. One
finding per model.

**Why it matters:** a cache write costs more than a cache read, and the whole
prefix is written again. `impact.tokens` is the tokens written again and
`impact.usd` their cost, when the model is priced and only one model rewrote.

**What to do:** keep `CLAUDE.md` and the tool list stable within a session. A
change early in the prompt, such as an edited instructions file or an MCP server
added mid-session, makes the next request write the whole cached prefix again.

**How it goes away:** a session in which every request reads the prefix the
one before it cached.

## `cli.watch.duplicate_in_flight`

**Checks:** a request was sent while an identical one was still in flight, the
`duplicates` line. Both were forwarded and both were billed.

**Why it matters:** the second answer costs the same as the first.
`impact.tokens` is the input billed again.

**What to do:** look for a client retry or a parallel agent sending the same
request. `shim watch` forwards both and never merges or holds them.

**How it goes away:** a session with no identical requests in flight together.

## `cli.watch.output_cut_off`

**Checks:** a response stopped at the provider's output limit, the `cut off`
line. One finding per model.

**Why it matters:** the answer is incomplete, and the agent may act on half of
it.

**What to do:** raise the output limit (`CLAUDE_CODE_MAX_OUTPUT_TOKENS` in
Claude Code) or ask for a shorter answer.

**How it goes away:** a session in which every response ends on its own.

## `cli.watch.image_tokens`

**Checks:** the session sent at least one image, the `images` line.
`impact.tokens` is the estimate of their input tokens.

**Why it matters:** history replays every image in each later request, so one
screenshot is paid for again on every turn.

**What to do:** crop or shrink screenshots before attaching them, and start a
new session when the image is no longer needed.

**How it goes away:** a session without images.

## `cli.watch.large_tool_result`

**Checks:** the largest tool result was over 25,000 bytes, the `largest tool
result` line. `impact.tokens` estimates it at four bytes a token.

**Why it matters:** a result stays in the history and is sent again with every
later request of the conversation.

**What to do:** ask for less output: filter command output with `head`, `grep`
or a narrower query, or write it to a file the agent reads in parts.

**How it goes away:** a session whose tool results stay under 25,000 bytes.
